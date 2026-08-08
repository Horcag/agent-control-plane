from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.entities.plan import PlanStore, PlanTaskDefinition
from agent_control_plane.features.job_watch import (
    RESUMED,
    STALE,
    START,
    TERMINAL,
    TRANSITION,
    WATCH_ERROR,
    EmptySelectionError,
    WatchEvent,
    WatchEventStream,
    WatchSelection,
    is_on_contract,
)


def _create_job(store: JobStore, root: Path, job_id: str, **overrides: Any) -> JobRecord:
    kwargs: dict[str, Any] = {
        "job_id": job_id,
        "task_id": f"task-{job_id}",
        "route": "main",
        "workspace_path": root / "repo",
        "expected_branch": "main",
        "config_path": root / "workspaces.toml",
        "run_dir": root / "runs" / job_id,
        "prompt_path": root / "runs" / job_id / "prompt.md",
        "result_path": root / "tasks" / job_id / "result.md",
        "timeout_sec": 60,
        "idle_timeout_sec": 30,
        "print_timeout": "1m",
        "max_restarts": 0,
        "yolo": False,
        "allow_dirty": False,
        "read_only": False,
    }
    kwargs.update(overrides)
    return store.create_job(**kwargs)


class _FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _stream(
    store: JobStore,
    selection: WatchSelection,
    *,
    clock: _FakeClock | None = None,
    sleep: _FakeSleep | None = None,
    **kwargs: Any,
) -> WatchEventStream:
    return WatchEventStream(
        store,
        selection,
        clock=clock or _FakeClock(datetime.now(UTC)),
        sleep=sleep or _FakeSleep(),
        **kwargs,
    )


def test_empty_selection_raises(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()

    with pytest.raises(EmptySelectionError):
        _stream(store, WatchSelection())

    with pytest.raises(EmptySelectionError):
        _stream(store, WatchSelection(plan_id="no-such-plan"))

    with pytest.raises(EmptySelectionError):
        _stream(store, WatchSelection(task_id_glob="no-such-task-*"))


def test_explicit_nonexistent_job_id_fails_fast_on_tick(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()

    # Explicit job_ids are trusted at selection time (the caller just created
    # them); a job_id that never existed is a caller error, not a transient
    # store outage, so it must surface immediately rather than being retried
    # into silence or folded into "matched nothing".
    stream = _stream(store, WatchSelection(job_ids=frozenset({"missing-job"})))
    with pytest.raises(KeyError):
        stream.tick()


def test_start_then_transition_then_terminal_dedup(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = _create_job(store, tmp_path, "job-1")
    stream = _stream(store, WatchSelection(job_ids=frozenset({job.job_id})))

    first = stream.tick()
    assert [event.kind for event in first] == [START, TRANSITION]
    assert first[0].status == "created"

    # No change -> no events.
    assert stream.tick() == []

    store.update_job(job.job_id, status="running", started_at="2026-08-09T00:00:00+00:00")
    running_events = stream.tick()
    assert [event.kind for event in running_events] == [TRANSITION]
    assert running_events[0].status == "running"

    store.mark_finished(job.job_id, "completed")
    store.mark_finalization_completed(job.job_id)
    terminal_events = stream.tick()
    assert [event.kind for event in terminal_events] == [TERMINAL]
    terminal_event = terminal_events[0]
    assert terminal_event.status == "completed"
    assert terminal_event.result_status == "completed"
    assert terminal_event.on_contract is True

    # Terminal status is stable -> stream goes quiet and reports done.
    assert stream.tick() == []
    assert stream.all_terminal() is True


def test_off_contract_terminal_reports_on_contract_false(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = _create_job(store, tmp_path, "job-2", expected_result_status="completed")
    stream = _stream(store, WatchSelection(job_ids=frozenset({job.job_id})))
    stream.tick()  # consume start/transition for "created"

    store.mark_finished(job.job_id, "partial")
    store.mark_finalization_completed(job.job_id)
    events = stream.tick()

    assert [event.kind for event in events] == [TERMINAL]
    assert events[0].status == "partial"
    assert events[0].expected_result_status == "completed"
    assert events[0].on_contract is False


def test_is_on_contract_matches_expected_result_status_and_finalization(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = _create_job(store, tmp_path, "job-3", expected_result_status="partial")
    store.mark_finished(job.job_id, "partial")
    on_contract_job = store.get_job(job.job_id)
    assert is_on_contract(on_contract_job) is False  # finalization still "pending"

    store.mark_finalization_completed(job.job_id)
    assert is_on_contract(store.get_job(job.job_id)) is True

    store.mark_finalization_failed(job.job_id, "boom")
    assert is_on_contract(store.get_job(job.job_id)) is False


def test_stale_then_resumed_heartbeat(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = _create_job(store, tmp_path, "job-4")
    store.update_job(job.job_id, status="running")

    now = datetime.now(UTC)
    heartbeat_at = now.isoformat(timespec="seconds")
    store.update_job(job.job_id, worker_heartbeat_at=heartbeat_at)

    clock = _FakeClock(now)
    stream = _stream(store, WatchSelection(job_ids=frozenset({job.job_id})), clock=clock)
    stream.tick()  # consume start/transition

    # Not stale yet.
    clock.advance(100)
    assert stream.tick() == []

    # Cross the default 300s threshold.
    clock.advance(250)
    stale_events = stream.tick()
    assert [event.kind for event in stale_events] == [STALE]
    assert stale_events[0].heartbeat_age_sec is not None
    assert stale_events[0].heartbeat_age_sec >= 300.0

    # Same heartbeat, still stale -> emitted once only.
    clock.advance(10)
    assert stream.tick() == []

    # Worker beats again with a fresh heartbeat -> resumed.
    new_heartbeat = clock().isoformat(timespec="seconds")
    store.update_job(job.job_id, worker_heartbeat_at=new_heartbeat)
    resumed_events = stream.tick()
    assert [event.kind for event in resumed_events] == [RESUMED]

    # And it can go stale again after resuming.
    clock.advance(400)
    stale_again = stream.tick()
    assert [event.kind for event in stale_again] == [STALE]


def test_watch_error_after_consecutive_store_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = _create_job(store, tmp_path, "job-5")
    sleep = _FakeSleep()
    stream = _stream(
        store,
        WatchSelection(job_ids=frozenset({job.job_id})),
        sleep=sleep,
        read_retries=2,
        error_surface_threshold=2,
    )
    stream.tick()  # consume start/transition, resets failure counter

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "get_job", _boom)

    # First failing tick: under threshold, retried internally, stays silent.
    assert stream.tick() == []
    assert sleep.calls  # retry backoff was exercised without a real sleep

    # Second consecutive failing tick crosses the threshold.
    error_events = stream.tick()
    assert [event.kind for event in error_events] == [WATCH_ERROR]
    assert error_events[0].job_id == job.job_id
    assert "locked" in (error_events[0].message or "")

    # Once the store recovers, normal polling resumes with no leftover error state.
    monkeypatch.undo()
    assert stream.tick() == []


def test_plan_id_selection_matches_bound_jobs(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = JobStore(database)
    plans = PlanStore(database)
    job = _create_job(store, tmp_path, "job-6")
    plans.create_plan(
        plan_id="plan-a",
        title="Plan A",
        tasks=(PlanTaskDefinition("only", "Only task"),),
    )
    plans.bind_job("plan-a", "only", job.job_id)

    stream = _stream(store, WatchSelection(plan_id="plan-a"))
    assert stream.job_ids == frozenset({job.job_id})


def test_task_id_glob_selection_matches_by_pattern(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    matching = _create_job(store, tmp_path, "job-glob-1", task_id="acp-watch-alpha")
    _create_job(store, tmp_path, "job-glob-2", task_id="other-task")

    stream = _stream(store, WatchSelection(task_id_glob="acp-watch-*"))

    assert stream.job_ids == frozenset({matching.job_id})


def test_watch_event_is_frozen_dataclass() -> None:
    event = WatchEvent(
        kind=START,
        job_id="job",
        task_id="task",
        status="created",
        expected_result_status="completed",
        finalization_status="not_started",
    )
    with pytest.raises(AttributeError):
        event.status = "running"  # type: ignore[misc]
