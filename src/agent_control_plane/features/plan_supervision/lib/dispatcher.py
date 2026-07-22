from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.plan import PlanDispatchClaim, PlanStore
from agent_control_plane.features.plan_supervision.lib.retry_fingerprint import (
    circuit_breaker_state,
    fingerprint_from_spec,
)


class DispatchedPlanJob(Protocol):
    @property
    def job_id(self) -> str: ...

    @property
    def status(self) -> str: ...


class CheckoutSlot(Protocol):
    def __call__(self, name: str, *, branch: str, start_point: str | None = None) -> Any: ...


class PlanDispatcher:
    """Atomically claim ready plan tasks and launch each claim exactly once."""

    def __init__(
        self,
        *,
        plan_store: PlanStore,
        job_store: JobStore,
        coordination_root: Path,
        launch: Callable[[PlanDispatchClaim], DispatchedPlanJob],
        process_is_alive: Callable[[int], bool],
        checkout_slot: CheckoutSlot | None = None,
    ) -> None:
        self.plan_store = plan_store
        self.job_store = job_store
        self.coordination_root = coordination_root
        self.launch = launch
        self.process_is_alive = process_is_alive
        self.checkout_slot = checkout_slot

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
            needs_revision, escalated_fingerprint = circuit_breaker_state(
                self.plan_store, self.job_store, plan_id, claim.task_id
            )
            if needs_revision and fingerprint_from_spec(claim.execution) == escalated_fingerprint:
                message = (
                    f"Plan task {plan_id}/{claim.task_id} needs a strategy revision after "
                    "repeated identical failures; auto-dispatch is blocked"
                )
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
            inherited_base: str | None = None
            try:
                inherited_base = self._auto_position_slot(plan_id, claim)
                if inherited_base is not None:
                    claim = dataclasses.replace(
                        claim,
                        execution=dataclasses.replace(
                            claim.execution, expected_base_sha=inherited_base
                        ),
                    )
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
                        "inherited_base": inherited_base,
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
                    "inherited_base": inherited_base,
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

    def _auto_position_slot(self, plan_id: str, claim: PlanDispatchClaim) -> str | None:
        """Position a dependent task's slot at its single accepted dependency's sha.

        Returns the inherited base sha when positioning happened, else None. Skips when
        positioning would be ambiguous (no slot, an operator-provided base, or anything
        other than exactly one accepted dependency) or when no checkout_slot was wired.
        """
        if self.checkout_slot is None:
            return None
        if claim.execution.slot is None or claim.execution.expected_base_sha is not None:
            return None
        bases = self.plan_store.dependency_accepted_shas(plan_id, claim.task_id)
        if len(bases) != 1:
            return None
        branch = f"plan-base/{claim.dispatch_task_id}"
        self.checkout_slot(claim.execution.slot, branch=branch, start_point=bases[0])
        return bases[0]

    def _materialize_brief(self, task_id: str, brief: str) -> Path:
        brief_path = self.coordination_root / "tasks" / task_id / "brief.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief.rstrip() + "\n", encoding="utf-8")
        return brief_path
