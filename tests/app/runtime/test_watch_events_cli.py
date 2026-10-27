from __future__ import annotations

import io
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from agent_control_plane.app.runtime.cli import (
    _build_parser,
    _handle_watch_command,
    _handle_watch_events,
    _print_statuses,
    _statuses_payload,
    _write_event_line,
)
from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.plan import PlanStore, PlanTaskDefinition
from agent_control_plane.features.job_watch import RESUMED, STALE, WatchEvent
from agent_control_plane.shared.config import (
    CodexModelCatalogConfig,
    CodexQuotaDomainConfig,
    ControlConfig,
    ControlDefaults,
    RouteConfig,
)


class _FakeMonotonic:
    """A float-returning clock/sleep pair: sleep() advances the clock instead of blocking."""

    def __init__(self, *, on_sleep: Any = None) -> None:
        self.value = 0.0
        self._on_sleep = on_sleep

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds
        if self._on_sleep is not None:
            self._on_sleep()


def _config(root: Path) -> ControlConfig:
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=root / "repo",
        slot_root=root / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=ControlDefaults(
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=0,
            yolo=False,
            allow_dirty=False,
            prepare_slots=False,
            guardrail_poll_sec=2.0,
            forbidden_status_globs=("uv.lock", ".venv/**"),
            runs_layout="date",
            auto_archive_days=None,
            auto_archive_limit=200,
            codex_model="test-codex",
            codex_mechanical_model="test-codex",
            codex_balanced_model="test-codex",
            codex_deep_model="test-codex",
        ),
        model_catalog=_model_catalog(root),
        routes=MappingProxyType(
            {
                "main": RouteConfig(
                    name="main",
                    path=root / "repo",
                    required_branch="main",
                    worktree_root=root / "worktrees",
                    worktree_base=root / "repo",
                    source_roots=(Path("backend"),),
                    test_roots=(Path("backend/tests"),),
                    exclude_dirs=(),
                )
            }
        ),
        slots=MappingProxyType({}),
        slot_prepare=(),
    )


def _model_catalog(root: Path) -> CodexModelCatalogConfig:
    cache_path = root / "models_cache.json"
    cache_path.write_text(
        json.dumps(
            {"models": [{"slug": "test-codex", "supported_reasoning_levels": ["low", "medium"]}]}
        ),
        encoding="utf-8",
    )
    return CodexModelCatalogConfig(
        cache_path=cache_path,
        max_cache_age_sec=60.0,
        models=(),
        quota_domains=(CodexQuotaDomainConfig("primary", 2, 8, 75.0),),
    )


def _create_job(store: JobStore, root: Path, job_id: str, **overrides: Any):
    kwargs: dict[str, Any] = {
        "job_id": job_id,
        "task_id": f"task-{job_id}",
        "route": "main",
        "workspace_path": root / "workspace",
        "expected_branch": "main",
        "config_path": root / "workspaces.toml",
        "run_dir": root / "runs" / job_id,
        "prompt_path": root / "runs" / job_id / "prompt.md",
        "result_path": root / "tasks" / job_id / "result.md",
        "timeout_sec": 10,
        "idle_timeout_sec": 5,
        "print_timeout": "10s",
        "max_restarts": 0,
        "yolo": False,
        "allow_dirty": False,
        "read_only": False,
    }
    kwargs.update(overrides)
    return store.create_job(**kwargs)


def _parse_watch(argv: list[str]):
    return _build_parser().parse_args(["watch", *argv])


def test_events_terminal_on_contract_exits_zero_with_terminal_line(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    lines = out.getvalue().splitlines()
    assert rc == 0
    assert any(" TERMINAL " in line and "status=completed" in line for line in lines)
    assert lines[-1].split()[1] == "SUMMARY"
    assert "on_contract=1" in lines[-1]
    assert "off_contract=0" in lines[-1]
    assert "timed_out=false" in lines[-1]


@pytest.mark.parametrize("status", ["failed", "guardrail_violation"])
def test_events_terminal_off_contract_exits_one(tmp_path: Path, status: str) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, status)
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 1
    lines = out.getvalue().splitlines()
    assert any(f"status={status}" in line and " TERMINAL " in line for line in lines)
    assert "off_contract=1" in lines[-1]


def test_events_contract_mismatch_exits_one(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1", expected_result_status="completed")
    control.store.mark_finished(job.job_id, "contract_mismatch")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 1
    lines = out.getvalue().splitlines()
    assert any("status=contract_mismatch" in line and " TERMINAL " in line for line in lines)


def test_events_timeout_with_running_job_exits_three(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.update_job(job.job_id, status="running")
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "5", "--timeout-sec", "10"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 3
    lines = out.getvalue().splitlines()
    assert not any(" TERMINAL " in line for line in lines)
    assert "timed_out=true" in lines[-1]
    assert "non_terminal=1" in lines[-1]


def test_events_terminal_status_pending_finalization_does_not_stop_the_watch(
    tmp_path: Path,
) -> None:
    # A job reaches a terminal status before finalization (checkpointing, the
    # controller gate battery) has run. The watch must keep polling instead of
    # judging the contract against a decision that has not been made yet.
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "completed")
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "3"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 3
    lines = out.getvalue().splitlines()
    assert any(" TERMINAL " in line and "status=completed" in line for line in lines)
    assert "timed_out=true" in lines[-1]
    assert "on_contract=0" in lines[-1]


def test_events_finalization_settling_after_terminal_status_exits_zero(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "completed")
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    calls = {"count": 0}

    def _settle_finalization() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            control.store.mark_finalization_completed(job.job_id)

    fake = _FakeMonotonic(on_sleep=_settle_finalization)

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    lines = out.getvalue().splitlines()
    assert rc == 0
    assert sum(1 for line in lines if " TERMINAL " in line) == 2
    assert "on_contract=1" in lines[-1]
    assert "timed_out=false" in lines[-1]


def test_events_terminal_with_failed_finalization_exits_one(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_failed(job.job_id, "checkpoint gate failed")
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 1
    lines = out.getvalue().splitlines()
    assert "off_contract=1" in lines[-1]
    assert "timed_out=false" in lines[-1]


def test_events_no_line_ever_contains_a_result_token(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    job.result_path.parent.mkdir(parents=True, exist_ok=True)
    job.result_path.write_text("Status: completed\n", encoding="utf-8")
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 0
    lines = out.getvalue().splitlines()
    assert lines  # sanity: the run actually produced output
    assert not any("result=" in line for line in lines)


def test_events_selector_matching_nothing_exits_four_not_zero(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    args = _parse_watch(["--events", "--task-glob", "no-such-task-*", "--timeout-sec", "1"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 4
    lines = out.getvalue().splitlines()
    assert any(" WATCH-ERROR " in line for line in lines)
    assert lines[-1].split()[1] == "SUMMARY"
    assert "jobs=0" in lines[-1]


def test_events_one_line_per_state_change_no_duplicates(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "5", "--timeout-sec", "30"])
    out = io.StringIO()
    calls = {"count": 0}

    def _advance_job() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            control.store.update_job(job.job_id, status="running")
        elif calls["count"] == 2:
            control.store.mark_finished(job.job_id, "completed")
            control.store.mark_finalization_completed(job.job_id)

    fake = _FakeMonotonic(on_sleep=_advance_job)

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    lines = out.getvalue().splitlines()
    kinds = [line.split()[1] for line in lines]
    assert rc == 0
    # One line per distinct state observed (created -> running -> completed), plus
    # the summary -- the first observation must not also emit a redundant TRANSITION.
    assert kinds == ["START", "TRANSITION", "TERMINAL", "SUMMARY"]
    # No duplicate lines at all -- every printed line is distinct.
    assert len(lines) == len(set(lines))


def test_events_job_first_observed_terminal_emits_single_line(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    lines = out.getvalue().splitlines()
    kinds = [line.split()[1] for line in lines]
    assert rc == 0
    assert kinds == ["TERMINAL", "SUMMARY"]


def test_events_successful_job_has_no_error_field(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(
        job.job_id, "completed", last_error="Result file completed with status completed"
    )
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 0
    lines = out.getvalue().splitlines()
    assert any(" TERMINAL " in line and "status=completed" in line for line in lines)
    assert not any("error=" in line for line in lines)


def test_events_failed_job_has_error_field(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "failed", last_error="worker crashed: boom")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 1
    lines = out.getvalue().splitlines()
    terminal_line = next(line for line in lines if " TERMINAL " in line)
    assert "error=worker crashed: boom" in terminal_line


def test_stale_and_resumed_lines_are_rendered() -> None:
    # STALE/RESUMED formatting is driven directly rather than through the full poll
    # loop: the stream's heartbeat clock is real wall time (not the injected
    # poll-loop clock), so pinning the rendered line here is the deterministic way
    # to cover it.
    stale_event = WatchEvent(
        kind=STALE,
        job_id="job-1",
        task_id="task-job-1",
        status="running",
        expected_result_status="completed",
        finalization_status="not_started",
        heartbeat_age_sec=305.0,
        at="2026-08-09T20:00:00+00:00",
    )
    resumed_event = WatchEvent(
        kind=RESUMED,
        job_id="job-1",
        task_id="task-job-1",
        status="running",
        expected_result_status="completed",
        finalization_status="not_started",
        heartbeat_age_sec=12.0,
        at="2026-08-09T20:05:00+00:00",
    )
    out = io.StringIO()

    _write_event_line(out, stale_event)
    _write_event_line(out, resumed_event)

    lines = out.getvalue().splitlines()
    assert lines[0] == (
        "2026-08-09T20:00:00+00:00 STALE job=task-job-1 status=running finalization=not_started"
    )
    assert lines[1] == (
        "2026-08-09T20:05:00+00:00 RESUMED job=task-job-1 status=running finalization=not_started"
    )


def test_events_plan_selection_watches_bound_job(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    plans = PlanStore(control.config.database_path)
    job = _create_job(control.store, tmp_path, "job-1")
    plans.create_plan(
        plan_id="plan-a",
        title="Plan A",
        tasks=(PlanTaskDefinition("only", "Only task"),),
    )
    plans.bind_job("plan-a", "only", job.job_id)
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch(["--events", "--plan", "plan-a", "--timeout-sec", "5"])
    out = io.StringIO()
    fake = _FakeMonotonic()

    rc = _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep)

    assert rc == 0
    assert "on_contract=1" in out.getvalue().splitlines()[-1]


def test_default_watch_without_events_output_is_byte_for_byte_unchanged(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)

    expected = control.watch_job(
        job.job_id,
        poll_interval_sec=0,
        timeout_sec=5,
        log_lines=80,
        include_details=True,
    )
    args = _parse_watch([job.job_id, "--poll-interval-sec", "0", "--timeout-sec", "5"])

    import agent_control_plane.app.runtime.cli as cli_module

    captured: list[dict[str, Any]] = []
    original = cli_module._print_json
    cli_module._print_json = captured.append
    try:
        rc = _handle_watch_command(control, args)
    finally:
        cli_module._print_json = original

    assert rc == 0
    assert len(captured) == 1
    actual = captured[0]
    # watch_elapsed_sec is wall-clock and legitimately differs between the two
    # separate watch_job() calls this test makes; everything else -- the shape
    # and content of the payload the CLI would print -- must be identical.
    assert actual.keys() == expected.keys()
    assert isinstance(actual["watch_elapsed_sec"], float)
    for key in expected:
        if key == "watch_elapsed_sec":
            continue
        assert actual[key] == expected[key], key


def test_default_watch_off_contract_exits_one(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "job-1")
    control.store.mark_finished(job.job_id, "failed")
    control.store.mark_finalization_completed(job.job_id)
    args = _parse_watch([job.job_id, "--poll-interval-sec", "0", "--timeout-sec", "5"])

    import agent_control_plane.app.runtime.cli as cli_module

    original = cli_module._print_json
    cli_module._print_json = lambda value: None
    try:
        rc = _handle_watch_command(control, args)
    finally:
        cli_module._print_json = original

    assert rc == 1


def test_default_watch_missing_job_exits_four(tmp_path: Path) -> None:
    control = AgentControlPlane(_config(tmp_path))
    args = _parse_watch(["no-such-job", "--poll-interval-sec", "0", "--timeout-sec", "1"])

    rc = _handle_watch_command(control, args)

    assert rc == 4


def test_watch_parser_supports_multiple_job_ids_and_selectors() -> None:
    args = _parse_watch(["job-1", "job-2", "--events", "--plan", "plan-a", "--task-glob", "task-*"])

    assert args.job_id == ["job-1", "job-2"]
    assert args.events is True
    assert args.plan_id == "plan-a"
    assert args.task_id_glob == "task-*"


def test_watch_without_events_rejects_multiple_job_ids() -> None:
    args = _parse_watch(["job-1", "job-2"])

    rc = _handle_watch_command(AgentControlPlaneStub(), args)  # type: ignore[arg-type]

    assert rc == 2


class AgentControlPlaneStub:
    """Never reached: argument validation must fail before touching the control plane."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected access to control.{name} during usage-error validation")


def test_statuses_payload_matches_terminal_statuses_and_contract_capability() -> None:
    payload = _statuses_payload()

    assert set(payload["on_contract_capable_statuses"]) == {"completed", "partial", "blocked"}
    assert "contract_mismatch" in payload["always_off_contract_statuses"]
    assert "inefficient_tool_usage" in payload["always_off_contract_statuses"]
    assert "stopped_dirty_after_failure" in payload["always_off_contract_statuses"]
    assert set(payload["terminal_statuses"]) == set(payload["on_contract_capable_statuses"]) | set(
        payload["always_off_contract_statuses"]
    )


def test_print_statuses_json_matches_payload(capsys: pytest.CaptureFixture[str]) -> None:
    _print_statuses(json_output=True)

    out = capsys.readouterr().out
    assert json.loads(out) == _statuses_payload()


@pytest.mark.parametrize("kind", ["activity", "activity_status", "agent_idle", "agent_resumed"])
def test_agy_activity_messages_are_rendered(kind: str) -> None:
    event = WatchEvent(
        kind=kind,
        job_id="job",
        task_id="task",
        status="running",
        expected_result_status="completed",
        finalization_status="not_started",
        message="Model activity\nnext line " + "x" * 500,
    )
    out = io.StringIO()
    _write_event_line(out, event)
    assert "message=Model activity" in out.getvalue()
    assert len(out.getvalue().splitlines()) == 1
    assert len(out.getvalue()) < 450


def test_agy_summary_and_compact_summary_preserve_bounded_activity(tmp_path: Path) -> None:
    from agent_control_plane.shared.agy_activity import agy_activity_paths

    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "activity-summary")
    control.store.update_job(job.job_id, log_path=tmp_path / "attempt.log")
    snapshot, _ = agy_activity_paths(tmp_path / "attempt.log")
    snapshot.write_text(
        json.dumps(
            {
                "schema": 1,
                "job_id": job.job_id,
                "attempt_ref": "attempt",
                "seq": 2,
                "last_event_at": "2026-10-26T00:00:00+00:00",
                "last_progress_at": "2026-10-26T00:00:00+00:00",
                "current_action": "Tool activity",
                "current_tool_name": "read_file",
                "current_state": "active",
                "current_step_index": 2,
                "native_step_index": 114,
                "native_step_count": 115,
                "action_started_at": "2026-10-26T00:00:00+00:00",
                "event_count": 2,
                "tool_calls": 0,
                "model_responses": 1,
                "usage": {"available": True, "thinking_tokens": 42},
                "stream_state": "running",
                "conversation_id": None,
            }
        )
    )
    for summary in (control.summary_job(job.job_id), control.mcp_summary_job(job.job_id)):
        activity = summary["agy_activity"]
        assert activity["available"] is True
        assert activity["current_action"] == "Tool activity"
        assert activity["current_tool_name"] == "read_file"
        assert activity["current_state"] == "active"
        assert activity["current_step_index"] == 2
        assert activity["native_step_index"] == 114
        assert activity["native_step_count"] == 115
        assert activity["usage"] == {"available": True, "thinking_tokens": 42}
        assert activity["progress_age_sec"] >= 0
        assert activity["action_age_sec"] >= 0
        assert len(json.dumps(activity)) < 1500
    terminal_activity = json.loads(snapshot.read_text())
    terminal_activity.update(
        current_action="AGY stream closed",
        current_tool_name=None,
        current_state="done",
        current_step_index=None,
    )
    for state in ("closed", "error"):
        terminal_activity["stream_state"] = state
        snapshot.write_text(json.dumps(terminal_activity))
        assert control.summary_job(job.job_id)["agy_activity"]["progress_state"] == state
        assert control.mcp_summary_job(job.job_id)["agy_activity"]["progress_state"] == state
    snapshot.write_text("{}")
    activity = control.summary_job(job.job_id)["agy_activity"]
    assert activity["available"] is False
    assert activity["progress_state"] == "unknown"
    codex = _create_job(control.store, tmp_path, "codex-summary", backend="codex")
    assert "agy_activity" not in control.summary_job(codex.job_id)


def test_terminal_agy_mcp_and_cli_drain_backlog_without_counting_terminal_twice(
    tmp_path: Path,
) -> None:
    from agent_control_plane.shared.agy_activity import agy_activity_paths

    control = AgentControlPlane(_config(tmp_path))
    job = _create_job(control.store, tmp_path, "terminal-activity")
    log_path = tmp_path / "terminal.log"
    control.store.update_job(job.job_id, log_path=log_path)
    _, events_path = agy_activity_paths(log_path)
    events_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "job_id": job.job_id,
                    "attempt_ref": log_path.stem,
                    "seq": seq,
                    "at": "2026-10-26T00:00:00+00:00",
                    "kind": "model",
                    "summary": "Model activity",
                }
            )
            for seq in range(1, 46)
        )
        + "\n"
    )
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)
    cursor = None
    delivered: list[int] = []
    on_contract = 0
    for expected_done in (False, False, True):
        snapshot = control.watch_events(job_ids=[job.job_id], cursor=cursor)
        cursor = snapshot["cursor"]
        assert snapshot["done"] is expected_done
        assert snapshot["pending"] == (
            []
            if expected_done
            else [
                {
                    "job_id": job.job_id,
                    "status": "completed",
                    "activity_pending": True,
                }
            ]
        )
        for event in snapshot["events"]:
            on_contract += event["on_contract"] is True
            if event["activity"]:
                delivered.append(event["activity"]["seq"])
    assert on_contract == 1
    assert delivered == list(range(1, 46))
    out = io.StringIO()
    fake = _FakeMonotonic()
    args = _parse_watch([job.job_id, "--events", "--poll-interval-sec", "1", "--timeout-sec", "5"])
    assert _handle_watch_events(control, args, out=out, clock=fake.clock, sleep=fake.sleep) == 0
    lines = out.getvalue().splitlines()
    assert sum(" ACTIVITY " in line for line in lines) == 45
    assert sum(" TERMINAL " in line for line in lines) == 1
    assert "jobs=1 on_contract=1 off_contract=0 non_terminal=0 timed_out=false" in lines[-1]
