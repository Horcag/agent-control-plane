from __future__ import annotations

from pathlib import Path

from agent_control_plane.features.agent_runner import (
    WorkerLease,
    WorkerLeaseState,
    probe_worker_lease,
)


def test_worker_lease_distinguishes_live_released_and_foreign_instances(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    assert probe_worker_lease(run_dir, "worker-a").state is WorkerLeaseState.MISSING

    lease = WorkerLease(run_dir, "worker-a")
    lease.acquire()
    try:
        assert probe_worker_lease(run_dir, "worker-a").state is WorkerLeaseState.HELD_MATCH
        assert probe_worker_lease(run_dir, "worker-b").state is WorkerLeaseState.HELD_MISMATCH
    finally:
        lease.release()

    assert probe_worker_lease(run_dir, "worker-a").state is WorkerLeaseState.RELEASED_MATCH
