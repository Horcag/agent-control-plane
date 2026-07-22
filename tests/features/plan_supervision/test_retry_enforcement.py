from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.entities.plan import PlanExecutionSpec, PlanStore, PlanTaskDefinition
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.features.plan_supervision import PlanService
from agent_control_plane.features.plan_supervision.lib.plan_service import PlanRetryError

BRIEF = "Implement the retry circuit breaker."
SCOPE = ("src/a.py", "src/b.py")
BUDGET = 40


def _service(tmp_path: Path, *, launch=None) -> tuple[PlanService, JobStore, PlanStore]:
    database_path = tmp_path / "jobs.sqlite3"
    job_store = JobStore(database_path)
    plan_store = PlanStore(database_path)
    service = PlanService(
        coordination_root=tmp_path / ".agent-work",
        job_store=job_store,
        plan_store=plan_store,
        review_inbox=ReviewInboxStore(database_path),
        launch=launch or (lambda _claim: pytest.fail("launch should not be called")),
        cancel_job=lambda _job_id: None,
        accept_handoff=lambda *_args, **_kwargs: {},
        verify_continuation_handoff=lambda *_args, **_kwargs: {},
        reconcile_jobs=lambda _job_id=None: {},
        process_is_alive=lambda _pid: False,
        policy_error=RuntimeError,
    )
    return service, job_store, plan_store


def _canonical_scope_json(scope: tuple[str, ...]) -> str:
    return json.dumps(sorted(set(scope)), ensure_ascii=False, separators=(",", ":"))


def _make_job(
    job_store: JobStore,
    tmp_path: Path,
    job_id: str,
    *,
    brief: str = BRIEF,
    effective_scope: tuple[str, ...] = SCOPE,
    tool_call_budget: int | None = BUDGET,
    status: str,
    runner_failure: str | None,
) -> JobRecord:
    job_store.create_job(
        job_id=job_id,
        task_id=job_id,
        route="acp",
        workspace_path=tmp_path / "workspace",
        expected_branch="work/retry-enforcement",
        config_path=tmp_path / "config.toml",
        run_dir=tmp_path / "runs" / job_id,
        prompt_path=tmp_path / "runs" / job_id / "prompt.md",
        result_path=tmp_path / "tasks" / job_id / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        brief_sha256=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        effective_scope_json=_canonical_scope_json(effective_scope),
        codex_tool_call_budget=tool_call_budget,
    )
    job_store.update_job(job_id, status=status, finalization_status="completed")
    job_store.set_runner_failure(job_id, runner_failure)
    return job_store.get_job(job_id)


def _create_task(service: PlanService, plan_id: str, task_id: str = "task") -> None:
    service.create_plan(
        plan_id=plan_id,
        title="Retry enforcement",
        tasks=(
            PlanTaskDefinition(
                task_id,
                "Task",
                execution=PlanExecutionSpec(
                    route="acp",
                    brief=BRIEF,
                    effective_scope=SCOPE,
                    codex_tool_call_budget=BUDGET,
                ),
            ),
        ),
    )


def test_identical_retry_after_tool_call_budget_failure_is_blocked(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-a")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-a", "task", "job-1")

    with pytest.raises(PlanRetryError, match="identical retry after tool_call_budget"):
        service.retry_plan_task("plan-a", "task")


def test_identical_retry_after_inefficient_tool_usage_failure_is_blocked(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-b")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="inefficient_tool_usage",
        runner_failure="inefficient_tool_usage",
    )
    plan_store.bind_job("plan-b", "task", "job-1")

    with pytest.raises(PlanRetryError, match="identical retry after inefficient_tool_usage"):
        service.retry_plan_task("plan-b", "task")


def test_retry_allowed_when_brief_override_changes_fingerprint(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-c")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-c", "task", "job-1")

    result = service.retry_plan_task("plan-c", "task", brief_override="A different approach.")

    assert result["task"]["state"] in {"pending", "ready"}


def test_retry_allowed_when_retry_override_reason_is_set(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-d")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-d", "task", "job-1")

    result = service.retry_plan_task(
        "plan-d", "task", retry_override_reason="Operator confirmed a manual retry."
    )

    assert result["task"]["state"] in {"pending", "ready"}


def test_non_circuit_breaking_failure_stays_freely_retryable(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-e")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="stopped_dirty_after_failure",
        runner_failure="rate_limit",
    )
    plan_store.bind_job("plan-e", "task", "job-1")

    result = service.retry_plan_task("plan-e", "task")

    assert result["task"]["state"] in {"pending", "ready"}


def test_two_identical_circuit_breaking_failures_escalate_to_strategy_revision(
    tmp_path: Path,
) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-f")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-f", "task", "job-1")

    service.retry_plan_task("plan-f", "task", retry_override_reason="First manual retry.")

    _make_job(
        job_store,
        tmp_path,
        "job-2",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-f", "task", "job-2")

    # A plain retry is blocked once escalated.
    with pytest.raises(PlanRetryError, match="strategy revision"):
        service.retry_plan_task("plan-f", "task")

    # The override escape hatch no longer suffices past escalation.
    with pytest.raises(PlanRetryError, match="strategy revision"):
        service.retry_plan_task("plan-f", "task", retry_override_reason="Please, just retry.")

    # It is surfaced in the plan summary.
    snapshot = service.plan_snapshot("plan-f")
    task_summary = next(t for t in snapshot["requires_root_decision"] if t["task_id"] == "task")
    assert task_summary["needs_strategy_revision"] is True

    # Only a fingerprint change re-arms it.
    result = service.retry_plan_task("plan-f", "task", brief_override="A genuinely new strategy.")
    assert result["task"]["state"] in {"pending", "ready"}


def test_awaiting_review_retry_requires_explicit_opt_in(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-h")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="completed",
        runner_failure=None,
    )
    plan_store.bind_job("plan-h", "task", "job-1")

    with pytest.raises(ValueError, match=r"not eligible for retry.*awaiting_review"):
        service.retry_plan_task("plan-h", "task")

    result = service.retry_plan_task("plan-h", "task", allow_awaiting_review=True)

    assert result["task"]["state"] in {"pending", "ready"}


def test_escalated_task_is_never_auto_dispatched(tmp_path: Path) -> None:
    service, job_store, plan_store = _service(tmp_path)
    _create_task(service, "plan-g")
    _make_job(
        job_store,
        tmp_path,
        "job-1",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-g", "task", "job-1")
    service.retry_plan_task("plan-g", "task", retry_override_reason="First manual retry.")
    _make_job(
        job_store,
        tmp_path,
        "job-2",
        status="stopped_dirty_after_failure",
        runner_failure="tool_call_budget",
    )
    plan_store.bind_job("plan-g", "task", "job-2")

    # Simulate the task slipping back to 'ready' outside the service-layer guard.
    plan_store.retry_task("plan-g", "task")

    result = service.dispatch_plan("plan-g")

    assert result["dispatched"] == []
    assert result["claimed"] == 1
    assert len(result["failures"]) == 1
    assert "strategy revision" in result["failures"][0]["error"]
