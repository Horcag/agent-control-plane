from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


class PlanSupervisorGateway(Protocol):
    """Narrow runtime surface needed by the foreground plan supervisor."""

    def plan_snapshot(self, plan_id: str) -> dict[str, Any]: ...

    def dispatch_plan(self, plan_id: str, *, max_jobs: int = 1) -> dict[str, Any]: ...

    def watch_plan(
        self,
        plan_id: str,
        *,
        since: int,
        poll_interval_sec: float,
        timeout_sec: float | None,
    ) -> dict[str, Any]: ...

    def reconcile_jobs(self, job_id: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PlanRunOptions:
    max_jobs: int = 1
    poll_interval_sec: float = 5.0
    timeout_sec: float | None = None


class PlanSupervisor:
    """Resume a durable plan until human review or another safe stop boundary."""

    def __init__(self, gateway: PlanSupervisorGateway) -> None:
        self._gateway = gateway

    def run_until_review(
        self,
        plan_id: str,
        options: PlanRunOptions | None = None,
    ) -> dict[str, Any]:
        selected = options or PlanRunOptions()
        _validate_options(selected)
        started = time.monotonic()
        snapshot = self._gateway.plan_snapshot(plan_id)
        dispatch_passes = 0
        jobs_dispatched = 0
        reconciliations = 0
        iterations = 0
        last_dispatch: dict[str, Any] | None = None
        last_reconciliation: dict[str, Any] | None = None

        def result(reason: str) -> dict[str, Any]:
            return {
                "plan_id": plan_id,
                "mode": "until_review",
                "reason": reason,
                "requires_root_review": reason
                in {
                    "review_required",
                    "blocked",
                    "dispatch_failed",
                    "manual_dispatch_required",
                    "stalled",
                },
                "iterations": iterations,
                "dispatch_passes": dispatch_passes,
                "jobs_dispatched": jobs_dispatched,
                "reconciliations": reconciliations,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "last_dispatch": last_dispatch,
                "last_reconciliation": last_reconciliation,
                "snapshot": snapshot,
            }

        while True:
            iterations += 1
            stop_reason = _stop_reason(snapshot)
            if stop_reason is not None:
                return result(stop_reason)

            remaining = _remaining_timeout(started, selected.timeout_sec)
            if remaining is not None and remaining <= 0:
                return result("timed_out")

            if snapshot.get("ready_next"):
                last_dispatch = self._gateway.dispatch_plan(
                    plan_id,
                    max_jobs=selected.max_jobs,
                )
                dispatch_passes += 1
                dispatched = last_dispatch.get("dispatched") or []
                jobs_dispatched += len(dispatched)
                next_snapshot = last_dispatch.get("snapshot")
                snapshot = (
                    next_snapshot
                    if isinstance(next_snapshot, dict)
                    else self._gateway.plan_snapshot(plan_id)
                )
                if last_dispatch.get("failures"):
                    return result("dispatch_failed")
                if dispatched or snapshot.get("running"):
                    continue
                if snapshot.get("ready_next"):
                    return result("manual_dispatch_required")
                continue

            running = snapshot.get("running") or []
            if running:
                cursor = int(snapshot["cursor"])
                watch_timeout = selected.poll_interval_sec
                if remaining is not None:
                    watch_timeout = min(watch_timeout, remaining)
                snapshot = self._gateway.watch_plan(
                    plan_id,
                    since=cursor,
                    poll_interval_sec=min(selected.poll_interval_sec, watch_timeout),
                    timeout_sec=watch_timeout,
                )
                if _stop_reason(snapshot) is not None:
                    continue
                job_ids = _running_job_ids(snapshot)
                for job_id in job_ids:
                    last_reconciliation = self._gateway.reconcile_jobs(job_id)
                    reconciliations += 1
                if job_ids:
                    snapshot = self._gateway.plan_snapshot(plan_id)
                continue

            return result("stalled")


def _validate_options(options: PlanRunOptions) -> None:
    if options.max_jobs <= 0:
        raise ValueError("max_jobs must be positive")
    if options.poll_interval_sec <= 0:
        raise ValueError("poll_interval_sec must be positive")
    if options.timeout_sec is not None and options.timeout_sec < 0:
        raise ValueError("timeout_sec must be non-negative")


def _remaining_timeout(started: float, timeout_sec: float | None) -> float | None:
    if timeout_sec is None:
        return None
    return max(0.0, timeout_sec - (time.monotonic() - started))


def _stop_reason(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("archived_at") is not None:
        return "archived"
    if snapshot.get("status") in {"cancelling", "cancelled"}:
        return "cancelled"
    if snapshot.get("status") == "completed":
        return "completed"
    if snapshot.get("awaiting_review"):
        return "review_required"
    if snapshot.get("blocked"):
        return "blocked"
    return None


def _running_job_ids(snapshot: dict[str, Any]) -> tuple[str, ...]:
    job_ids = {str(task["job_id"]) for task in snapshot.get("running") or [] if task.get("job_id")}
    return tuple(sorted(job_ids))
