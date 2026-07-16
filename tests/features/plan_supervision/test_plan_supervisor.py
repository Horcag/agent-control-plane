from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_control_plane.features.plan_supervision import PlanRunOptions, PlanSupervisor


def test_supervisor_dispatches_watches_reconciles_and_stops_before_review() -> None:
    gateway = _ScriptedGateway(_snapshot(ready=True, cursor=1))
    gateway.dispatch_snapshot = _snapshot(running=True, cursor=2)
    gateway.watch_snapshot = _snapshot(running=True, cursor=2)
    gateway.reconciled_snapshot = _snapshot(awaiting_review=True, cursor=3)

    result = PlanSupervisor(gateway).run_until_review(
        "transfer",
        PlanRunOptions(max_jobs=2, poll_interval_sec=1, timeout_sec=30),
    )

    assert result["reason"] == "review_required"
    assert result["dispatch_passes"] == 1
    assert result["jobs_dispatched"] == 1
    assert result["snapshot"]["awaiting_review"][0]["task_id"] == "schema"
    assert gateway.calls == [
        ("snapshot", "transfer"),
        ("dispatch", "transfer", 2),
        ("watch", "transfer", 2, 1, 1),
        ("reconcile", "schema-job"),
        ("snapshot", "transfer"),
    ]


def test_supervisor_stops_immediately_when_root_review_is_already_required() -> None:
    gateway = _ScriptedGateway(_snapshot(awaiting_review=True, cursor=7))

    result = PlanSupervisor(gateway).run_until_review("transfer")

    assert result["reason"] == "review_required"
    assert result["dispatch_passes"] == 0
    assert gateway.calls == [("snapshot", "transfer")]


def test_supervisor_never_retries_a_blocked_task() -> None:
    gateway = _ScriptedGateway(_snapshot(blocked=True, cursor=4))

    result = PlanSupervisor(gateway).run_until_review("transfer")

    assert result["reason"] == "blocked"
    assert result["snapshot"]["blocked"][0]["state"] == "failed"
    assert gateway.calls == [("snapshot", "transfer")]


def test_supervisor_stops_when_plan_was_cancelled() -> None:
    snapshot = _snapshot(cursor=8)
    snapshot["status"] = "cancelled"
    gateway = _ScriptedGateway(snapshot)

    result = PlanSupervisor(gateway).run_until_review("transfer")

    assert result["reason"] == "cancelled"
    assert result["requires_root_review"] is False
    assert gateway.calls == [("snapshot", "transfer")]


def test_supervisor_reports_manual_ready_task_instead_of_busy_looping() -> None:
    gateway = _ScriptedGateway(_snapshot(ready=True, cursor=5))
    gateway.dispatch_snapshot = _snapshot(ready=True, cursor=5)
    gateway.dispatch_claimed = 0

    result = PlanSupervisor(gateway).run_until_review("transfer")

    assert result["reason"] == "manual_dispatch_required"
    assert gateway.calls == [
        ("snapshot", "transfer"),
        ("dispatch", "transfer", 1),
    ]


def test_supervisor_requires_a_positive_poll_interval() -> None:
    gateway = _ScriptedGateway(_snapshot(running=True, cursor=1))

    with pytest.raises(ValueError, match="poll_interval_sec must be positive"):
        PlanSupervisor(gateway).run_until_review(
            "transfer",
            PlanRunOptions(poll_interval_sec=0),
        )


class _ScriptedGateway:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.current = snapshot
        self.dispatch_snapshot = snapshot
        self.watch_snapshot = snapshot
        self.reconciled_snapshot = snapshot
        self.dispatch_claimed = 1
        self.calls: list[tuple[Any, ...]] = []

    def plan_snapshot(self, plan_id: str) -> dict[str, Any]:
        self.calls.append(("snapshot", plan_id))
        return deepcopy(self.current)

    def dispatch_plan(self, plan_id: str, *, max_jobs: int = 1) -> dict[str, Any]:
        self.calls.append(("dispatch", plan_id, max_jobs))
        self.current = self.dispatch_snapshot
        dispatched = (
            [{"task_id": "schema", "job_id": "schema-job"}] if self.dispatch_claimed else []
        )
        return {
            "plan_id": plan_id,
            "claimed": self.dispatch_claimed,
            "dispatched": dispatched,
            "failures": [],
            "snapshot": deepcopy(self.current),
        }

    def watch_plan(
        self,
        plan_id: str,
        *,
        since: int,
        poll_interval_sec: float,
        timeout_sec: float | None,
    ) -> dict[str, Any]:
        self.calls.append(("watch", plan_id, since, poll_interval_sec, timeout_sec))
        self.current = self.watch_snapshot
        return deepcopy(self.current)

    def reconcile_jobs(self, job_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("reconcile", job_id))
        self.current = self.reconciled_snapshot
        return {"job_id": job_id}


def _snapshot(
    *,
    cursor: int,
    ready: bool = False,
    running: bool = False,
    awaiting_review: bool = False,
    blocked: bool = False,
    completed: bool = False,
) -> dict[str, Any]:
    task = {
        "task_id": "schema",
        "job_id": "schema-job" if running or awaiting_review or blocked else None,
        "state": (
            "running"
            if running
            else "awaiting_review"
            if awaiting_review
            else "failed"
            if blocked
            else "ready"
        ),
    }
    return {
        "plan_id": "transfer",
        "status": "completed" if completed else "active",
        "cursor": cursor,
        "ready_next": [task] if ready else [],
        "running": [task] if running else [],
        "awaiting_review": [task] if awaiting_review else [],
        "requires_root_decision": [task] if awaiting_review else [],
        "blocked": [task] if blocked else [],
    }
