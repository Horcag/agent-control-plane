from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane, StartOptions
from agent_control_plane.entities.review_inbox import ReviewInboxDraft
from agent_control_plane.features.agent_runner import (
    FinalizationLease,
    ProcessIdentity,
    ProcessTerminationResult,
    ProcessTerminationState,
    WorkerLease,
    WorkerLeaseState,
    capture_process_identity,
    probe_worker_lease,
    supports_verified_process_termination,
    terminate_verified_process,
)
from agent_control_plane.features.agent_runner.lib.codex_process_monitor import (
    terminate_spawned_process,
)
from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunResult
from agent_control_plane.features.result_handoff import (
    clean_checkpointed_workspace,
    create_slot_checkpoint,
)
from agent_control_plane.shared.config import (
    CodexModelCatalogConfig,
    CodexQuotaDomainConfig,
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
)
from agent_control_plane.shared.git_tools import run_git, workspace_state


def test_terminal_transition_persists_replay_intent_atomically(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot))
    job = _active_slot_job(control, tmp_path, slot, "job-atomic")
    control.store.assign_worker(
        job.job_id,
        "worker-atomic",
        worker_pid=123,
        heartbeat_at="2000-01-01T00:00:00+00:00",
    )

    finished = control.store.mark_finished(job.job_id, "completed")

    assert finished.status == "completed"
    assert finished.finalization_status == "pending"
    assert finished.finalization_error is None
    assert finished.worker_instance_id is None
    assert finished.worker_pid is None
    assert finished.runner_pid is None


def test_terminal_transition_is_idempotent_and_preserves_first_outcome(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot))
    job = _active_slot_job(control, tmp_path, slot, "job-finish-once")

    first = control.store.mark_finished(job.job_id, "completed", "first outcome")
    control.store.mark_finalization_completed(job.job_id)
    repeated = control.store.mark_finished(job.job_id, "failed", "stale overwrite")

    assert first.status == "completed"
    assert repeated.status == "completed"
    assert repeated.last_error == "first outcome"
    assert repeated.finalization_status == "completed"


def test_reconcile_respects_single_writer_finalization_lease(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-finalizer-lease")
    (slot / "pending.txt").write_text("do not race cleanup\n", encoding="utf-8")
    control.store.mark_finished(job.job_id, "completed")
    lease = FinalizationLease(job.run_dir, "other-finalizer")
    lease.acquire()
    try:
        blocked = AgentControlPlane(config).reconcile_jobs(job.job_id)
    finally:
        lease.release()

    assert blocked["reconciled_terminal_jobs"] == []
    assert blocked["errors"]
    assert (slot / "pending.txt").exists()
    assert control.slots.inspect_slot("app-1").active_job_id == job.job_id

    recovered = AgentControlPlane(config).reconcile_jobs(job.job_id)
    assert recovered["errors"] == []
    assert recovered["reconciled_terminal_jobs"] == [job.job_id]


def test_reconcile_replays_crash_after_terminal_transition(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-terminal-crash")
    base_sha = run_git(slot, "rev-parse", "HEAD")
    (slot / "worker.txt").write_text("survive restart\n", encoding="utf-8")
    control.store.mark_finished(job.job_id, "completed")

    recovered = AgentControlPlane(config)
    first = recovered.reconcile_jobs()
    item = recovered.review_inbox.get(f"agent_job:{job.job_id}")
    checkpoint_sha = item.checkpoint_sha

    assert job.job_id in first["reconciled_terminal_jobs"]
    assert first["errors"] == []
    assert recovered.store.get_job(job.job_id).finalization_status == "completed"
    assert item.delivery_status == "checkpointed"
    assert item.slot_released is True
    assert run_git(slot, "show", f"{checkpoint_sha}:worker.txt") == "survive restart"
    assert run_git(slot, "rev-parse", "HEAD") == base_sha
    assert workspace_state(slot).porcelain == ""
    assert recovered.slots.inspect_slot("app-1").active_job_id is None

    second = AgentControlPlane(config).reconcile_jobs()
    repeated = recovered.review_inbox.get(f"agent_job:{job.job_id}")
    assert second["reconciled_terminal_jobs"] == []
    assert second["errors"] == []
    assert repeated.checkpoint_sha == checkpoint_sha


def test_reconcile_reuses_checkpoint_after_crash_before_slot_release(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-release-crash")
    base_sha = run_git(slot, "rev-parse", "HEAD")
    (slot / "worker.txt").write_text("checkpoint once\n", encoding="utf-8")
    finished = control.store.mark_finished(job.job_id, "completed")
    checkpoint = create_slot_checkpoint(
        slot,
        job_id=job.job_id,
        task_id=job.task_id,
        terminal_status=finished.status,
        scratch_root=job.run_dir / "checkpoint",
    )
    control.review_inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id=job.job_id,
            source_status=finished.status,
            source_completed_at=finished.finished_at,
            delivery_status="checkpointed",
            task_id=job.task_id,
            route=job.route,
            workspace_path=slot,
            slot_name=job.slot_name,
            result_path=job.result_path,
            checkpoint_ref=checkpoint.ref_name,
            checkpoint_sha=checkpoint.commit_sha,
            checkpoint_tree_sha=checkpoint.tree_sha,
            base_sha=checkpoint.base_sha,
            slot_released=False,
        )
    )
    clean_checkpointed_workspace(slot, checkpoint, scratch_root=job.run_dir / "checkpoint")
    assert control.slots.inspect_slot("app-1").active_job_id == job.job_id

    report = AgentControlPlane(config).reconcile_jobs()
    item = control.review_inbox.get(f"agent_job:{job.job_id}")

    assert report["errors"] == []
    assert item.checkpoint_sha == checkpoint.commit_sha
    assert item.slot_released is True
    assert run_git(slot, "rev-parse", "HEAD") == base_sha
    assert workspace_state(slot).porcelain == ""
    assert control.slots.inspect_slot("app-1").status == "available"


@pytest.mark.skipif(
    not supports_verified_process_termination(),
    reason="OS has no safe exact-process termination primitive",
)
def test_reconcile_replays_exact_process_killed_after_checkpoint_before_cleanup(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-finalization-kill")
    (slot / "worker.txt").write_text("checkpoint once\n", encoding="utf-8")
    control.store.mark_finished(job.job_id, "completed")
    ready_path = tmp_path / "finalization-ready"
    release_path = tmp_path / "finalization-release"
    helper_script = """
import runpy
import sys
import time
from pathlib import Path

test_path, root, route, slot, job_id, ready_path, release_path = sys.argv[1:]
namespace = runpy.run_path(test_path)
config = namespace["_config"](Path(root), Path(route), Path(slot))
from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
import agent_control_plane.app.runtime.finalization_service as finalization_service

original_cleanup = finalization_service.clean_checkpointed_workspace

def block_after_durable_handoff(*args, **kwargs):
    Path(ready_path).write_text("checkpoint and inbox durable", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not Path(release_path).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return original_cleanup(*args, **kwargs)

finalization_service.clean_checkpointed_workspace = block_after_durable_handoff
AgentControlPlane(config).finalization.replay(job_id, allow_inactive=False)
"""
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[3] / "src"
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    finalizer = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-c",
            helper_script,
            str(Path(__file__).resolve()),
            str(tmp_path),
            str(route),
            str(slot),
            job.job_id,
            str(ready_path),
            str(release_path),
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    identity: ProcessIdentity | None = None
    try:
        _wait_for_file(ready_path, finalizer)
        identity = capture_process_identity(finalizer.pid)
        assert identity is not None
        before = control.review_inbox.get(f"agent_job:{job.job_id}")
        assert before.checkpoint_sha is not None
        assert before.slot_released is False
        assert control.slots.inspect_slot("app-1").status == "finalizing"

        terminated = terminate_verified_process(identity)
        finalizer.wait(timeout=5)

        assert terminated.state is ProcessTerminationState.TERMINATED
        report = AgentControlPlane(config).reconcile_jobs(job.job_id)
        after = control.review_inbox.get(f"agent_job:{job.job_id}")
        with sqlite3.connect(config.database_path) as database:
            inbox_rows = database.execute(
                "select count(*) from review_inbox_items where source_kind = ? and source_id = ?",
                ("agent_job", job.job_id),
            ).fetchone()[0]

        assert report["errors"] == []
        assert job.job_id in report["reconciled_terminal_jobs"]
        assert after.checkpoint_sha == before.checkpoint_sha
        assert inbox_rows == 1
        assert after.slot_released is True
        assert control.slots.inspect_slot("app-1").status == "available"
    finally:
        release_path.touch(exist_ok=True)
        if identity is not None and capture_process_identity(identity.pid) == identity:
            terminate_verified_process(identity)
        if finalizer.poll() is None:
            terminate_spawned_process(finalizer)


def test_reconcile_treats_reused_live_pid_without_worker_lease_as_orphan(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-reused-pid")
    control.store.assign_worker(
        job.job_id,
        "worker-gone",
        worker_pid=os.getpid(),
        heartbeat_at="2000-01-01T00:00:00+00:00",
    )

    report = AgentControlPlane(config).reconcile_jobs()
    recovered = control.store.get_job(job.job_id)

    assert job.job_id in report["reconciled_orphaned_jobs"]
    assert report["errors"] == []
    assert recovered.status == "worker_error"
    assert recovered.finalization_status == "completed"
    assert control.slots.inspect_slot("app-1").active_job_id is None


def test_orphan_runner_requires_explicit_verified_termination(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-live-orphan-runner")
    identity = ProcessIdentity(pid=4242, started_key="test:123", executable="python")
    control.store.assign_worker(
        job.job_id,
        "worker-gone",
        worker_pid=1111,
        heartbeat_at="2000-01-01T00:00:00+00:00",
    )
    control.store.update_job(
        job.job_id,
        runner_pid=identity.pid,
        runner_process_identity=identity.to_json(),
    )
    live_pids = {identity.pid}

    def terminate(expected: ProcessIdentity) -> ProcessTerminationResult:
        assert expected == identity
        live_pids.remove(expected.pid)
        return ProcessTerminationResult(
            state=ProcessTerminationState.TERMINATED,
            pid=expected.pid,
            message="terminated exact test process",
        )

    with (
        patch(
            "agent_control_plane.app.runtime.orchestrator.process_is_alive",
            side_effect=lambda pid: pid in live_pids,
        ),
        patch(
            "agent_control_plane.app.runtime.orchestrator.terminate_verified_process",
            side_effect=terminate,
        ) as terminate_mock,
    ):
        quarantined = AgentControlPlane(config).reconcile_jobs(job.job_id)
        recovered = AgentControlPlane(config).reconcile_jobs(
            job.job_id,
            terminate_verified_runners=True,
        )

    assert quarantined["live_runner_conflicts"] == [job.job_id]
    assert terminate_mock.call_count == 1
    assert recovered["terminated_orphan_runners"] == [job.job_id]
    assert recovered["reconciled_orphaned_jobs"] == [job.job_id]
    assert control.store.get_job(job.job_id).status == "worker_error"
    assert control.slots.inspect_slot("app-1").active_job_id is None


@pytest.mark.skipif(
    not supports_verified_process_termination(),
    reason="OS has no safe exact-process termination primitive",
)
def test_reconcile_does_not_terminate_live_process_with_mismatched_durable_identity(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-live-identity-mismatch")
    unrelated = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity: ProcessIdentity | None = None
    try:
        identity = capture_process_identity(unrelated.pid)
        assert identity is not None
        control.store.assign_worker(
            job.job_id,
            "worker-gone",
            worker_pid=unrelated.pid,
            heartbeat_at="2000-01-01T00:00:00+00:00",
        )
        control.store.update_job(
            job.job_id,
            runner_pid=unrelated.pid,
            runner_process_identity=ProcessIdentity(
                pid=identity.pid,
                started_key=identity.started_key + ":mismatch",
                executable=identity.executable,
            ).to_json(),
        )

        report = AgentControlPlane(config).reconcile_jobs(
            job.job_id,
            terminate_verified_runners=True,
        )

        assert report["terminated_orphan_runners"] == []
        assert job.job_id in report["live_runner_conflicts"]
        assert capture_process_identity(unrelated.pid) == identity
        assert control.slots.inspect_slot("app-1").active_job_id == job.job_id
    finally:
        if identity is not None and capture_process_identity(identity.pid) == identity:
            terminate_verified_process(identity)
        if unrelated.poll() is None:
            terminate_spawned_process(unrelated)


@pytest.mark.skipif(
    not supports_verified_process_termination(),
    reason="OS has no safe exact-process termination primitive",
)
def test_reconcile_kills_verified_child_after_coordinator_death(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-coordinator-death")
    ready_path = tmp_path / "coordinator-ready.json"
    helper_path = Path(__file__).parent / "helpers" / "orphan_coordinator.py"
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[3] / "src"
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    coordinator = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            str(helper_path),
            str(config.database_path),
            str(job.run_dir),
            job.job_id,
            "coordinator-e2e",
            str(ready_path),
        ],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_identity: ProcessIdentity | None = None
    coordinator_identity: ProcessIdentity | None = None
    try:
        _wait_for_file(ready_path, coordinator)
        payload = json.loads(ready_path.read_text(encoding="utf-8"))
        coordinator_identity = ProcessIdentity.from_dict(payload["coordinator_identity"])
        child_identity = ProcessIdentity.from_dict(payload["identity"])
        assert capture_process_identity(coordinator_identity.pid) == coordinator_identity
        assert capture_process_identity(child_identity.pid) == child_identity

        coordinator_termination = terminate_verified_process(coordinator_identity)
        coordinator.wait(timeout=5)

        assert coordinator_termination.state is ProcessTerminationState.TERMINATED
        assert coordinator.poll() is not None
        assert capture_process_identity(child_identity.pid) == child_identity
        report = AgentControlPlane(config).reconcile_jobs(
            job.job_id,
            terminate_verified_runners=True,
        )

        assert report["errors"] == []
        assert report["terminated_orphan_runners"] == [job.job_id]
        assert report["reconciled_orphaned_jobs"] == [job.job_id]
        assert capture_process_identity(child_identity.pid) is None
        assert control.store.get_job(job.job_id).status == "worker_error"
        assert control.slots.inspect_slot("app-1").active_job_id is None
    finally:
        if (
            coordinator_identity is not None
            and capture_process_identity(coordinator_identity.pid) == coordinator_identity
        ):
            terminate_verified_process(coordinator_identity)
        if coordinator.poll() is None:
            terminate_spawned_process(coordinator)
        if (
            child_identity is not None
            and capture_process_identity(child_identity.pid) == child_identity
        ):
            terminate_verified_process(child_identity)


def test_reconcile_preserves_live_worker_and_quarantines_foreign_lease(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    live = _active_slot_job(control, tmp_path, slot, "job-live")
    control.store.assign_worker(
        live.job_id,
        "worker-live",
        worker_pid=os.getpid(),
        heartbeat_at="2000-01-01T00:00:00+00:00",
    )
    live_lease = WorkerLease(live.run_dir, "worker-live")
    live_lease.acquire()
    try:
        report = AgentControlPlane(config).reconcile_jobs()
    finally:
        live_lease.release()

    assert live.job_id in report["live_jobs"]
    assert control.store.get_job(live.job_id).status == "queued"
    assert control.slots.inspect_slot("app-1").active_job_id == live.job_id

    control.store.mark_finished(live.job_id, "cancelled")
    control.finish_job(live.job_id, "cancelled")
    foreign = _active_slot_job(control, tmp_path, slot, "job-foreign")
    control.store.assign_worker(
        foreign.job_id,
        "expected-worker",
        worker_pid=os.getpid(),
        heartbeat_at="2000-01-01T00:00:00+00:00",
    )
    foreign_lease = WorkerLease(foreign.run_dir, "another-worker")
    foreign_lease.acquire()
    try:
        conflict_report = AgentControlPlane(config).reconcile_jobs()
    finally:
        foreign_lease.release()

    assert foreign.job_id in conflict_report["worker_identity_conflicts"]
    assert control.store.get_job(foreign.job_id).status == "queued"
    assert control.slots.inspect_slot("app-1").active_job_id == foreign.job_id


def test_reconcile_terminal_job_never_touches_slot_owned_by_new_job(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot)
    control = AgentControlPlane(config)
    old_job = _active_slot_job(control, tmp_path, slot, "job-old-owner")
    control.store.mark_finished(old_job.job_id, "completed")
    control.slot_store.release_slot("app-1", old_job.job_id)

    new_job = _active_slot_job(control, tmp_path, slot, "job-new-owner")
    new_change = slot / "new-job-change.txt"
    new_change.write_text("must survive old-job recovery\n", encoding="utf-8")

    report = AgentControlPlane(config).reconcile_jobs(old_job.job_id)

    assert report["reconciled_terminal_jobs"] == []
    assert report["errors"]
    assert control.store.get_job(old_job.job_id).finalization_status == "failed"
    assert control.slots.inspect_slot("app-1").active_job_id == new_job.job_id
    assert new_change.read_text(encoding="utf-8") == "must survive old-job recovery\n"
    assert "new-job-change.txt" in workspace_state(slot).porcelain


def test_start_assigns_worker_identity_before_launch(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot))
    task_id = "worker-identity-start"
    task_dir = control.config.coordination_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("Implement the bounded task.\n", encoding="utf-8")
    (control.config.coordination_root / "agent-protocol.md").write_text(
        "Follow the task brief and write the required result.\n",
        encoding="utf-8",
    )
    (control.config.coordination_root / "workspace-routing.md").write_text(
        "Use only the assigned workspace.\n",
        encoding="utf-8",
    )
    launched: dict[str, str] = {}

    def launch(job_id: str, worker_instance_id: str) -> int:
        launched[job_id] = worker_instance_id
        assigned = control.store.get_job(job_id)
        assert assigned.worker_instance_id == worker_instance_id
        assert assigned.status == "queued"
        return 4242

    with patch.object(control, "_launch_worker", side_effect=launch):
        job = control.start_job(StartOptions(task_id=task_id, route="app"))

    assert launched[job.job_id] == job.worker_instance_id
    assert job.worker_pid == 4242


def test_stale_worker_identity_cannot_mutate_or_finish_replacement(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot))
    job = _active_slot_job(control, tmp_path, slot, "job-worker-fence")
    control.store.assign_worker(job.job_id, "old-worker", worker_pid=100)
    control.store.update_job(
        job.job_id,
        worker_instance_id="replacement-worker",
        worker_pid=200,
    )

    assert not control.store.update_for_worker(
        job.job_id,
        "old-worker",
        status="running",
        runner_pid=999,
    )
    assert control.store.mark_finished_by_worker(job.job_id, "old-worker", "completed") is None
    current = control.store.get_job(job.job_id)
    assert current.status == "queued"
    assert current.worker_instance_id == "replacement-worker"
    assert current.worker_pid == 200
    assert current.runner_pid is None


def test_run_job_holds_matching_worker_lease(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "route")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot))
    task_id = "job-lease-runtime"
    task_dir = control.config.coordination_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("Complete the lease test.\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / task_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text("Complete the lease test.\n", encoding="utf-8")
    result_path = task_dir / "result.md"
    result_path.write_text("Status: blocked\n", encoding="utf-8")
    job = control.store.create_job(
        job_id=task_id,
        task_id=task_id,
        route="app",
        workspace_path=route,
        expected_branch="main",
        config_path=control.config.config_path,
        run_dir=run_dir,
        prompt_path=prompt_path,
        result_path=result_path,
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
    )
    runner = _LeaseObservingRunner(control, job.job_id)

    with patch.object(control, "_runner_for_backend", return_value=runner):
        finished = control.run_job(job.job_id)

    assert finished.status == "completed"
    assert runner.observed_state is WorkerLeaseState.HELD_MATCH


def _active_slot_job(
    control: AgentControlPlane,
    root: Path,
    slot: Path,
    job_id: str,
):
    run_dir = root / "runs" / job_id
    run_dir.mkdir(parents=True)
    result_path = root / ".agent-work" / "tasks" / job_id / "result.md"
    result_path.parent.mkdir(parents=True)
    result_path.write_text("Status: completed\n", encoding="utf-8")
    job = control.store.create_job(
        job_id=job_id,
        task_id=job_id,
        route="app",
        workspace_path=slot,
        expected_branch="main",
        config_path=control.config.config_path,
        run_dir=run_dir,
        prompt_path=run_dir / "prompt.md",
        result_path=result_path,
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        slot_name="app-1",
    )
    control.slots.sync_configured_slots()
    control.slot_store.acquire_slot("app-1", job.job_id)
    return job


def _wait_for_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"Coordinator exited before ready: {stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"Coordinator did not become ready: {path}")
        time.sleep(0.05)


def _config(root: Path, route: Path, slot: Path) -> ControlConfig:
    model_catalog = _model_catalog(root)
    defaults = replace(
        ControlDefaults(
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=0,
            yolo=False,
            allow_dirty=False,
            prepare_slots=False,
            guardrail_poll_sec=1.0,
            forbidden_status_globs=(),
            codex_model="test-codex",
            codex_mechanical_model="test-codex",
            codex_balanced_model="test-codex",
            codex_deep_model="test-codex",
        ),
        terminal_slot_policy="checkpoint",
    )
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=route,
        slot_root=root / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=defaults,
        model_catalog=model_catalog,
        routes=MappingProxyType(
            {
                "app": RouteConfig(
                    name="app",
                    path=route,
                    required_branch="main",
                    worktree_root=root / "worktrees",
                    worktree_base=route,
                    source_roots=(Path("src"),),
                    test_roots=(Path("tests"),),
                    exclude_dirs=(),
                )
            }
        ),
        slots=MappingProxyType({"app-1": SlotConfig(name="app-1", route="app", path=slot)}),
        slot_prepare=(),
    )


def _model_catalog(root: Path) -> CodexModelCatalogConfig:
    cache_path = root / "models_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "test-codex",
                        "supported_reasoning_levels": ["low", "medium"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return CodexModelCatalogConfig(
        cache_path=cache_path,
        max_cache_age_sec=60.0,
        models=(),
        quota_domains=(CodexQuotaDomainConfig("primary", 2, 8, 75.0),),
    )


def _committed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "checkout", "-b", "main")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=ACP Test",
        "-c",
        "user.email=acp-test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    return path


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


class _LeaseObservingRunner:
    def __init__(self, control: AgentControlPlane, job_id: str) -> None:
        self.control = control
        self.job_id = job_id
        self.observed_state: WorkerLeaseState | None = None

    def run(self, spec, *, cancel_requested, pid_observed):
        active = self.control.store.get_job(self.job_id)
        assert active.worker_instance_id is not None
        self.observed_state = probe_worker_lease(
            active.run_dir,
            active.worker_instance_id,
        ).state
        pid_observed(os.getpid())
        assert cancel_requested() is False
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="lease observed",
        )
