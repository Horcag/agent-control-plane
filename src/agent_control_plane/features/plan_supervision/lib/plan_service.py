from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.entities.plan import (
    PlanDispatchClaim,
    PlanExecutionSpec,
    PlanStore,
    PlanTaskDefinition,
)
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.features.plan_supervision.lib.dispatcher import PlanDispatcher
from agent_control_plane.features.plan_supervision.lib.supervisor import (
    PlanRunOptions,
    PlanSupervisor,
)
from agent_control_plane.shared.verification_report import inspect_verification_report


class PlanService:
    """Own the durable plan lifecycle while the control plane stays its public facade."""

    def __init__(
        self,
        *,
        coordination_root: Path,
        job_store: JobStore,
        plan_store: PlanStore,
        review_inbox: ReviewInboxStore,
        launch: Callable[[PlanDispatchClaim], JobRecord],
        cancel_job: Callable[[str], Any],
        accept_handoff: Callable[..., dict[str, Any]],
        verify_continuation_handoff: Callable[..., dict[str, Any]],
        reconcile_jobs: Callable[[str | None], dict[str, Any]],
        process_is_alive: Callable[[int], bool],
        policy_error: type[RuntimeError],
    ) -> None:
        self._coordination_root = coordination_root
        self._job_store = job_store
        self._plan_store = plan_store
        self._review_inbox = review_inbox
        self._launch = launch
        self._cancel_job = cancel_job
        self._accept_handoff = accept_handoff
        self._verify_continuation_handoff = verify_continuation_handoff
        self._reconcile_jobs = reconcile_jobs
        self._process_is_alive = process_is_alive
        self._policy_error = policy_error

    def create_plan(
        self,
        *,
        plan_id: str,
        title: str,
        objective: str = "",
        tasks: tuple[PlanTaskDefinition, ...] = (),
    ) -> dict[str, Any]:
        self._job_store.initialize()
        self._plan_store.create_plan(plan_id=plan_id, title=title, objective=objective, tasks=tasks)
        return self._plan_store.snapshot(plan_id)

    def add_plan_task(
        self,
        plan_id: str,
        *,
        task_id: str,
        title: str,
        depends_on: tuple[str, ...] = (),
        execution: PlanExecutionSpec | None = None,
    ) -> dict[str, Any]:
        self._job_store.initialize()
        self._plan_store.add_task(
            plan_id,
            PlanTaskDefinition(task_id, title, depends_on=depends_on, execution=execution),
        )
        return self._plan_store.snapshot(plan_id)

    def bind_plan_job(self, plan_id: str, task_id: str, job_id: str) -> dict[str, Any]:
        self._plan_store.bind_job(plan_id, task_id, job_id)
        return self._plan_store.snapshot(plan_id)

    def accept_plan_task(
        self, plan_id: str, task_id: str, *, accepted_sha: str | None = None
    ) -> dict[str, Any]:
        self._validate_plan_task_acceptance(plan_id, task_id)
        cursor = self._plan_store.snapshot(plan_id)["cursor"]
        self._plan_store.accept_task(plan_id, task_id, accepted_sha=accepted_sha)
        return self._plan_store.snapshot(plan_id, since=cursor)

    def accept_handoff(self, plan_id: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self._validate_plan_task_acceptance(plan_id, task_id)
        return self._accept_handoff(plan_id, task_id, **kwargs)

    def verify_continuation_handoff(
        self, plan_id: str, task_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self._verify_continuation_handoff(plan_id, task_id, **kwargs)

    def reject_plan_task(self, plan_id: str, task_id: str) -> dict[str, Any]:
        cursor = self._plan_store.snapshot(plan_id)["cursor"]
        self._plan_store.reject_task(plan_id, task_id)
        return self._plan_store.snapshot(plan_id, since=cursor)

    def dispatch_plan(self, plan_id: str, *, max_jobs: int = 1) -> dict[str, Any]:
        return PlanDispatcher(
            plan_store=self._plan_store,
            coordination_root=self._coordination_root,
            launch=self._launch,
            process_is_alive=self._process_is_alive,
        ).dispatch(plan_id, max_jobs=max_jobs)

    def retry_plan_task(
        self, plan_id: str, task_id: str, *, brief_override: str | None = None
    ) -> dict[str, Any]:
        retried = self._plan_store.retry_task(plan_id, task_id, brief_override=brief_override)
        return {"task": retried, "snapshot": self._plan_store.snapshot(plan_id)}

    def cancel_plan(self, plan_id: str) -> dict[str, Any]:
        cancellation = self._plan_store.request_cancel(plan_id)
        cancelled_jobs: list[str] = []
        failures: list[dict[str, str]] = []
        for job_id in cancellation["active_job_ids"]:
            try:
                self._cancel_job(job_id)
            except (KeyError, ValueError) as exc:
                failures.append({"job_id": job_id, "error": str(exc)})
            else:
                cancelled_jobs.append(job_id)
        return {
            **cancellation,
            "cancelled_jobs": cancelled_jobs,
            "failures": failures,
            "snapshot": self._plan_store.snapshot(plan_id),
        }

    def archive_plan(self, plan_id: str) -> dict[str, Any]:
        self._plan_store.archive_plan(plan_id)
        return self._plan_store.snapshot(plan_id)

    def plan_snapshot(
        self,
        plan_id: str,
        *,
        since: int | None = None,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        return self._plan_store.snapshot(
            plan_id,
            since=since,
            event_limit=event_limit,
            item_limit=item_limit,
        )

    def watch_plan(
        self,
        plan_id: str,
        *,
        since: int,
        poll_interval_sec: float = 5.0,
        timeout_sec: float | None = 25.0,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        if poll_interval_sec < 0:
            raise ValueError("poll_interval_sec must be non-negative")
        if timeout_sec is not None and timeout_sec < 0:
            raise ValueError("timeout_sec must be non-negative")
        if poll_interval_sec == 0 and timeout_sec is None:
            raise ValueError("poll_interval_sec=0 requires a timeout_sec")
        started = time.monotonic()
        while True:
            snapshot = self.plan_snapshot(
                plan_id, since=since, event_limit=event_limit, item_limit=item_limit
            )
            elapsed = time.monotonic() - started
            if snapshot["changes"] or snapshot["status"] in {"completed", "cancelled"}:
                snapshot["timed_out"] = False
                snapshot["watch_elapsed_sec"] = round(elapsed, 3)
                return snapshot
            if timeout_sec is not None and elapsed >= timeout_sec:
                snapshot["timed_out"] = True
                snapshot["watch_elapsed_sec"] = round(elapsed, 3)
                return snapshot
            sleep_for = poll_interval_sec
            if timeout_sec is not None:
                sleep_for = min(sleep_for, max(0.0, timeout_sec - elapsed))
            if sleep_for > 0:
                time.sleep(sleep_for)

    def run_plan_until_review(
        self,
        plan_id: str,
        *,
        max_jobs: int = 1,
        poll_interval_sec: float = 5.0,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        return PlanSupervisor(self).run_until_review(
            plan_id,
            PlanRunOptions(
                max_jobs=max_jobs,
                poll_interval_sec=poll_interval_sec,
                timeout_sec=timeout_sec,
            ),
        )

    def list_plans(
        self, limit: int = 20, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        return self._plan_store.list_plans(limit, include_archived=include_archived)

    def reconcile_jobs(self, job_id: str | None = None) -> dict[str, Any]:
        return self._reconcile_jobs(job_id)

    def _validate_plan_task_acceptance(self, plan_id: str, task_id: str) -> dict[str, Any]:
        target = self._plan_store.review_target(plan_id, task_id)
        result_path = target.get("result_path")
        verification = (
            inspect_verification_report(Path(result_path), expected_status=target.get("job_status"))
            if result_path
            else None
        )
        if verification is None or verification.state != "valid":
            detail = verification.error if verification is not None else "result path is missing"
            state = verification.state if verification else "missing"
            raise self._policy_error(
                f"Plan task verification is not valid for acceptance: {plan_id}/{task_id}: {state}"
                + (f" ({detail})" if detail else "")
            )
        try:
            inbox_item = self._review_inbox.get(f"agent_job:{target['job_id']}")
        except KeyError:
            inbox_item = None
        if inbox_item is not None and (
            inbox_item.verification_state != "valid"
            or not isinstance(inbox_item.verification_bundle, dict)
            or inbox_item.verification_bundle.get("review_ready") is not True
        ):
            raise self._policy_error(
                f"Plan task handoff is not review-ready for acceptance: {plan_id}/{task_id}"
            )
        return target
