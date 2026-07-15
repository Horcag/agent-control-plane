from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.features.agent_runner.lib.worker_lease import (
    WorkerLeaseError,
    WorkerLeaseState,
    probe_worker_lease,
)


class WorkerRecoveryState(StrEnum):
    LIVE = "live"
    ORPHANED = "orphaned"
    IDENTITY_CONFLICT = "identity_conflict"


class JobReconciler:
    """Replays durable finalization and fences orphaned worker instances."""

    def __init__(
        self,
        *,
        store: JobStore,
        slot_store: SlotStore,
        is_terminal: Callable[[JobRecord], bool],
        finalize: Callable[[str, bool], JobRecord],
        write_orphan_result: Callable[[JobRecord, str], None],
        process_is_alive: Callable[[int], bool],
        stale_after_sec: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if stale_after_sec < 0:
            raise ValueError("stale_after_sec must be non-negative")
        self.store = store
        self.slot_store = slot_store
        self.is_terminal = is_terminal
        self.finalize = finalize
        self.write_orphan_result = write_orphan_result
        self.process_is_alive = process_is_alive
        self.stale_after_sec = stale_after_sec
        self.clock = clock

    def reconcile(self, job_id: str | None = None) -> dict[str, Any]:
        jobs = self._candidates(job_id)
        report: dict[str, list[str]] = {
            "reconciled_orphaned_jobs": [],
            "reconciled_terminal_jobs": [],
            "live_jobs": [],
            "live_runner_conflicts": [],
            "worker_identity_conflicts": [],
            "errors": [],
        }
        for job in jobs:
            try:
                self._reconcile_job(job, report)
            except Exception as exc:  # noqa: BLE001 - one corrupt job must not stop the sweep
                report["errors"].append(f"{job.job_id}: {exc}")
        return report

    def _candidates(self, job_id: str | None) -> list[JobRecord]:
        if job_id is not None:
            return [self.store.get_job(job_id)]
        candidates = {job.job_id: job for job in self.store.reconciliation_candidates()}
        for slot in self.slot_store.list_slots():
            if slot.active_job_id is None or slot.active_job_id in candidates:
                continue
            try:
                candidates[slot.active_job_id] = self.store.get_job(slot.active_job_id)
            except KeyError:
                continue
        return list(candidates.values())

    def _reconcile_job(self, job: JobRecord, report: dict[str, list[str]]) -> None:
        if self.is_terminal(job):
            self._reconcile_terminal(job, report)
            return
        state = self._worker_state(job)
        if state is WorkerRecoveryState.LIVE:
            report["live_jobs"].append(job.job_id)
            return
        if state is WorkerRecoveryState.IDENTITY_CONFLICT:
            report["worker_identity_conflicts"].append(job.job_id)
            return

        live_runner_pids = sorted(
            {
                pid
                for pid in (job.runner_pid, job.agy_pid)
                if pid is not None and self.process_is_alive(pid)
            }
        )
        if live_runner_pids:
            message = (
                f"Worker lease is gone for {job.job_id}, but runner PID(s) "
                f"{', '.join(str(pid) for pid in live_runner_pids)} are still alive; "
                "workspace finalization is quarantined."
            )
            self.store.add_event(job.job_id, "error", message)
            report["live_runner_conflicts"].append(job.job_id)
            return

        message = (
            "Worker process is no longer alive or has no matching live worker lease for "
            f"instance {job.worker_instance_id or '-'} (last PID {job.worker_pid or '-'})."
        )
        self.store.add_event(job.job_id, "error", message)
        self.store.finish_running_attempts(job.job_id, "worker_lost", message=message)
        self.write_orphan_result(job, message)
        terminal_status = "cancelled" if job.status == "cancel_requested" else "worker_error"
        self.store.mark_finished(job.job_id, terminal_status, message)
        finalized = self.finalize(job.job_id, True)
        if finalized.finalization_status != "completed":
            raise RuntimeError(finalized.finalization_error or "finalization remains incomplete")
        report["reconciled_orphaned_jobs"].append(job.job_id)

    def _reconcile_terminal(self, job: JobRecord, report: dict[str, list[str]]) -> None:
        slot_owned = False
        if job.slot_name is not None:
            slot = self.slot_store.get_slot(job.slot_name)
            slot_owned = slot is not None and slot.active_job_id == job.job_id
        if job.finalization_status == "completed" and not slot_owned:
            return
        if job.finalization_status == "completed":
            self.store.prepare_finalization_replay(job.job_id)
        finalized = self.finalize(job.job_id, True)
        if finalized.finalization_status != "completed":
            raise RuntimeError(finalized.finalization_error or "finalization remains incomplete")
        report["reconciled_terminal_jobs"].append(job.job_id)

    def _worker_state(self, job: JobRecord) -> WorkerRecoveryState:
        instance_id = job.worker_instance_id
        if instance_id is None:
            heartbeat = job.worker_heartbeat_at or job.created_at
            if (
                job.worker_pid is None
                and self.clock() - _timestamp(heartbeat) <= self.stale_after_sec
            ):
                return WorkerRecoveryState.LIVE
            if job.worker_pid is not None and self.process_is_alive(job.worker_pid):
                return WorkerRecoveryState.IDENTITY_CONFLICT
            return WorkerRecoveryState.ORPHANED

        try:
            probe = probe_worker_lease(job.run_dir, instance_id)
        except WorkerLeaseError:
            return WorkerRecoveryState.IDENTITY_CONFLICT
        if probe.state is WorkerLeaseState.HELD_MATCH:
            return WorkerRecoveryState.LIVE
        if probe.state is WorkerLeaseState.HELD_MISMATCH:
            return WorkerRecoveryState.IDENTITY_CONFLICT
        heartbeat = job.worker_heartbeat_at or job.created_at
        if job.status == "queued" and self.clock() - _timestamp(heartbeat) <= self.stale_after_sec:
            return WorkerRecoveryState.LIVE
        return WorkerRecoveryState.ORPHANED


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
