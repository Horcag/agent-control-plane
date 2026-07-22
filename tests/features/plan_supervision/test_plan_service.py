from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.plan import PlanExecutionSpec, PlanStore, PlanTaskDefinition
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.features.plan_supervision import PlanService


def test_plan_service_dispatches_a_claim_through_its_job_launcher(tmp_path: Path) -> None:
    launched: list[str] = []
    database_path = tmp_path / "jobs.sqlite3"
    service = PlanService(
        coordination_root=tmp_path / ".agent-work",
        job_store=JobStore(database_path),
        plan_store=PlanStore(database_path),
        review_inbox=ReviewInboxStore(database_path),
        launch=lambda claim: _launched_job(claim.dispatch_task_id, launched),
        cancel_job=lambda _job_id: None,
        accept_handoff=lambda *_args, **_kwargs: {},
        verify_continuation_handoff=lambda *_args, **_kwargs: {},
        reconcile_jobs=lambda _job_id=None: {},
        process_is_alive=lambda _pid: False,
        policy_error=RuntimeError,
    )
    service.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="app", brief="Implement the task."),
            ),
        ),
    )

    result = service.dispatch_plan("dispatch")

    dispatch = result["dispatched"]
    dispatch_task_id = dispatch[0]["dispatch_task_id"]
    assert dispatch_task_id.startswith("plan-dispatch-task-a1-")
    assert launched == [dispatch_task_id]
    assert dispatch == [
        {
            "task_id": "task",
            "dispatch_task_id": dispatch_task_id,
            "attempt_no": 1,
            "job_id": f"job-{dispatch_task_id}",
            "status": "queued",
            "inherited_base": None,
        }
    ]
    assert (tmp_path / ".agent-work" / "tasks" / dispatch_task_id / "brief.md").read_text(
        encoding="utf-8"
    ) == "Implement the task.\n"


def _launched_job(task_id: str, launched: list[str]) -> SimpleNamespace:
    launched.append(task_id)
    return SimpleNamespace(job_id=f"job-{task_id}", status="queued")
