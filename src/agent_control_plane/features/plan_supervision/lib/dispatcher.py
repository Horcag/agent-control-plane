from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agent_control_plane.entities.plan import PlanDispatchClaim, PlanStore


class DispatchedPlanJob(Protocol):
    @property
    def job_id(self) -> str: ...

    @property
    def status(self) -> str: ...


class PlanDispatcher:
    """Atomically claim ready plan tasks and launch each claim exactly once."""

    def __init__(
        self,
        *,
        plan_store: PlanStore,
        coordination_root: Path,
        launch: Callable[[PlanDispatchClaim], DispatchedPlanJob],
        process_is_alive: Callable[[int], bool],
    ) -> None:
        self.plan_store = plan_store
        self.coordination_root = coordination_root
        self.launch = launch
        self.process_is_alive = process_is_alive

    def dispatch(self, plan_id: str, *, max_jobs: int = 1) -> dict[str, Any]:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        reconciled_dispatches = self.plan_store.reconcile_orphaned_dispatches(
            plan_id,
            process_is_alive=self.process_is_alive,
        )
        claims = self.plan_store.claim_ready_tasks(
            plan_id,
            owner_pid=os.getpid(),
            limit=max_jobs,
        )
        dispatched: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for claim in claims:
            try:
                self._materialize_brief(claim.dispatch_task_id, claim.execution.brief)
                job = self.launch(claim)
            except Exception as exc:  # noqa: BLE001 - durable dispatch failure boundary
                message = str(exc)
                self.plan_store.mark_dispatch_failed(
                    plan_id,
                    claim.task_id,
                    dispatch_token=claim.dispatch_token,
                    error=message,
                )
                failures.append(
                    {
                        "task_id": claim.task_id,
                        "dispatch_task_id": claim.dispatch_task_id,
                        "attempt_no": claim.attempt_no,
                        "error": message,
                    }
                )
                continue
            dispatched.append(
                {
                    "task_id": claim.task_id,
                    "dispatch_task_id": claim.dispatch_task_id,
                    "attempt_no": claim.attempt_no,
                    "job_id": job.job_id,
                    "status": job.status,
                }
            )
        return {
            "plan_id": plan_id,
            "reconciled_dispatches": reconciled_dispatches,
            "claimed": len(claims),
            "dispatched": dispatched,
            "failures": failures,
            "snapshot": self.plan_store.snapshot(plan_id),
        }

    def _materialize_brief(self, task_id: str, brief: str) -> Path:
        brief_path = self.coordination_root / "tasks" / task_id / "brief.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief.rstrip() + "\n", encoding="utf-8")
        return brief_path
