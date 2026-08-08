from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent_control_plane.entities.job import TERMINAL_STATUSES, JobRecord, JobStore
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.sqlite_runtime import control_database

DEFAULT_STALE_AFTER_SEC = 300.0
DEFAULT_READ_RETRIES = 3
DEFAULT_READ_RETRY_BACKOFF_SEC = 0.05
DEFAULT_ERROR_SURFACE_THRESHOLD = 3

START = "start"
TRANSITION = "transition"
TERMINAL = "terminal"
STALE = "stale"
RESUMED = "resumed"
WATCH_ERROR = "watch_error"

EVENT_KINDS = frozenset({START, TRANSITION, TERMINAL, STALE, RESUMED, WATCH_ERROR})


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
    result_status: str | None = None
    on_contract: bool | None = None
    last_error: str | None = None
    heartbeat_age_sec: float | None = None
    message: str | None = None
    at: str = field(default_factory=utc_now)


def is_on_contract(job: JobRecord) -> bool:
    """A job is on-contract when it lands on its own declared expected status."""
    return job.status == job.expected_result_status and job.finalization_status == "completed"


@dataclass
class _JobWatchState:
    last_status: str | None = None
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

    def all_terminal(self) -> bool:
        return bool(self._states) and all(
            state.last_status in TERMINAL_STATUSES for state in self._states.values()
        )

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
        stop_when_all_terminal: bool = True,
        max_ticks: int | None = None,
    ) -> Iterator[WatchEvent]:
        """Convenience generator: tick, yield, sleep, repeat until done."""
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        ticks = 0
        while True:
            yield from self.tick()
            ticks += 1
            if stop_when_all_terminal and self.all_terminal():
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
        if state.last_status is None:
            events.append(self._event(START, job))
        if job.status != state.last_status:
            kind = TERMINAL if job.status in TERMINAL_STATUSES else TRANSITION
            events.append(self._event(kind, job))
        state.last_status = job.status
        if job.status not in TERMINAL_STATUSES:
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
        except ValueError:
            return None
        now = self._clock()
        age: timedelta = now - beat
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
        return WatchEvent(
            kind=kind,
            job_id=job.job_id,
            task_id=job.task_id,
            status=job.status,
            expected_result_status=job.expected_result_status,
            finalization_status=job.finalization_status,
            result_status=job.status if kind == TERMINAL else None,
            on_contract=is_on_contract(job) if kind == TERMINAL else None,
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
