from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agent_control_plane.entities.job import TERMINAL_STATUSES, JobRecord, JobStore
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.sqlite_runtime import control_database

DEFAULT_STALE_AFTER_SEC = 300.0
DEFAULT_READ_RETRIES = 3
DEFAULT_READ_RETRY_BACKOFF_SEC = 0.05
DEFAULT_ERROR_SURFACE_THRESHOLD = 3

# finalization_status values that mean the checkpoint/gate battery has been decided,
# as opposed to "not_started"/"pending" which are still in flight.
FINALIZATION_SETTLED_STATUSES = frozenset({"completed", "failed"})

START = "start"
TRANSITION = "transition"
TERMINAL = "terminal"
STALE = "stale"
RESUMED = "resumed"
WATCH_ERROR = "watch_error"

EVENT_KINDS = frozenset({START, TRANSITION, TERMINAL, STALE, RESUMED, WATCH_ERROR})

# Bumped whenever the cursor payload stops being readable by the previous decoder.
CURSOR_VERSION = 1


# Slack added to the longest watched job timeout so the watch outlives finalization
# (checkpoint plus the controller gate battery), which runs after the worker exits.
FINALIZATION_SLACK_SEC = 600


def watch_command_for(
    job_ids: Iterable[str],
    *,
    config_path: str | None = None,
    timeout_sec: float | None = None,
) -> str:
    """Build the exact shell invocation that supervises ``job_ids``.

    Callers hand this to whatever turns stdout lines into notifications. It exists so
    nobody has to assemble the flags — an assembled-by-hand watch is where the config
    path gets guessed and the timeout gets invented.
    """
    parts = ["agent-control", "watch", "--events"]
    if config_path:
        parts += ["--config", f'"{config_path}"']
    if timeout_sec is not None:
        parts += ["--timeout-sec", str(int(timeout_sec))]
    parts += sorted(job_ids)
    return " ".join(parts)


class EmptySelectionError(ValueError):
    """Raised when a watch selection matches no jobs."""


def _default_clock() -> datetime:
    return datetime.fromisoformat(utc_now())


@dataclass(frozen=True)
class WatchSelection:
    """Which jobs a :class:`WatchEventStream` should follow.

    ``job_ids`` are watched directly; ``plan_id`` pulls in every job currently
    bound to that plan; ``task_id_glob`` matches job task IDs with SQLite GLOB
    syntax (``*``, ``?``, ``[...]``). The selectors are additive (union).
    """

    job_ids: frozenset[str] = frozenset()
    plan_id: str | None = None
    task_id_glob: str | None = None

    def is_empty(self) -> bool:
        return not self.job_ids and not self.plan_id and not self.task_id_glob


@dataclass(frozen=True)
class WatchEvent:
    kind: str
    job_id: str
    task_id: str
    status: str
    expected_result_status: str
    finalization_status: str
    on_contract: bool | None = None
    last_error: str | None = None
    heartbeat_age_sec: float | None = None
    message: str | None = None
    at: str = field(default_factory=utc_now)


def is_on_contract(job: JobRecord) -> bool:
    """A job is on-contract when it lands on its own declared expected status."""
    return job.status == job.expected_result_status and job.finalization_status == "completed"


def is_settled(job: JobRecord) -> bool:
    """A job is done, for watch purposes, once its status is terminal and finalization
    has been decided (not still "not_started"/"pending")."""
    return (
        job.status in TERMINAL_STATUSES and job.finalization_status in FINALIZATION_SETTLED_STATUSES
    )


@dataclass
class _JobWatchState:
    last_status: str | None = None
    settled: bool = False
    stale_emitted_heartbeat: str | None = None
    consecutive_failures: int = 0


class WatchEventStream:
    """Poll a :class:`JobStore` and yield deduplicated, typed watch events.

    The stream owns all dedup and terminal-status detection; callers never
    need to track a ``seen`` set or retype ``TERMINAL_STATUSES``. ``clock``
    and ``sleep`` are injectable so tests can drive staleness and retry
    behaviour without waiting on a real clock.
    """

    def __init__(
        self,
        store: JobStore,
        selection: WatchSelection,
        *,
        stale_after_sec: float = DEFAULT_STALE_AFTER_SEC,
        read_retries: int = DEFAULT_READ_RETRIES,
        read_retry_backoff_sec: float = DEFAULT_READ_RETRY_BACKOFF_SEC,
        error_surface_threshold: int = DEFAULT_ERROR_SURFACE_THRESHOLD,
        clock: Callable[[], datetime] = _default_clock,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if stale_after_sec <= 0:
            raise ValueError("stale_after_sec must be positive")
        if read_retries < 1:
            raise ValueError("read_retries must be at least 1")
        if error_surface_threshold < 1:
            raise ValueError("error_surface_threshold must be at least 1")
        self._store = store
        self._selection = selection
        self._stale_after_sec = stale_after_sec
        self._read_retries = read_retries
        self._read_retry_backoff_sec = read_retry_backoff_sec
        self._error_surface_threshold = error_surface_threshold
        self._clock = clock
        self._sleep = sleep
        self._selection_failures = 0
        self._store.initialize()
        job_ids = self._resolve_selection()
        if not job_ids:
            raise EmptySelectionError(
                "Watch selection matched no jobs: "
                f"job_ids={sorted(selection.job_ids)!r} plan_id={selection.plan_id!r} "
                f"task_id_glob={selection.task_id_glob!r}"
            )
        self._states: dict[str, _JobWatchState] = {job_id: _JobWatchState() for job_id in job_ids}

    @property
    def job_ids(self) -> frozenset[str]:
        return frozenset(self._states)

    def export_cursor(self) -> dict[str, Any]:
        """Serialize dedup state so a stateless caller can resume on the next call.

        A process that cannot keep the stream alive between polls (an MCP tool
        answering one request at a time) round-trips this payload instead of
        re-deriving "have I already reported this?" from the job table.
        """
        return {
            "v": CURSOR_VERSION,
            "jobs": {
                job_id: {
                    "status": state.last_status,
                    "settled": state.settled,
                    "stale_beat": state.stale_emitted_heartbeat,
                }
                for job_id, state in sorted(self._states.items())
            },
        }

    def load_cursor(self, cursor: Mapping[str, Any] | None) -> None:
        """Restore dedup state produced by :meth:`export_cursor`.

        Entries for jobs outside the current selection are dropped rather than
        re-added: a job that left the selection must not keep a watch pending
        forever.
        """
        if not cursor:
            return
        version = cursor.get("v")
        if version != CURSOR_VERSION:
            raise ValueError(
                f"unsupported watch cursor version {version!r}; expected {CURSOR_VERSION}"
            )
        jobs = cursor.get("jobs")
        if jobs is None:
            return
        if not isinstance(jobs, Mapping):
            raise ValueError("watch cursor 'jobs' must be a mapping")
        for raw_job_id, raw_state in jobs.items():
            state = self._states.get(str(raw_job_id))
            if state is None:
                continue
            if not isinstance(raw_state, Mapping):
                raise ValueError(f"watch cursor entry for {str(raw_job_id)!r} must be a mapping")
            last_status = raw_state.get("status")
            state.last_status = None if last_status is None else str(last_status)
            state.settled = bool(raw_state.get("settled", False))
            stale_beat = raw_state.get("stale_beat")
            state.stale_emitted_heartbeat = None if stale_beat is None else str(stale_beat)

    def all_settled(self) -> bool:
        return bool(self._states) and all(state.settled for state in self._states.values())

    def tick(self) -> list[WatchEvent]:
        """Poll every watched job once and return the events observed this tick."""
        events: list[WatchEvent] = []
        try:
            discovered = self._resolve_selection_with_retry()
            self._selection_failures = 0
        except sqlite3.Error as exc:
            self._selection_failures += 1
            discovered = frozenset(self._states)
            if self._selection_failures >= self._error_surface_threshold:
                events.append(self._selection_error_event(exc))
        for job_id in discovered:
            self._states.setdefault(job_id, _JobWatchState())
        for job_id, state in list(self._states.items()):
            events.extend(self._tick_job(job_id, state))
        return events

    def run(
        self,
        *,
        poll_interval_sec: float,
        stop_when_all_settled: bool = True,
        max_ticks: int | None = None,
    ) -> Iterator[WatchEvent]:
        """Convenience generator: tick, yield, sleep, repeat until done."""
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        ticks = 0
        while True:
            yield from self.tick()
            ticks += 1
            if stop_when_all_settled and self.all_settled():
                return
            if max_ticks is not None and ticks >= max_ticks:
                return
            self._sleep(poll_interval_sec)

    def _tick_job(self, job_id: str, state: _JobWatchState) -> list[WatchEvent]:
        try:
            job = self._read_job_with_retry(job_id)
        except sqlite3.Error as exc:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._error_surface_threshold:
                return [self._job_error_event(job_id, exc)]
            return []
        state.consecutive_failures = 0
        events: list[WatchEvent] = []
        is_terminal_status = job.status in TERMINAL_STATUSES
        settled_now = is_settled(job)
        if state.last_status is None:
            kind = TERMINAL if is_terminal_status else START
            events.append(self._event(kind, job))
        elif job.status != state.last_status:
            kind = TERMINAL if is_terminal_status else TRANSITION
            events.append(self._event(kind, job))
        elif settled_now and not state.settled:
            # Status already went terminal on an earlier tick; finalization has just
            # been decided. That's the fact the exit code is actually based on.
            events.append(self._event(TERMINAL, job))
        state.last_status = job.status
        if settled_now:
            state.settled = True
        if not is_terminal_status:
            events.extend(self._heartbeat_events(job, state))
        return events

    def _heartbeat_events(self, job: JobRecord, state: _JobWatchState) -> list[WatchEvent]:
        heartbeat_at = job.worker_heartbeat_at
        if heartbeat_at is None:
            return []
        age = self._heartbeat_age_sec(heartbeat_at)
        if age is None:
            return []
        if age >= self._stale_after_sec:
            if state.stale_emitted_heartbeat == heartbeat_at:
                return []
            state.stale_emitted_heartbeat = heartbeat_at
            return [self._event(STALE, job, heartbeat_age_sec=age)]
        if state.stale_emitted_heartbeat is not None:
            state.stale_emitted_heartbeat = None
            return [self._event(RESUMED, job, heartbeat_age_sec=age)]
        return []

    def _heartbeat_age_sec(self, heartbeat_at: str) -> float | None:
        try:
            beat = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
            now = self._clock()
            age: timedelta = now - beat
        except (TypeError, ValueError):
            return None
        return max(0.0, age.total_seconds())

    def _read_job_with_retry(self, job_id: str) -> JobRecord:
        last_exc: sqlite3.Error | None = None
        for attempt in range(self._read_retries):
            try:
                return self._store.get_job(job_id)
            except sqlite3.Error as exc:
                last_exc = exc
                if attempt + 1 < self._read_retries:
                    self._sleep(self._read_retry_backoff_sec * (attempt + 1))
        assert last_exc is not None  # nosec B101 - loop above always sets it on failure
        raise last_exc

    def _resolve_selection_with_retry(self) -> frozenset[str]:
        last_exc: sqlite3.Error | None = None
        for attempt in range(self._read_retries):
            try:
                return self._resolve_selection()
            except sqlite3.Error as exc:
                last_exc = exc
                if attempt + 1 < self._read_retries:
                    self._sleep(self._read_retry_backoff_sec * (attempt + 1))
        assert last_exc is not None  # nosec B101 - loop above always sets it on failure
        raise last_exc

    def _resolve_selection(self) -> frozenset[str]:
        job_ids = set(self._selection.job_ids)
        if self._selection.plan_id:
            job_ids |= _job_ids_for_plan(self._store, self._selection.plan_id)
        if self._selection.task_id_glob:
            job_ids |= _job_ids_matching_task_glob(self._store, self._selection.task_id_glob)
        return frozenset(job_ids)

    def _event(
        self,
        kind: str,
        job: JobRecord,
        *,
        heartbeat_age_sec: float | None = None,
    ) -> WatchEvent:
        settled = is_settled(job)
        return WatchEvent(
            kind=kind,
            job_id=job.job_id,
            task_id=job.task_id,
            status=job.status,
            expected_result_status=job.expected_result_status,
            finalization_status=job.finalization_status,
            on_contract=is_on_contract(job) if settled else None,
            last_error=job.last_error,
            heartbeat_age_sec=heartbeat_age_sec,
        )

    def _selection_error_event(self, exc: Exception) -> WatchEvent:
        return WatchEvent(
            kind=WATCH_ERROR,
            job_id="",
            task_id="",
            status="",
            expected_result_status="",
            finalization_status="",
            message=f"watch selection resolution failed: {exc}",
        )

    def _job_error_event(self, job_id: str, exc: Exception) -> WatchEvent:
        return WatchEvent(
            kind=WATCH_ERROR,
            job_id=job_id,
            task_id="",
            status="",
            expected_result_status="",
            finalization_status="",
            message=f"store read failed after {self._error_surface_threshold} consecutive attempts: {exc}",
        )


def _job_ids_for_plan(store: JobStore, plan_id: str) -> frozenset[str]:
    with control_database(store.database_path) as db:
        tables = {
            str(row["name"])
            for row in db.execute("select name from sqlite_master where type = 'table'").fetchall()
        }
        if "plan_tasks" not in tables:
            return frozenset()
        rows = db.execute(
            "select distinct job_id from plan_tasks where plan_id = ? and job_id is not null",
            (plan_id,),
        ).fetchall()
    return frozenset(str(row["job_id"]) for row in rows)


def _job_ids_matching_task_glob(store: JobStore, pattern: str) -> frozenset[str]:
    with control_database(store.database_path) as db:
        rows = db.execute("select job_id from jobs where task_id glob ?", (pattern,)).fetchall()
    return frozenset(str(row["job_id"]) for row in rows)
