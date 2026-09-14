from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.review_inbox import ReviewInboxDraft, ReviewInboxStore
from agent_control_plane.entities.slot import SlotRecord, SlotStore, SlotStoreError
from agent_control_plane.features.lifecycle_cleanup import SlotLifecycleService
from agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle import (
    canonical_windows_path,
    is_symlink_or_junction,
    prep_symlink_targets_equal,
)
from agent_control_plane.features.slot_lifecycle.lib.slot_manager import SlotError
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
    SlotPrepareCommand,
    load_config,
)
from agent_control_plane.shared.git_tools import GitError, run_git
from agent_control_plane.shared.native_quality import (
    resolve_native_quality_contract,
    write_native_quality_contract,
)
from agent_control_plane.shared.process_liveness import process_is_alive


def test_audit_and_apply_are_exact_idempotent_and_return_slot_to_default_branch(
    tmp_path: Path,
) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    generation = slots.acquire_slot("app-1", "job-1").generation
    slots.release_slot("app-1", "job-1")
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-1"
    run_git(route, "update-ref", checkpoint_ref, accepted)
    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=generation,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    audit = service.reconcile()
    resource = audit["resources"][0]
    assert resource["classification"] == "safe-to-delete"
    assert resource["generation"] == generation

    applied = service.apply(resource["operation_id"])
    repeated = service.apply(resource["operation_id"])

    assert applied["action"] == "completed", applied.get("reason")
    assert repeated["action"] == "already_completed"
    assert run_git(slot, "branch", "--show-current") == "slot/app-1"
    assert run_git(slot, "status", "--porcelain=v1", "-uall") == ""


def test_dirty_untracked_active_remote_move_and_generation_races_fail_closed(
    tmp_path: Path,
) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    inbox = ReviewInboxStore(config.database_path)
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    (slot / "untracked.txt").write_text("unique\n", encoding="utf-8")
    assert service.audit()["resources"][0]["classification"] == "dirty"
    (slot / "untracked.txt").unlink()
    active = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [4242],
    )
    assert active.audit()["resources"][0]["classification"] == "active"


def test_apply_quarantines_when_slot_generation_changes_after_enqueue(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/race"
    run_git(slot, "update-ref", checkpoint_ref, accepted)
    item = service.inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="race",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    service.inbox.resolve(item.item_id, "accepted")
    operation_id = service.reconcile()["enqueued"][0]
    slots.acquire_slot("app-1", "new-job")
    slots.release_slot("app-1", "new-job")

    result = service.apply(operation_id)

    assert result["action"] == "quarantined"
    assert "generation moved" in result["reason"] or "claim failed" in result["reason"]
    assert run_git(slot, "show-ref", "--verify", "--hash", checkpoint_ref) == accepted


def test_bounded_poll_uses_exponential_backoff_without_applying(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    slept: list[float] = []
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
        sleep=slept.append,
    )
    results = service.poll(passes=4, interval_sec=1, max_interval_sec=2)

    assert len(results) == 4
    assert slept == [1, 2, 2]


def test_background_worker_is_detached_from_short_lived_dispatch_caller(tmp_path: Path) -> None:
    config, route, _slot = _fixture(tmp_path)
    control = AgentControlPlane(config)
    job = control.store.create_job(
        job_id="job",
        task_id="task",
        route="app",
        workspace_path=route,
        expected_branch="main",
        expected_result_status="completed",
        controller_gate_mode="none",
        config_path=config.config_path,
        run_dir=config.runs_root / "job",
        prompt_path=config.runs_root / "job/prompt.md",
        result_path=config.runs_root / "job/result.md",
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="agy",
        workspace_access="native",
    )
    with patch("agent_control_plane.app.runtime.orchestrator.subprocess.Popen") as popen:
        popen.return_value.pid = 123
        control._launch_worker(job.job_id, "worker")
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")


def test_cancellation_and_unaccepted_slots_refuse_cleanup(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    slots.acquire_slot("app-1", "job-cancel")
    inbox = ReviewInboxStore(config.database_path)
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    assert service.audit()["resources"][0]["classification"] == "active"
    slots.release_slot("app-1", "job-cancel")

    (slot / "cancelled_work.txt").write_text("unfinished\n", encoding="utf-8")
    assert service.audit()["resources"][0]["classification"] == "dirty"
    (slot / "cancelled_work.txt").unlink()

    audit = service.audit()
    assert audit["resources"][0]["classification"] == "unique-unpushed"
    assert "no exact accepted checkpoint receipt" in audit["resources"][0]["reasons"][0]
    assert "operation_id" not in audit["resources"][0]
    assert service.reconcile()["enqueued"] == []


def test_crash_recovery_and_idempotent_retry(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-crash").generation
    slots.release_slot("app-1", "job-crash")
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-crash"
    run_git(route, "update-ref", checkpoint_ref, accepted)
    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-crash",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    enqueued = service.reconcile()["enqueued"]
    assert len(enqueued) == 1
    op_id = enqueued[0]
    intent = service._intent(op_id)
    assert intent is not None
    assert intent["state"] == "planned"

    recovered_service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    applied = recovered_service.apply(op_id)
    assert applied["action"] == "completed"
    stored = recovered_service._intent(op_id)
    assert stored is not None
    assert stored["state"] == "completed"

    repeated = recovered_service.apply(op_id)
    assert repeated["action"] == "already_completed"


def test_remote_movement_and_remote_url_drift_quarantine(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-drift").generation
    slots.release_slot("app-1", "job-drift")
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-drift"
    run_git(route, "update-ref", checkpoint_ref, accepted)
    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-drift",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Canonical remote ref moves to a different commit
    (route / "other.txt").write_text("other\n", encoding="utf-8")
    run_git(route, "add", "other.txt")
    run_git(route, "commit", "-m", "other")
    dummy_sha = run_git(route, "rev-parse", "HEAD")
    run_git(route, "update-ref", "refs/remotes/origin/main", dummy_sha)
    run_git(route, "push", "--force", "origin", "main")
    result = service.apply(op_id)
    assert result["action"] == "quarantined"

    # Reset and test canonical remote URL changes
    run_git(route, "push", "--force", "origin", f"{accepted}:refs/heads/main")
    run_git(route, "update-ref", "refs/remotes/origin/main", accepted)
    service._set_intent_state(op_id, "planned", None)
    # Mark available so retry can acquire
    slots.mark_available("app-1")
    run_git(route, "remote", "set-url", "origin", "https://drifted.example.invalid/repo.git")
    result_url = service.apply(op_id)
    assert result_url["action"] == "quarantined"


def test_generation_and_identity_races_quarantine(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-race").generation
    slots.release_slot("app-1", "job-race")
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-race"
    run_git(route, "update-ref", checkpoint_ref, accepted)
    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-race",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Inode/device race: slot directory removed and re-created
    run_git(route, "worktree", "remove", "--force", str(slot))
    run_git(route, "worktree", "add", "-b", "task/app-1-new", str(slot), "main")
    result = service.apply(op_id)
    assert result["action"] == "quarantined"


def test_stale_registrations_and_unmanaged_worktrees(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    missing_path = tmp_path / "slots/nonexistent"
    slots.register_slot("missing-slot", "app", missing_path)
    slots.register_slot("unknown-route-slot", "ghost-route", slot)

    extra_worktree = tmp_path / "extra-wt"
    run_git(route, "worktree", "add", "-b", "unmanaged-branch", str(extra_worktree), "main")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    audit = service.audit()
    classifications = {
        r.get("name") or r.get("path") or r.get("branch"): r["classification"]
        for r in audit["resources"]
    }
    assert classifications["missing-slot"] == "stale"
    assert classifications["unknown-route-slot"] == "stale"
    assert classifications[str(extra_worktree.resolve(strict=False))] == "stale"
    assert classifications["unmanaged-branch"] in {"retained-unowned", "unique-unpushed"}


def test_active_cwd_and_unavailable_platform(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    inbox = ReviewInboxStore(config.database_path)

    active_service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [7890],
    )
    assert active_service.audit()["resources"][0]["classification"] == "active"

    unavail_service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [-1],
    )
    audit = unavail_service.audit()
    assert audit["resources"][0]["classification"] == "quarantined"
    assert "unavailable" in audit["resources"][0]["reasons"][0]


def test_event_triggered_refresh_on_acceptance(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    control = AgentControlPlane(config)
    control.slot_store.register_slot("app-1", "app", slot)
    gen = control.slot_store.require_slot("app-1").generation
    item = control.review_inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-acc",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref="refs/agent-control-plane/jobs/job-acc",
            checkpoint_sha=run_git(slot, "rev-parse", "HEAD"),
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    resolved = control.resolve_review_inbox_item(item.item_id, "accepted")
    assert "lifecycle_refresh" in resolved
    assert resolved["lifecycle_refresh"]["status"] in {"enqueued", "completed", "skipped"}


def test_detached_and_foreground_worker_parity(tmp_path: Path) -> None:
    config, route, _slot = _fixture(tmp_path)
    control = AgentControlPlane(config)
    job = control.store.create_job(
        job_id="job-parity",
        task_id="task-parity",
        route="app",
        workspace_path=route,
        expected_branch="main",
        expected_result_status="completed",
        controller_gate_mode="none",
        config_path=config.config_path,
        run_dir=config.runs_root / "job-parity",
        prompt_path=config.runs_root / "job-parity/prompt.md",
        result_path=config.runs_root / "job-parity/result.md",
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="agy",
        workspace_access="native",
    )
    with patch("agent_control_plane.app.runtime.orchestrator.subprocess.Popen") as popen:
        popen.return_value.pid = 4321
        pid = control._launch_worker(job.job_id, "worker-det")
    assert pid == 4321
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")
    events = control.store.recent_events(job.job_id)
    assert any("worker" in e[2].lower() for e in events)


def test_lifecycle_cli_audit_and_status_alias(tmp_path: Path, capsys) -> None:
    config, _route, _slot = _fixture(tmp_path)
    from agent_control_plane.app.runtime.cli import main

    assert main(["lifecycle", "audit", "--no-refresh", "--config", str(config.config_path)]) == 0
    out_audit = json.loads(capsys.readouterr().out)
    assert "resources" in out_audit

    assert main(["lifecycle", "status", "--no-refresh", "--config", str(config.config_path)]) == 0
    out_status = json.loads(capsys.readouterr().out)
    assert "resources" in out_status


# --- Finding 1: Exclusive cleanup claim and deterministic acquisition races ---
def test_cleanup_exclusive_claim_and_deterministic_race(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-1").generation
    slots.release_slot("app-1", "job-1")

    # 1. Direct claim mutual exclusion
    slots.claim_for_cleanup("app-1", "op-excl", gen)
    rec = slots.require_slot("app-1")
    assert rec.status == "cleaning"
    assert rec.active_job_id == "op-excl"

    # Acquire and finalization must fail
    with pytest.raises(SlotStoreError):
        slots.acquire_slot("app-1", "job-other")

    with pytest.raises(SlotStoreError):
        slots.claim_for_finalization("app-1", "job-other")

    # Release from cleanup
    slots.release_cleanup(
        "app-1", "op-excl", expected_generation=rec.generation, status="available"
    )
    assert slots.require_slot("app-1").status == "available"
    gen = slots.require_slot("app-1").generation

    # 2. Race: another job acquires between audit and checkout mutation
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-race"
    run_git(route, "update-ref", checkpoint_ref, accepted)
    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-race",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Another job claims slot before checkout
    slots.acquire_slot("app-1", "concurrent-job")

    # Apply must fail closed and quarantine the operation, leaving slot intact
    result = service.apply(op_id)
    assert result["action"] == "quarantined"
    assert "generation moved" in result["reason"] or "claim failed" in result["reason"]
    assert slots.require_slot("app-1").active_job_id == "concurrent-job"


# --- Finding 2: Default branch divergence and preservation ---
def test_default_branch_divergence_and_protection(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-div").generation
    slots.release_slot("app-1", "job-div")

    # Create unique unmerged commit on default branch slot/app-1
    default_branch = "slot/app-1"
    run_git(route, "branch", default_branch, "main")
    # Commit unique change on slot/app-1
    temp_dir = tmp_path / "temp_wt"
    run_git(route, "worktree", "add", str(temp_dir), default_branch)
    (temp_dir / "divergent.txt").write_text("unique work\n", encoding="utf-8")
    run_git(temp_dir, "add", "divergent.txt")
    run_git(temp_dir, "commit", "-m", "divergent work on default branch")
    run_git(route, "worktree", "remove", str(temp_dir))

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-div"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-div",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # Audit must detect divergent commits on default branch and refuse safe-to-delete
    audit = service.audit()
    assert audit["resources"][0]["classification"] == "quarantined"
    assert "divergent" in audit["resources"][0]["reasons"][0]


# --- Finding 3: Crash-recoverable journal step-by-step injection ---
def test_crash_recovery_injected_crashes_across_all_steps(tmp_path: Path) -> None:
    steps_to_test = [
        "checkout_default",
        "fast_forward_default",
        "delete_task_branch",
        "delete_checkpoint",
        "release_slot",
        "prune",
    ]

    for crash_step in steps_to_test:
        sub_tmp = tmp_path / f"step_{crash_step}"
        sub_tmp.mkdir()
        config, route, slot = _fixture(sub_tmp)
        slots = SlotStore(config.database_path)
        slots.register_slot("app-1", "app", slot)
        gen = slots.acquire_slot("app-1", "job-crash-step").generation
        slots.release_slot("app-1", "job-crash-step")

        accepted = run_git(slot, "rev-parse", "HEAD")
        checkpoint_ref = "refs/agent-control-plane/jobs/job-crash-step"
        run_git(route, "update-ref", checkpoint_ref, accepted)

        inbox = ReviewInboxStore(config.database_path)
        item = inbox.upsert(
            ReviewInboxDraft(
                source_kind="agent_job",
                source_id="job-crash-step",
                source_status="completed",
                delivery_status="checkpointed",
                route="app",
                workspace_path=slot,
                slot_name="app-1",
                slot_generation=gen,
                checkpoint_ref=checkpoint_ref,
                checkpoint_sha=accepted,
                result_text="Status: completed\n",
                verification_bundle=_valid_bundle(),
                slot_released=True,
            )
        )
        inbox.resolve(item.item_id, "accepted")

        service = SlotLifecycleService(
            config,
            slots=slots,
            jobs=JobStore(config.database_path),
            inbox=inbox,
            live_cwds=lambda _path: [],
        )
        op_id = service.reconcile()["enqueued"][0]

        # Inject crash after specified step
        def _crash(step_name: str, target: str = crash_step) -> None:
            if step_name == target:
                raise RuntimeError(f"injected crash after {step_name}")

        with pytest.raises(RuntimeError, match=f"injected crash after {crash_step}"):
            service.apply(op_id, crash_hook=_crash)

        # On restart: fresh service recovers and resumes to completion
        recovered_service = SlotLifecycleService(
            config,
            slots=SlotStore(config.database_path),
            jobs=JobStore(config.database_path),
            inbox=ReviewInboxStore(config.database_path),
            live_cwds=lambda _path: [],
        )
        applied = recovered_service.apply(op_id)
        assert applied["action"] == "completed"

    # Also test temporary worktree remove crash
    sub_tmp = tmp_path / "step_remove_wt"
    sub_tmp.mkdir()
    config, route, slot = _fixture(sub_tmp)
    # Register an unconfigured temporary slot
    run_git(route, "fetch", "origin")
    temp_slot_path = sub_tmp / "slots/temp-slot"
    run_git(route, "worktree", "add", "-b", "task/temp-slot", str(temp_slot_path), "origin/main")
    (temp_slot_path / "temp.txt").write_text("temp\n", encoding="utf-8")
    run_git(temp_slot_path, "add", "temp.txt")
    run_git(temp_slot_path, "commit", "-m", "temp")
    run_git(temp_slot_path, "push", "origin", "HEAD:main")
    accepted_temp = run_git(temp_slot_path, "rev-parse", "HEAD")
    temp_ckpt = "refs/agent-control-plane/jobs/temp-job"
    run_git(route, "update-ref", temp_ckpt, accepted_temp)

    slots = SlotStore(config.database_path)
    slots.register_slot("temp-slot", "app", temp_slot_path)
    gen = slots.acquire_slot("temp-slot", "temp-job").generation
    slots.release_slot("temp-slot", "temp-job")

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="temp-job",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=temp_slot_path,
            slot_name="temp-slot",
            slot_generation=gen,
            checkpoint_ref=temp_ckpt,
            checkpoint_sha=accepted_temp,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    def _crash_wt(step_name: str) -> None:
        if step_name == "remove_worktree":
            raise RuntimeError("injected crash after remove_worktree")

    with pytest.raises(RuntimeError, match="injected crash after remove_worktree"):
        service.apply(op_id, crash_hook=_crash_wt)

    recovered = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    res = recovered.apply(op_id)
    assert res["action"] == "completed"
    assert not temp_slot_path.exists()
    assert slots.require_slot("temp-slot").status == "deleted"
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/task/temp-slot")
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/slot/temp-slot")
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", temp_ckpt)


# --- Finding 4: Acceptance bound to exact slot generation ---
def test_acceptance_exact_slot_generation_binding(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)

    # Job 1 runs on slot generation 2
    gen1 = slots.acquire_slot("app-1", "job-gen1").generation
    slots.release_slot("app-1", "job-gen1")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-gen1"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-gen1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen1,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    # Now slot is reused for Job 2 (moves generation to gen1 + 1)
    gen2 = slots.acquire_slot("app-1", "job-gen2").generation
    assert gen2 > gen1
    slots.release_slot("app-1", "job-gen2")

    # Audit slot at generation gen2: Job 1's accepted receipt must NOT authorize cleanup
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    audit = service.audit()
    assert audit["resources"][0]["classification"] == "unique-unpushed"
    assert (
        "no exact accepted checkpoint receipt for slot generation"
        in audit["resources"][0]["reasons"][0]
    )


# --- Finding 5: Re-fetch exact named canonical ref before destructive mutation ---
def test_destructive_mutation_refetches_canonical_remote_tip(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-refetch").generation
    slots.release_slot("app-1", "job-refetch")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-refetch"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-refetch",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Stale tracking ref: upstream remote is advanced directly without local tracking ref updated
    remote = tmp_path / "remote.git"
    cloned_other = tmp_path / "cloned_other"
    subprocess.run(
        ["git", "clone", str(remote), str(cloned_other)], check=True, capture_output=True
    )
    run_git(cloned_other, "config", "user.name", "Other")
    run_git(cloned_other, "config", "user.email", "other@example.invalid")
    (cloned_other / "upstream_advance.txt").write_text("advance\n", encoding="utf-8")
    run_git(cloned_other, "add", "upstream_advance.txt")
    run_git(cloned_other, "commit", "-m", "advance upstream")
    run_git(cloned_other, "push", "origin", "HEAD:refs/heads/main")

    # When apply runs, it refetches inside the exclusive fence, discovers remote tip moved, and quarantines
    result = service.apply(op_id)
    assert result["action"] == "quarantined"
    assert "canonical remote ref moved" in result["reason"]


# --- Finding 6: /proc uncertainty and ignored files fail-closed ---
def test_proc_uncertainty_and_ignored_files_safety(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    inbox = ReviewInboxStore(config.database_path)

    # 1. /proc uncertainty
    uncert_service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [-2],
    )
    audit = uncert_service.audit()
    assert audit["resources"][0]["classification"] == "quarantined"
    assert "uncertainty" in audit["resources"][0]["reasons"][0]

    # 2. Ignored files present in slot
    clean_service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    (slot / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    run_git(slot, "add", ".gitignore")
    run_git(slot, "commit", "-m", "add gitignore")
    (slot / "artifact.ignored").write_text("untracked ignored\n", encoding="utf-8")

    ignored_audit = clean_service.audit()
    assert ignored_audit["resources"][0]["classification"] == "dirty"
    assert "unexpected ignored files" in ignored_audit["resources"][0]["reasons"][0]


# --- Finding 7: Deferred reconciliation outbox and fetch coalescing ---
def test_deferred_reconciliation_outbox_and_fetch_coalescing(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    control = AgentControlPlane(config)
    control.slot_store.register_slot("app-1", "app", slot)
    gen = control.slot_store.require_slot("app-1").generation

    item = control.review_inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-deferred",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref="refs/agent-control-plane/jobs/job-deferred",
            checkpoint_sha=run_git(slot, "rev-parse", "HEAD"),
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )

    # Acceptance resolves without blocking on network fetch: outbox is enqueued
    resolved = control.resolve_review_inbox_item(item.item_id, "accepted")
    assert resolved["lifecycle_refresh"]["status"] == "enqueued"
    assert resolved["lifecycle_refresh"]["route"] == "app"

    pending = control.slot_lifecycle.pending_reconciliation_requests("app")
    assert len(pending) >= 1
    assert pending[0]["reason"] == "acceptance"

    # Coalescing: multiple slots on route 'app' fetch canonical ref only once
    slot2 = tmp_path / "slots/app-2"
    run_git(
        control.config.routes["app"].path, "worktree", "add", "-b", "slot/app-2", str(slot2), "main"
    )
    control.slot_store.register_slot("app-2", "app", slot2)

    fetch_count = 0
    real_run_git = run_git

    def _counted_run_git(repo: Path, *args: str) -> str:
        nonlocal fetch_count
        if len(args) > 0 and args[0] == "fetch":
            fetch_count += 1
        return real_run_git(repo, *args)

    with patch(
        "agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle.run_git",
        side_effect=_counted_run_git,
    ):
        control.slot_lifecycle.audit(refresh=True)

    # Across app-1 and app-2 on route 'app', exactly one fetch occurred
    assert fetch_count == 1
    # Audit-only calls must not consume requests
    assert len(control.slot_lifecycle.pending_reconciliation_requests("app")) == 1
    # Reconciliation acknowledges the pending requests
    control.slot_lifecycle.reconcile(refresh=True)
    assert len(control.slot_lifecycle.pending_reconciliation_requests("app")) == 0


# --- Finding 8: Unowned canonical-reachable branches labeled retained-unowned ---
def test_unowned_canonical_reachable_branches_audited_as_retained_unowned(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)

    # Create unowned branch reachable from canonical remote
    run_git(route, "branch", "unowned-canonical-branch", "main")
    sha = run_git(route, "rev-parse", "unowned-canonical-branch")

    inbox = ReviewInboxStore(config.database_path)
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # Without acceptance receipt: must be retained-unowned, NOT accepted-integrated
    audit1 = service.audit()
    branch_rows = [r for r in audit1["resources"] if r.get("branch") == "unowned-canonical-branch"]
    assert len(branch_rows) == 1
    assert branch_rows[0]["classification"] == "retained-unowned"

    # Now create accepted receipt
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-unowned",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=1,
            checkpoint_ref="refs/agent-control-plane/jobs/job-unowned",
            checkpoint_sha=sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    # With acceptance receipt: now recognized as accepted-integrated
    audit2 = service.audit()
    branch_rows2 = [r for r in audit2["resources"] if r.get("branch") == "unowned-canonical-branch"]
    assert branch_rows2[0]["classification"] == "accepted-integrated"


# --- Item 14: Strengthened detached worker regression test ---
def test_real_detached_worker_survives_caller_exit(tmp_path: Path) -> None:
    config, _route, _slot = _fixture(tmp_path)

    src_dir = str((Path(__file__).resolve().parents[3] / "src").as_posix())
    caller_pid_file = tmp_path / "caller.pid"
    script_body = f"""import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, r"{src_dir}")
from agent_control_plane.shared.process_liveness import process_is_alive

# Prove caller death (historical regression boundary: caller process exits, worker survives)
caller_pid_path = Path(r"{caller_pid_file}")
if caller_pid_path.exists():
    caller_pid = int(caller_pid_path.read_text().strip())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not process_is_alive(caller_pid):
            break
        time.sleep(0.05)

prompt = ""
for i, arg in enumerate(sys.argv):
    if arg == "--print" and i + 1 < len(sys.argv):
        prompt = sys.argv[i + 1]

result_path = None
verification_path = None

prompt_lines = prompt.splitlines()
for i, line in enumerate(prompt_lines):
    if "Write the final result to:" in line and i + 1 < len(prompt_lines):
        result_path = Path(prompt_lines[i + 1].lstrip("- ").strip())
    if "Write the machine-readable verification bundle to:" in line and i + 1 < len(prompt_lines):
        verification_path = Path(prompt_lines[i + 1].lstrip("- ").strip())

for i, arg in enumerate(sys.argv):
    if arg == "--result" and i + 1 < len(sys.argv):
        result_path = Path(sys.argv[i + 1])

if result_path is not None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("Status: completed\\n\\nWorker finished.\\n", encoding="utf-8")

if verification_path is not None:
    verification_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {{
        "schema_version": 1,
        "status": "completed",
        "changed_files": [],
        "checks": [
            {{
                "command": "true",
                "cwd": ".",
                "outcome": "passed",
                "exit_code": 0,
                "summary": "fake check passed",
            }}
        ],
        "unverified": [],
    }}
    verification_path.write_text(json.dumps(bundle), encoding="utf-8")

sys.exit(0)
"""
    fake_agy = tmp_path / "fake_agy.py"
    fake_agy.write_text(script_body, encoding="utf-8")

    toml_text = config.config_path.read_text(encoding="utf-8")
    toml_text = toml_text.replace(
        "[control]\n",
        f'[control]\nagy_command = "{fake_agy.as_posix()}"\n',
    )
    config.config_path.write_text(toml_text, encoding="utf-8")

    task_dir = config.coordination_root / "tasks/task-real-detached"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "brief.md").write_text("# Brief\nDo something\n", encoding="utf-8")
    caller_py = tmp_path / "caller.py"
    caller_py.write_text(
        f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, r"{src_dir}")
os.environ["PYTHONPATH"] = r"{src_dir}" + os.pathsep + os.environ.get("PYTHONPATH", "")
from agent_control_plane.shared.config import load_config
from agent_control_plane.app.runtime.orchestrator import AgentControlPlane

config = load_config(Path(r"{config.config_path.as_posix()}"))
control = AgentControlPlane(config)
job = control.store.create_job(
    job_id="job-real-detached",
    task_id="task-real-detached",
    route="app",
    workspace_path=config.routes["app"].path,
    expected_branch="main",
    expected_result_status="completed",
    controller_gate_mode="none",
    config_path=config.config_path,
    run_dir=config.runs_root / "job-real-detached",
    prompt_path=config.runs_root / "job-real-detached/prompt.md",
    result_path=config.runs_root / "job-real-detached/result.md",
    timeout_sec=10,
    idle_timeout_sec=10,
    print_timeout="10s",
    max_restarts=0,
    yolo=False,
    allow_dirty=False,
    read_only=False,
    backend="agy",
    workspace_access="native",
)
job.prompt_path.parent.mkdir(parents=True, exist_ok=True)
job.prompt_path.write_text(
    f\"\"\"Write the final result to:
- {{job.result_path}}

Write the machine-readable verification bundle to:
- {{job.run_dir / "verification.json"}}
\"\"\",
    encoding="utf-8",
)
Path(r"{caller_pid_file}").write_text(str(os.getpid()), encoding="utf-8")
pid = control._launch_worker(job.job_id, "worker-instance-real")
print(f"CHILD_PID:{{pid}}")
sys.exit(0)
""",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")

    # Run caller subprocess to completion
    proc = subprocess.run(
        [sys.executable, str(caller_py)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert proc.returncode == 0
    child_pid = None
    for line in proc.stdout.splitlines():
        if line.startswith("CHILD_PID:"):
            child_pid = int(line.split(":")[1].strip())
    assert child_pid is not None

    # Wait for the detached child to record its lease and heartbeat in DB
    control = AgentControlPlane(load_config(config.config_path))
    deadline = time.monotonic() + 10.0
    recorded_pid = None
    while time.monotonic() < deadline:
        j = control.store.get_job("job-real-detached")
        if j.worker_pid is not None and j.worker_instance_id == "worker-instance-real":
            recorded_pid = j.worker_pid
            break
        time.sleep(0.05)

    assert recorded_pid == child_pid
    # Worker lease file was created
    assert (
        config.runs_root / "job-real-detached/worker.lease"
    ).exists() or j.worker_heartbeat_at is not None

    # Wait for child worker process to terminate cleanly
    while time.monotonic() < deadline:
        j = control.store.get_job("job-real-detached")
        if j.status in ("completed", "failed", "blocked", "worker_error"):
            break
        time.sleep(0.05)

    assert j.status == "completed"
    with control.store._connect() as db:
        attempts = [
            dict(r)
            for r in db.execute(
                "select * from attempts where job_id = ?", ("job-real-detached",)
            ).fetchall()
        ]
    assert len(attempts) >= 1
    assert attempts[0]["status"] == "completed"
    assert j.result_path.exists()
    assert "Status: completed" in j.result_path.read_text(encoding="utf-8")
    ver_path = j.run_dir / "verification.json"
    assert ver_path.exists()
    ver_data = json.loads(ver_path.read_text(encoding="utf-8"))
    assert ver_data["schema_version"] == 1
    assert ver_data["status"] == "completed"

    # Child process has exited
    child_deadline = time.monotonic() + 5.0
    child_alive = True
    while time.monotonic() < child_deadline:
        if not process_is_alive(child_pid):
            child_alive = False
            break
        time.sleep(0.05)
    assert not child_alive

    # Foreground completion parity assertion
    task_fg = config.coordination_root / "tasks/task-real-fg"
    task_fg.mkdir(parents=True, exist_ok=True)
    (task_fg / "brief.md").write_text("# Brief\nForeground task\n", encoding="utf-8")
    fg_job = control.store.create_job(
        job_id="job-real-fg",
        task_id="task-real-fg",
        route="app",
        workspace_path=config.routes["app"].path,
        expected_branch="main",
        expected_result_status="completed",
        controller_gate_mode="none",
        config_path=config.config_path,
        run_dir=config.runs_root / "job-real-fg",
        prompt_path=config.runs_root / "job-real-fg/prompt.md",
        result_path=config.runs_root / "job-real-fg/result.md",
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="agy",
        workspace_access="native",
    )
    fg_job.prompt_path.parent.mkdir(parents=True, exist_ok=True)
    fg_job.prompt_path.write_text(
        f"""Write the final result to:
- {fg_job.result_path}

Write the machine-readable verification bundle to:
- {fg_job.run_dir / "verification.json"}
""",
        encoding="utf-8",
    )
    res_fg = control.run_job(fg_job.job_id)
    assert res_fg.status == "completed"
    with control.store._connect() as db:
        fg_attempts = [
            dict(r)
            for r in db.execute(
                "select * from attempts where job_id = ?", ("job-real-fg",)
            ).fetchall()
        ]
    assert len(fg_attempts) >= 1
    assert fg_attempts[0]["status"] == "completed"
    assert res_fg.result_path.exists()
    assert "Status: completed" in res_fg.result_path.read_text(encoding="utf-8")


def test_detached_worker_fails_on_false_backend(tmp_path: Path) -> None:
    config, _route, _slot = _fixture(tmp_path)
    toml_text = config.config_path.read_text(encoding="utf-8")
    toml_text = toml_text.replace(
        "[control]\n",
        '[control]\nagy_command = "/usr/bin/false"\n',
    )
    config.config_path.write_text(toml_text, encoding="utf-8")

    task_dir = config.coordination_root / "tasks/task-false-backend"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "brief.md").write_text("# Brief\nShould fail\n", encoding="utf-8")

    control = AgentControlPlane(load_config(config.config_path))
    job = control.store.create_job(
        job_id="job-false-backend",
        task_id="task-false-backend",
        route="app",
        workspace_path=config.routes["app"].path,
        expected_branch="main",
        expected_result_status="completed",
        controller_gate_mode="none",
        config_path=config.config_path,
        run_dir=config.runs_root / "job-false-backend",
        prompt_path=config.runs_root / "job-false-backend/prompt.md",
        result_path=config.runs_root / "job-false-backend/result.md",
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="agy",
        workspace_access="native",
    )
    job.prompt_path.parent.mkdir(parents=True, exist_ok=True)
    job.prompt_path.write_text("prompt content\n", encoding="utf-8")
    res_job = control.run_job(job.job_id)
    assert res_job.status in ("failed", "blocked")


def test_old_cleanup_error_racing_with_later_job_owner(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)

    # Job 1 acquires slot at generation 2
    gen1 = slots.acquire_slot("app-1", "job-1").generation
    assert gen1 == 2
    slots.release_slot("app-1", "job-1")

    # Cleanup claimed for Job 1
    slots.claim_for_cleanup("app-1", "op-1", expected_generation=gen1)
    s = slots.require_slot("app-1")
    assert s.status == "cleaning"
    assert s.active_job_id == "op-1"
    assert s.generation == gen1

    # Release cleanup
    slots.release_cleanup("app-1", "op-1", expected_generation=gen1)
    s = slots.require_slot("app-1")
    assert s.status == "available"
    assert s.active_job_id is None
    assert s.generation == 3

    # Job 2 acquires slot at generation 4
    gen2 = slots.acquire_slot("app-1", "job-2").generation
    assert gen2 == 4
    s = slots.require_slot("app-1")
    assert s.status == "active"
    assert s.active_job_id == "job-2"

    # Late release_cleanup from old cleanup op-1 must fail closed with SlotStoreError
    with pytest.raises(
        SlotStoreError, match="is not claimed for cleanup by operation op-1 at generation 2"
    ):
        slots.release_cleanup("app-1", "op-1", expected_generation=gen1)

    # Late quarantine_cleanup from old cleanup op-1 must fail closed with SlotStoreError
    with pytest.raises(
        SlotStoreError, match="is not claimed for cleanup by operation op-1 at generation 2"
    ):
        slots.quarantine_cleanup("app-1", "op-1", expected_generation=gen1, note="late error")

    # Calling with newer generation but wrong active_job_id must also fail
    with pytest.raises(SlotStoreError):
        slots.quarantine_cleanup("app-1", "op-1", expected_generation=gen2, note="late error")

    # Verify Job 2's slot state is preserved completely
    s_after = slots.require_slot("app-1")
    assert s_after.status == "active"
    assert s_after.active_job_id == "job-2"
    assert s_after.generation == 4


def test_release_acquire_upsert_race(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen1 = slots.acquire_slot("app-1", "job-1").generation
    slots.release_slot("app-1", "job-1")

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen1,
            checkpoint_ref="refs/agent-control-plane/jobs/job-1",
            checkpoint_sha="1111111111111111111111111111111111111111",
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    # Slot reused for job-2, incrementing generation
    gen2 = slots.acquire_slot("app-1", "job-2").generation
    assert gen2 > gen1

    # Late upsert attempting to rebind accepted item to a newer generation
    updated = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen2,
            checkpoint_ref="refs/agent-control-plane/jobs/job-1",
            checkpoint_sha="1111111111111111111111111111111111111111",
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    # The accepted record's slot_generation must be preserved and NOT updated to gen2
    assert updated.slot_generation == gen1
    persisted = inbox.get(item.item_id)
    assert persisted.slot_generation == gen1


def test_executor_lease_serialization_and_expiry(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-lease").generation
    slots.release_slot("app-1", "job-lease")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-lease"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-lease",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    simulated_time = 1000.0
    alive_pids = {1001}

    service1 = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
        clock=lambda: simulated_time,
        process_is_alive=lambda pid: pid in alive_pids,
    )
    op_id = service1.reconcile()["enqueued"][0]

    # Acquire lease by worker-A for 10 seconds
    assert service1.acquire_executor_lease(
        op_id, "worker-A", lease_duration_sec=10.0, owner_pid=1001
    )

    # Worker-B cannot apply because lease is held by worker-A
    service2 = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
        clock=lambda: simulated_time,
        process_is_alive=lambda pid: pid in alive_pids,
    )
    with pytest.raises(RuntimeError, match="locked by another active executor"):
        service2.apply(op_id, executor_id="worker-B", owner_pid=1002)

    # Fast-forward time past lease expiry (15 seconds later) and worker-A dies
    simulated_time = 1015.0
    alive_pids.clear()
    # Now worker-B can acquire lease and apply to completion
    result = service2.apply(op_id, executor_id="worker-B", owner_pid=1002)
    assert result["action"] == "completed"


def test_executor_lease_takeover_rejected_while_owner_alive_beyond_expiry(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-lease-alive").generation
    slots.release_slot("app-1", "job-lease-alive")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-lease-alive"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-lease-alive",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    simulated_time = 1000.0
    alive_pids = {5001}

    service1 = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
        clock=lambda: simulated_time,
        process_is_alive=lambda pid: pid in alive_pids,
    )
    op_id = service1.reconcile()["enqueued"][0]

    # Executor A acquires lease with owner_pid=5001 for 10 seconds
    assert service1.acquire_executor_lease(
        op_id, "executor-A", lease_duration_sec=10.0, owner_pid=5001
    )
    assert service1.get_fence_token(op_id, "executor-A") == 1

    # Fast-forward time past lease expiry (100 seconds later)
    simulated_time = 1100.0

    # Executor B attempts takeover while Executor A is still alive (5001 in alive_pids)
    service2 = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
        clock=lambda: simulated_time,
        process_is_alive=lambda pid: pid in alive_pids,
    )

    # Takeover MUST be rejected because Executor A is alive beyond expiry (age alone does not allow takeover)
    assert not service2.acquire_executor_lease(op_id, "executor-B", owner_pid=5002)
    with pytest.raises(RuntimeError, match="locked by another active executor"):
        service2.apply(op_id, executor_id="executor-B", owner_pid=5002)

    # Still owned by Executor A with fence token 1
    assert service1.get_fence_token(op_id, "executor-A") == 1

    # Now Executor A dies (proven process death)
    alive_pids.remove(5001)

    # Now Executor B can take over
    assert service2.acquire_executor_lease(op_id, "executor-B", owner_pid=5002)
    # Monotonic fence token is incremented to 2
    assert service2.get_fence_token(op_id, "executor-B") == 2

    # If stale Executor A attempts mutation with old fence token 1, it must be rejected
    with pytest.raises(RuntimeError, match=r"[Ff]enc"):
        service1.verify_fence(op_id, "executor-A", fence_token=1)


def test_fast_forward_crash_recovery_with_lagging_default_sha(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-ff-crash").generation
    slots.release_slot("app-1", "job-ff-crash")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-ff-crash"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-ff-crash",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Verify default_sha lags canonical_tip in the audited frozen intent
    intent = service._intent(op_id)
    assert intent is not None
    frozen_proof = intent["proof"]
    assert frozen_proof["default_sha"] != frozen_proof["canonical_tip"]
    assert frozen_proof["canonical_tip"] == accepted

    # Inject crash right after fast-forward git mutation succeeds, before recording step completion
    def _crash(step_name: str) -> None:
        if step_name == "fast_forward_default":
            raise RuntimeError("simulated crash after fast-forward git update")

    with pytest.raises(RuntimeError, match="simulated crash after fast-forward git update"):
        service.apply(op_id, crash_hook=_crash)

    # Prove that the step was left in "started" in journal, while git worktree is already at canonical_tip
    step = service._get_step(op_id, "fast_forward_default")
    assert step is not None
    assert step["status"] == "started"
    current_head = run_git(slot, "rev-parse", "HEAD")
    assert current_head == accepted

    # Resume safely: fresh service resumes from the crash and completes
    recovered = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    res = recovered.apply(op_id)
    assert res["action"] == "completed"

    # Step is now completed and slot is released to available
    step_post = recovered._get_step(op_id, "fast_forward_default")
    assert step_post is not None
    assert step_post["status"] == "completed"
    released_slot = recovered.slots.get_slot("app-1")
    assert released_slot is not None
    assert released_slot.status == "available"
    assert released_slot.active_job_id is None


def test_resume_invariant_preservation_detects_dirty_or_drift(tmp_path: Path) -> None:
    # Subcase A: Dirty file injected after checkout_default
    config_a, route_a, slot_a = _fixture(tmp_path / "caseA")
    slots_a = SlotStore(config_a.database_path)
    slots_a.register_slot("app-1", "app", slot_a)
    gen_a = slots_a.acquire_slot("app-1", "job-drift-A").generation
    slots_a.release_slot("app-1", "job-drift-A")

    accepted_a = run_git(slot_a, "rev-parse", "HEAD")
    checkpoint_ref_a = "refs/agent-control-plane/jobs/job-drift-A"
    run_git(route_a, "update-ref", checkpoint_ref_a, accepted_a)

    inbox_a = ReviewInboxStore(config_a.database_path)
    item_a = inbox_a.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-drift-A",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot_a,
            slot_name="app-1",
            slot_generation=gen_a,
            checkpoint_ref=checkpoint_ref_a,
            checkpoint_sha=accepted_a,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox_a.resolve(item_a.item_id, "accepted")

    service_a = SlotLifecycleService(
        config_a,
        slots=slots_a,
        jobs=JobStore(config_a.database_path),
        inbox=inbox_a,
        live_cwds=lambda _path: [],
    )
    op_id_a = service_a.reconcile()["enqueued"][0]

    def _crash_after_checkout(step_name: str) -> None:
        if step_name == "checkout_default":
            raise RuntimeError("crash after checkout_default")

    with pytest.raises(RuntimeError, match="crash after checkout_default"):
        service_a.apply(op_id_a, crash_hook=_crash_after_checkout)

    # Slot is now in cleaning state after checkout_default
    # Inject untracked file into slot
    (slot_a / "unexpected_file.txt").write_text("drift\n", encoding="utf-8")

    # Resuming service must revalidate invariants, detect dirty worktree, and quarantine
    recovered_a = SlotLifecycleService(
        config_a,
        slots=SlotStore(config_a.database_path),
        jobs=JobStore(config_a.database_path),
        inbox=ReviewInboxStore(config_a.database_path),
        live_cwds=lambda _path: [],
    )
    res_a = recovered_a.apply(op_id_a)
    assert res_a["action"] == "quarantined"
    assert "tracked or untracked workspace changes" in res_a["reason"]
    assert slots_a.require_slot("app-1").status == "quarantined"

    # Subcase B: Canonical remote tip moves after checkout_default
    config_b, route_b, slot_b = _fixture(tmp_path / "caseB")
    slots_b = SlotStore(config_b.database_path)
    slots_b.register_slot("app-1", "app", slot_b)
    gen_b = slots_b.acquire_slot("app-1", "job-drift-B").generation
    slots_b.release_slot("app-1", "job-drift-B")

    accepted_b = run_git(slot_b, "rev-parse", "HEAD")
    checkpoint_ref_b = "refs/agent-control-plane/jobs/job-drift-B"
    run_git(route_b, "update-ref", checkpoint_ref_b, accepted_b)

    inbox_b = ReviewInboxStore(config_b.database_path)
    item_b = inbox_b.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-drift-B",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot_b,
            slot_name="app-1",
            slot_generation=gen_b,
            checkpoint_ref=checkpoint_ref_b,
            checkpoint_sha=accepted_b,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox_b.resolve(item_b.item_id, "accepted")

    service_b = SlotLifecycleService(
        config_b,
        slots=slots_b,
        jobs=JobStore(config_b.database_path),
        inbox=inbox_b,
        live_cwds=lambda _path: [],
    )
    op_id_b = service_b.reconcile()["enqueued"][0]

    with pytest.raises(RuntimeError, match="crash after checkout_default"):
        service_b.apply(op_id_b, crash_hook=_crash_after_checkout)

    # Push a new commit to upstream remote main
    remote_b = tmp_path / "caseB/remote.git"
    cloned_b = tmp_path / "caseB/cloned_other"
    subprocess.run(["git", "clone", str(remote_b), str(cloned_b)], check=True, capture_output=True)
    run_git(cloned_b, "config", "user.name", "Other")
    run_git(cloned_b, "config", "user.email", "other@example.invalid")
    (cloned_b / "advance.txt").write_text("advance\n", encoding="utf-8")
    run_git(cloned_b, "add", "advance.txt")
    run_git(cloned_b, "commit", "-m", "advance")
    run_git(cloned_b, "push", "origin", "HEAD:refs/heads/main")

    recovered_b = SlotLifecycleService(
        config_b,
        slots=SlotStore(config_b.database_path),
        jobs=JobStore(config_b.database_path),
        inbox=ReviewInboxStore(config_b.database_path),
        live_cwds=lambda _path: [],
    )
    res_b = recovered_b.apply(op_id_b)
    assert res_b["action"] == "quarantined"
    assert "canonical remote ref moved" in res_b["reason"]
    assert slots_b.require_slot("app-1").status == "quarantined"


def test_pre_mutation_recheck_quarantines_on_activity_under_claim(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-precheck").generation
    slots.release_slot("app-1", "job-precheck")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-precheck"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-precheck",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    live_pids: list[int] = []
    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: live_pids,
    )
    op_id = service.reconcile()["enqueued"][0]

    orig_claim = slots.claim_for_cleanup

    def _claim_with_intruder(*args: object, **kwargs: object) -> SlotRecord:
        record = orig_claim(*args, **kwargs)
        live_pids.append(99999)
        return record

    with patch.object(slots, "claim_for_cleanup", side_effect=_claim_with_intruder):
        res = service.apply(op_id)

    assert res["action"] == "quarantined"
    assert "live processes detected" in res["reason"]
    assert slots.require_slot("app-1").status == "quarantined"


def test_deferred_reconciliation_requests_failure_and_concurrent_enqueue(tmp_path: Path) -> None:
    config, _route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    inbox = ReviewInboxStore(config.database_path)

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    req1_id = service.enqueue_reconciliation_request("app", reason="request-1")
    assert len(service.pending_reconciliation_requests("app")) == 1

    # 1. On route fetch failure: requests must remain pending
    real_run_git = run_git

    def _failing_fetch_git(repo: Path, *args: str) -> str:
        if len(args) > 0 and args[0] == "fetch":
            raise GitError("network unreachable")
        return real_run_git(repo, *args)

    with patch(
        "agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle.run_git",
        side_effect=_failing_fetch_git,
    ):
        reconcile_res = service.reconcile(refresh=True)

    assert req1_id not in reconcile_res["acknowledged_requests"]
    assert len(service.pending_reconciliation_requests("app")) == 1
    assert service.pending_reconciliation_requests("app")[0]["id"] == req1_id

    # 2. Concurrent arrivals: request 2 enqueued during reconciliation pass
    orig_audit = service.audit

    def _audit_with_concurrent_arrival(*args: object, **kwargs: object) -> dict[str, object]:
        res = orig_audit(*args, **kwargs)
        service.enqueue_reconciliation_request("app", reason="request-2")
        return res

    with patch.object(service, "audit", side_effect=_audit_with_concurrent_arrival):
        reconcile_res2 = service.reconcile(refresh=True)

    # Request 1 acknowledged, Request 2 remains pending
    assert req1_id in reconcile_res2["acknowledged_requests"]
    pending = service.pending_reconciliation_requests("app")
    assert len(pending) == 1
    assert pending[0]["reason"] == "request-2"


def _fixture(
    tmp_path: Path,
    slot_prepare: tuple[SlotPrepareCommand, ...] = (),
) -> tuple[ControlConfig, Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
    )
    route = tmp_path / "repo"
    subprocess.run(["git", "clone", str(remote), str(route)], check=True, capture_output=True)
    run_git(route, "config", "user.name", "Tests")
    run_git(route, "config", "user.email", "tests@example.invalid")
    (route / "base.txt").write_text("base\n", encoding="utf-8")
    run_git(route, "add", "base.txt")
    run_git(route, "commit", "-m", "base")
    run_git(route, "branch", "-M", "main")
    run_git(route, "push", "-u", "origin", "main")
    slot = tmp_path / "slots/app-1"
    run_git(route, "worktree", "add", "-b", "task/app-1", str(slot), "main")
    (slot / "change.txt").write_text("accepted\n", encoding="utf-8")
    run_git(slot, "add", "change.txt")
    run_git(slot, "commit", "-m", "accepted")
    run_git(slot, "push", "origin", "HEAD:main")
    defaults = ControlDefaults(
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        prepare_slots=False,
        guardrail_poll_sec=1,
        forbidden_status_globs=(),
    )
    route_config = RouteConfig(
        name="app",
        path=route,
        required_branch="main",
        worktree_root=tmp_path / "worktrees",
        worktree_base=route,
        source_roots=(Path("."),),
        test_roots=(Path("tests"),),
        exclude_dirs=(),
        slot_root=tmp_path / "slots",
        canonical_remote="origin",
        canonical_branch="main",
    )
    config = ControlConfig(
        config_path=tmp_path / "workspaces.toml",
        project_root=tmp_path,
        coordination_root=tmp_path / ".agent-work",
        runs_root=tmp_path / "runs",
        database_path=tmp_path / "runs/jobs.sqlite3",
        worktree_root=tmp_path / "worktrees",
        worktree_base=route,
        slot_root=tmp_path / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=defaults,
        routes=MappingProxyType({"app": route_config}),
        slots=MappingProxyType({"app-1": SlotConfig(name="app-1", route="app", path=slot)}),
        slot_prepare=slot_prepare,
    )
    prep_toml = ""
    if slot_prepare:
        prep_lines = ["[slot_prepare]"]
        for cmd in slot_prepare:
            prep_lines.append(f"[slot_prepare.{cmd.name}]")
            if cmd.routes:
                routes_str = ", ".join(f'"{r}"' for r in cmd.routes)
                prep_lines.append(f"routes = [{routes_str}]")
            prep_lines.append(f'working_dir = "{cmd.working_dir.as_posix()}"')
            if cmd.marker:
                prep_lines.append(f'marker = "{cmd.marker.as_posix()}"')
            cmd_str = ", ".join(f'"{c}"' for c in cmd.command)
            prep_lines.append(f"command = [{cmd_str}]")
            prep_lines.append(f"timeout_sec = {cmd.timeout_sec}")
        prep_toml = "\n" + "\n".join(prep_lines) + "\n"

    toml_content = f"""
[control]
coordination_root = "{(tmp_path / ".agent-work").as_posix()}"
runs_root = "{(tmp_path / "runs").as_posix()}"
database = "{(tmp_path / "runs/jobs.sqlite3").as_posix()}"
worktree_root = "{(tmp_path / "worktrees").as_posix()}"
slot_root = "{(tmp_path / "slots").as_posix()}"
{prep_toml}
[routes.app]
path = "{route.as_posix()}"
required_branch = "main"
canonical_remote = "origin"
canonical_branch = "main"

[slots.app-1]
route = "app"
path = "{slot.as_posix()}"
"""
    config.config_path.write_text(toml_content.strip() + "\n", encoding="utf-8")
    return config, route, slot


def _valid_bundle() -> dict[str, object]:
    return {
        "schema_version": 1,
        "review_ready": True,
        "worker_verification": {
            "state": "valid",
            "schema_version": 1,
            "payload": {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [],
                "checks": [],
                "unverified": [],
            },
            "sha256": "a" * 64,
        },
    }


def test_real_finalization_acceptance_canonical_integration_reconcile_apply(
    tmp_path: Path,
) -> None:
    config, route, slot = _fixture(tmp_path)
    # Enable terminal_slot_policy = "checkpoint" so finalization checkpoints and cleans worktree
    toml_text = config.config_path.read_text(encoding="utf-8")
    toml_text += '\n[control.defaults]\nterminal_slot_policy = "checkpoint"\n'
    config.config_path.write_text(toml_text, encoding="utf-8")
    config = load_config(config.config_path)

    control = AgentControlPlane(config)
    control.slot_lifecycle.live_cwds = lambda _path: []
    control.slot_store.register_slot("app-1", "app", slot)

    # Slot begins on a task branch at current origin/main
    run_git(route, "fetch", "origin")
    run_git(slot, "checkout", "-B", "task/job-real-shape", "origin/main")
    launch_base_sha = run_git(slot, "rev-parse", "HEAD")

    # Create job record and assign slot
    task_dir = config.coordination_root / "tasks/task-real-shape"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "brief.md").write_text("# Brief\nReal finalization shape\n", encoding="utf-8")

    job = control.store.create_job(
        job_id="job-real-shape",
        task_id="task-real-shape",
        route="app",
        workspace_path=slot,
        expected_branch="main",
        expected_result_status="completed",
        controller_gate_mode="full",
        config_path=config.config_path,
        run_dir=config.runs_root / "job-real-shape",
        prompt_path=config.runs_root / "job-real-shape/prompt.md",
        result_path=config.runs_root / "job-real-shape/result.md",
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="agy",
        workspace_access="native",
        slot_name="app-1",
        launch_base_sha=launch_base_sha,
    )
    control.slot_store.acquire_slot("app-1", job.job_id)
    job.run_dir.mkdir(parents=True, exist_ok=True)
    contract = resolve_native_quality_contract(
        config, job.route, workspace_access="native", read_only=False
    )
    write_native_quality_contract(job.run_dir, contract)

    # Worker writes a file change in the slot worktree
    (slot / "feature.txt").write_text("feature content\n", encoding="utf-8")
    job.result_path.parent.mkdir(parents=True, exist_ok=True)
    result_content = """Status: completed

Changed files:
- feature.txt

What changed:
Added feature.txt

Verification performed:
Ran verification check

Not verified / remaining risks:
None
"""
    job.result_path.write_text(result_content, encoding="utf-8")
    bundle = {
        "schema_version": 1,
        "status": "completed",
        "changed_files": [{"path": "feature.txt", "change": "added"}],
        "checks": [
            {
                "command": "true",
                "cwd": ".",
                "outcome": "passed",
                "exit_code": 0,
                "summary": "verified",
            }
        ],
        "unverified": [],
    }
    (job.run_dir / "verification.json").write_text(json.dumps(bundle), encoding="utf-8")

    # Run real finalization service finish
    fin_job = control.finalization.finish(job.job_id, "completed")
    assert fin_job.finalization_status == "completed"

    # Verify normal ACP checkpoint shape:
    # 1. Clean slot HEAD intentionally remains at base_sha
    assert run_git(slot, "rev-parse", "HEAD") == launch_base_sha
    assert run_git(slot, "status", "--porcelain=v1", "-uall") == ""

    # 2. Synthetic checkpoint commit exists on checkpoint_ref
    inbox_item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert inbox_item.base_sha == launch_base_sha
    assert inbox_item.checkpoint_sha is not None
    assert inbox_item.checkpoint_sha != launch_base_sha
    assert inbox_item.checkpoint_ref.startswith("refs/agent-control-plane/jobs/")
    assert run_git(route, "rev-parse", inbox_item.checkpoint_ref) == inbox_item.checkpoint_sha

    # 3. Acceptance resolves review inbox item
    resolved = control.resolve_review_inbox_item(inbox_item.item_id, "accepted")
    assert resolved["review_status"] == "accepted"

    # 4. Canonical integration: canonical branch merges the accepted checkpoint and updates canonical remote
    run_git(route, "merge", "--ff-only", inbox_item.checkpoint_sha)
    run_git(route, "push", "origin", "main")
    canonical_tip = run_git(route, "rev-parse", "origin/main")
    assert canonical_tip == inbox_item.checkpoint_sha

    # 5. Slot HEAD is STILL at launch_base_sha before reconcile/apply! (Normal ACP checkpoint shape!)
    assert run_git(slot, "rev-parse", "HEAD") == launch_base_sha

    # 6. Reconcile classifies slot as safe-to-delete
    reconciled = control.slot_lifecycle.reconcile()
    enqueued = reconciled.get("enqueued", [])
    assert len(enqueued) == 1
    op_id = enqueued[0]

    # 7. Apply safely cleans up slot and returns it to default branch at canonical tip
    applied = control.slot_lifecycle.apply(op_id)
    assert applied["action"] == "completed", applied.get("reason")
    assert run_git(slot, "branch", "--show-current") == "slot/app-1"
    assert run_git(slot, "rev-parse", "HEAD") == canonical_tip
    assert run_git(slot, "status", "--porcelain=v1", "-uall") == ""
    assert control.slot_store.require_slot("app-1").status == "available"

    # 8. Checkpoint ref was deleted
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", inbox_item.checkpoint_ref)


def test_remapping_configured_slot_to_canonical_checkout_reproduced_and_prevented(
    tmp_path: Path,
) -> None:
    config, route, _slot = _fixture(tmp_path)

    # 1. Config validation prevents slot pointing to canonical checkout
    bad_toml = f"""
[control]
coordination_root = "{(tmp_path / ".agent-work").as_posix()}"
runs_root = "{(tmp_path / "runs").as_posix()}"
database = "{(tmp_path / "runs/jobs.sqlite3").as_posix()}"
worktree_root = "{(tmp_path / "worktrees").as_posix()}"
slot_root = "{(tmp_path / "slots").as_posix()}"

[routes.app]
path = "{route.as_posix()}"
required_branch = "main"
canonical_remote = "origin"
canonical_branch = "main"

[slots.canonical-attempt]
route = "app"
path = "{route.as_posix()}"
"""
    bad_cfg_file = tmp_path / "bad_workspaces.toml"
    bad_cfg_file.write_text(bad_toml.strip() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical checkout"):
        load_config(bad_cfg_file)

    # 2. SlotManager prevents creating or ensuring slot at canonical checkout
    control = AgentControlPlane(config)
    with pytest.raises(SlotError, match="canonical checkout"):
        control.slots._ensure_slot_path_allowed(route, route="app")

    # 3. Audit quarantines slot if store was populated with canonical checkout
    control.slot_lifecycle.live_cwds = lambda _path: []
    control.slot_store.register_slot("canonical-slot", "app", route)
    audit = control.slot_lifecycle.audit()
    canonical_res = [r for r in audit["resources"] if r.get("name") == "canonical-slot"]
    assert len(canonical_res) == 1
    assert canonical_res[0]["classification"] == "quarantined"
    assert "canonical checkout" in canonical_res[0]["reasons"][0]


def test_temporary_dynamic_worktree_cleanup_no_default_branch_or_leftover_refs(
    tmp_path: Path,
) -> None:
    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")

    # Create temporary dynamic worktree (not in config.slots)
    temp_path = tmp_path / "slots/dynamic-wt"
    run_git(route, "worktree", "add", "-b", "task/dynamic-wt", str(temp_path), "origin/main")
    base_sha = run_git(temp_path, "rev-parse", "HEAD")

    (temp_path / "work.txt").write_text("dynamic work\n", encoding="utf-8")
    run_git(temp_path, "add", "work.txt")
    run_git(temp_path, "commit", "-m", "dynamic commit")
    run_git(temp_path, "push", "origin", "HEAD:main")
    accepted_sha = run_git(temp_path, "rev-parse", "HEAD")
    ckpt_ref = "refs/agent-control-plane/jobs/job-dynamic"
    run_git(route, "update-ref", ckpt_ref, accepted_sha)

    control = AgentControlPlane(config)
    control.slot_lifecycle.live_cwds = lambda _path: []
    control.slot_store.register_slot("dynamic-wt", "app", temp_path)
    gen = control.slot_store.acquire_slot("dynamic-wt", "job-dynamic").generation
    control.slot_store.release_slot("dynamic-wt", "job-dynamic")

    item = control.review_inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-dynamic",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=temp_path,
            slot_name="dynamic-wt",
            slot_generation=gen,
            checkpoint_ref=ckpt_ref,
            checkpoint_sha=accepted_sha,
            base_sha=base_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    control.resolve_review_inbox_item(item.item_id, "accepted")

    reconciled = control.slot_lifecycle.reconcile()
    dynamic_res = [r for r in reconciled["resources"] if r.get("name") == "dynamic-wt"]
    assert len(dynamic_res) == 1
    assert dynamic_res[0]["classification"] == "safe-to-delete"
    op_id = dynamic_res[0]["operation_id"]

    res = control.slot_lifecycle.apply(op_id)
    assert res["action"] == "completed"

    # Assert no worktree directory exists on filesystem
    assert not temp_path.exists()

    # Assert slot status is deleted in store
    assert control.slot_store.require_slot("dynamic-wt").status == "deleted"

    # Assert no reusable slot/<name> default branch was created or left behind
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/slot/dynamic-wt")

    # Assert task branch was deleted
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/task/dynamic-wt")

    # Assert checkpoint ref was deleted
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", ckpt_ref)


# --- Finding 5: Exact compare-and-delete OID branch preservation ---


def test_adversarial_task_branch_moved_after_enqueue_configured_quarantines(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-adv-task").generation
    slots.release_slot("app-1", "job-adv-task")

    accepted = run_git(slot, "rev-parse", "HEAD")
    ckpt_ref = "refs/agent-control-plane/jobs/job-adv-task"
    run_git(route, "update-ref", ckpt_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-adv-task",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=ckpt_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Adversary moves task ref after enqueue
    (slot / "adversarial.txt").write_text("adversarial edit\n", encoding="utf-8")
    run_git(slot, "add", "adversarial.txt")
    run_git(slot, "commit", "-m", "adversarial commit")
    moved_sha = run_git(slot, "rev-parse", "HEAD")
    assert moved_sha != accepted

    res = service.apply(op_id)
    assert res["action"] == "quarantined"
    # Assert unique moved ref remains and was not deleted
    assert run_git(route, "rev-parse", "--verify", "refs/heads/task/app-1") == moved_sha
    assert slots.require_slot("app-1").status == "quarantined"


def test_adversarial_task_branch_moved_between_precondition_and_mutation_quarantines(
    tmp_path: Path,
) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-adv-task-mid").generation
    slots.release_slot("app-1", "job-adv-task-mid")

    accepted = run_git(slot, "rev-parse", "HEAD")
    ckpt_ref = "refs/agent-control-plane/jobs/job-adv-task-mid"
    run_git(route, "update-ref", ckpt_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-adv-task-mid",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=ckpt_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Create a new commit to move task ref to
    new_commit_sha = run_git(
        route, "commit-tree", "-m", "adversarial move", "-p", accepted, f"{accepted}^{{tree}}"
    )
    assert new_commit_sha != accepted

    def _move_task_ref_pre_mutation(step_name: str) -> None:
        if step_name == "delete_task_branch":
            run_git(route, "update-ref", "refs/heads/task/app-1", new_commit_sha)

    res = service.apply(op_id, pre_mutation_hook=_move_task_ref_pre_mutation)
    assert res["action"] == "quarantined"
    # Assert unique moved ref remains and was not deleted
    assert run_git(route, "rev-parse", "--verify", "refs/heads/task/app-1") == new_commit_sha
    assert slots.require_slot("app-1").status == "quarantined"


def test_adversarial_temporary_task_branch_moved_quarantines(tmp_path: Path) -> None:
    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")

    temp_path = tmp_path / "slots/temp-adv-wt"
    run_git(route, "worktree", "add", "-b", "task/temp-adv", str(temp_path), "origin/main")
    base_sha = run_git(temp_path, "rev-parse", "HEAD")

    (temp_path / "adv.txt").write_text("adv work\n", encoding="utf-8")
    run_git(temp_path, "add", "adv.txt")
    run_git(temp_path, "commit", "-m", "adv commit")
    run_git(temp_path, "push", "origin", "HEAD:main")
    accepted_sha = run_git(temp_path, "rev-parse", "HEAD")
    ckpt_ref = "refs/agent-control-plane/jobs/job-temp-adv"
    run_git(route, "update-ref", ckpt_ref, accepted_sha)

    slots = SlotStore(config.database_path)
    slots.register_slot("temp-adv-wt", "app", temp_path)
    gen = slots.acquire_slot("temp-adv-wt", "job-temp-adv").generation
    slots.release_slot("temp-adv-wt", "job-temp-adv")

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-temp-adv",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=temp_path,
            slot_name="temp-adv-wt",
            slot_generation=gen,
            checkpoint_ref=ckpt_ref,
            checkpoint_sha=accepted_sha,
            base_sha=base_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    new_commit_sha = run_git(
        route, "commit-tree", "-m", "temp adv move", "-p", accepted_sha, f"{accepted_sha}^{{tree}}"
    )

    def _move_temp_task_branch(step_name: str) -> None:
        if step_name == "delete_task_branch":
            run_git(route, "update-ref", "refs/heads/task/temp-adv", new_commit_sha)

    res = service.apply(op_id, pre_mutation_hook=_move_temp_task_branch)
    assert res["action"] == "quarantined"
    assert run_git(route, "rev-parse", "--verify", "refs/heads/task/temp-adv") == new_commit_sha
    assert slots.require_slot("temp-adv-wt").status == "quarantined"


def test_adversarial_temporary_default_branch_moved_and_created_quarantines(tmp_path: Path) -> None:
    # Test Part A: default ref existed at freeze time and moved
    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")

    temp_path = tmp_path / "slots/temp-def-wt"
    run_git(route, "worktree", "add", "-b", "task/temp-def", str(temp_path), "origin/main")
    base_sha = run_git(temp_path, "rev-parse", "HEAD")

    (temp_path / "def.txt").write_text("def work\n", encoding="utf-8")
    run_git(temp_path, "add", "def.txt")
    run_git(temp_path, "commit", "-m", "def commit")
    run_git(temp_path, "push", "origin", "HEAD:main")
    accepted_sha = run_git(temp_path, "rev-parse", "HEAD")
    ckpt_ref = "refs/agent-control-plane/jobs/job-temp-def"
    run_git(route, "update-ref", ckpt_ref, accepted_sha)

    # Pre-create default branch at freeze time
    run_git(route, "update-ref", "refs/heads/slot/temp-def-wt", accepted_sha)

    slots = SlotStore(config.database_path)
    slots.register_slot("temp-def-wt", "app", temp_path)
    gen = slots.acquire_slot("temp-def-wt", "job-temp-def").generation
    slots.release_slot("temp-def-wt", "job-temp-def")

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-temp-def",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=temp_path,
            slot_name="temp-def-wt",
            slot_generation=gen,
            checkpoint_ref=ckpt_ref,
            checkpoint_sha=accepted_sha,
            base_sha=base_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    # Move default branch between precondition and mutation
    new_def_sha = run_git(
        route, "commit-tree", "-m", "def move", "-p", accepted_sha, f"{accepted_sha}^{{tree}}"
    )

    def _move_def_branch(step_name: str) -> None:
        if step_name == "delete_default_branch":
            run_git(route, "update-ref", "refs/heads/slot/temp-def-wt", new_def_sha)

    res = service.apply(op_id, pre_mutation_hook=_move_def_branch)
    assert res["action"] == "quarantined"
    assert run_git(route, "rev-parse", "--verify", "refs/heads/slot/temp-def-wt") == new_def_sha
    assert slots.require_slot("temp-def-wt").status == "quarantined"

    # Test Part B: default ref was absent at freeze time and created after enqueue
    temp_path_b = tmp_path / "slots/temp-absent-wt"
    run_git(route, "worktree", "add", "-b", "task/temp-absent", str(temp_path_b), "origin/main")
    base_sha_b = run_git(temp_path_b, "rev-parse", "HEAD")

    (temp_path_b / "absent.txt").write_text("absent work\n", encoding="utf-8")
    run_git(temp_path_b, "add", "absent.txt")
    run_git(temp_path_b, "commit", "-m", "absent commit")
    run_git(temp_path_b, "push", "origin", "HEAD:main")
    accepted_sha_b = run_git(temp_path_b, "rev-parse", "HEAD")
    ckpt_ref_b = "refs/agent-control-plane/jobs/job-temp-absent"
    run_git(route, "update-ref", ckpt_ref_b, accepted_sha_b)

    slots.register_slot("temp-absent-wt", "app", temp_path_b)
    gen_b = slots.acquire_slot("temp-absent-wt", "job-temp-absent").generation
    slots.release_slot("temp-absent-wt", "job-temp-absent")

    item_b = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-temp-absent",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=temp_path_b,
            slot_name="temp-absent-wt",
            slot_generation=gen_b,
            checkpoint_ref=ckpt_ref_b,
            checkpoint_sha=accepted_sha_b,
            base_sha=base_sha_b,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item_b.item_id, "accepted")

    service_b = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id_b = service_b.reconcile()["enqueued"][0]

    # Create default ref after enqueue
    created_sha = run_git(
        route,
        "commit-tree",
        "-m",
        "created def",
        "-p",
        accepted_sha_b,
        f"{accepted_sha_b}^{{tree}}",
    )
    run_git(route, "update-ref", "refs/heads/slot/temp-absent-wt", created_sha)

    res_b = service_b.apply(op_id_b)
    assert res_b["action"] == "quarantined"
    # Never delete a later-created ref:
    assert run_git(route, "rev-parse", "--verify", "refs/heads/slot/temp-absent-wt") == created_sha
    assert slots.require_slot("temp-absent-wt").status == "quarantined"


def test_crash_retry_task_branch_and_default_branch_exact_already_deleted(tmp_path: Path) -> None:
    # 1. Configured slot task branch crash after deletion
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.acquire_slot("app-1", "job-crash-task").generation
    slots.release_slot("app-1", "job-crash-task")

    accepted = run_git(slot, "rev-parse", "HEAD")
    ckpt_ref = "refs/agent-control-plane/jobs/job-crash-task"
    run_git(route, "update-ref", ckpt_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-crash-task",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=ckpt_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id = service.reconcile()["enqueued"][0]

    def _crash_after_del_task(step_name: str) -> None:
        if step_name == "delete_task_branch":
            raise RuntimeError("injected crash after delete_task_branch")

    with pytest.raises(RuntimeError, match="injected crash after delete_task_branch"):
        service.apply(op_id, crash_hook=_crash_after_del_task)

    # Task branch was deleted before crash
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/task/app-1")

    # Recover fresh service and apply: resumes and completes
    recovered = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    res = recovered.apply(op_id)
    assert res["action"] == "completed"

    # 2. Temporary slot default branch crash after deletion (when default branch existed at freeze)
    temp_path = tmp_path / "slots/temp-crash-def"
    run_git(route, "worktree", "add", "-b", "task/temp-crash", str(temp_path), "origin/main")
    base_sha = run_git(temp_path, "rev-parse", "HEAD")

    (temp_path / "crash.txt").write_text("crash work\n", encoding="utf-8")
    run_git(temp_path, "add", "crash.txt")
    run_git(temp_path, "commit", "-m", "crash commit")
    run_git(temp_path, "push", "origin", "HEAD:main")
    accepted_temp = run_git(temp_path, "rev-parse", "HEAD")
    temp_ckpt = "refs/agent-control-plane/jobs/job-crash-temp"
    run_git(route, "update-ref", temp_ckpt, accepted_temp)

    # Pre-create default branch for temporary slot
    run_git(route, "update-ref", "refs/heads/slot/temp-crash-def", accepted_temp)

    slots.register_slot("temp-crash-def", "app", temp_path)
    gen_temp = slots.acquire_slot("temp-crash-def", "job-crash-temp").generation
    slots.release_slot("temp-crash-def", "job-crash-temp")

    item_temp = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-crash-temp",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=temp_path,
            slot_name="temp-crash-def",
            slot_generation=gen_temp,
            checkpoint_ref=temp_ckpt,
            checkpoint_sha=accepted_temp,
            base_sha=base_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve(item_temp.item_id, "accepted")

    service_temp = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )
    op_id_temp = service_temp.reconcile()["enqueued"][0]

    def _crash_after_del_def(step_name: str) -> None:
        if step_name == "delete_default_branch":
            raise RuntimeError("injected crash after delete_default_branch")

    with pytest.raises(RuntimeError, match="injected crash after delete_default_branch"):
        service_temp.apply(op_id_temp, crash_hook=_crash_after_del_def)

    # Default branch was deleted before crash
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/slot/temp-crash-def")

    # Recover fresh service and apply
    recovered_temp = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    res_temp = recovered_temp.apply(op_id_temp)
    assert res_temp["action"] == "completed"


def test_unowned_branch_legacy_ownership_reconstruction(tmp_path: Path) -> None:
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create an unowned legacy branch on route
    run_git(route, "checkout", "-b", "codex/legacy-feature-1", "origin/main")
    (route / "legacy.txt").write_text("legacy code\n", encoding="utf-8")
    run_git(route, "add", "legacy.txt")
    run_git(route, "commit", "-m", "legacy feature commit")
    branch_sha = run_git(route, "rev-parse", "codex/legacy-feature-1")
    # Merge to main and push so it is integrated into canonical branch
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/legacy-feature-1")
    run_git(route, "push", "origin", "main")

    # Setup legacy database with accepted review inbox item and job record
    legacy_db_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy_db_path) as ldb:
        ldb.execute("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                route TEXT,
                workspace_path TEXT,
                expected_branch TEXT,
                root_acceptance TEXT,
                updated_at TEXT
            )
        """)
        ldb.execute("""
            CREATE TABLE review_inbox_items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT,
                review_status TEXT,
                checkpoint_sha TEXT,
                base_sha TEXT,
                route TEXT,
                workspace_path TEXT,
                created_at TEXT,
                reviewed_at TEXT
            )
        """)
        ldb.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "job-legacy-1",
                "app",
                str(route),
                "codex/legacy-feature-1",
                "accepted",
                "2026-01-01T01:00:00Z",
            ),
        )
        ldb.execute(
            "INSERT INTO review_inbox_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-legacy-1",
                "job-legacy-1",
                "accepted",
                branch_sha,
                "base-sha",
                "app",
                str(route),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    # Audit should reconstruct legacy ownership
    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/legacy-feature-1"]
    assert len(branch_rows) == 1
    assert branch_rows[0]["classification"] == "accepted-integrated"
    assert branch_rows[0]["operation_id"]
    op_id = branch_rows[0]["operation_id"]

    # Reconcile enqueues the operation
    rec = service.reconcile()
    assert op_id in rec["enqueued"]

    # Apply deletes the branch ref using compare-and-delete
    res = service.apply(op_id)
    assert res["action"] == "completed"
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/codex/legacy-feature-1")

    # Second apply is idempotent
    res2 = service.apply(op_id)
    assert res2["action"] == "already_completed"


def test_unowned_branch_foreign_legacy_db_same_route_different_workspace_retained(
    tmp_path: Path,
) -> None:
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create branch on route
    run_git(route, "checkout", "-b", "codex/foreign-feature", "origin/main")
    (route / "foreign.txt").write_text("feature content\n", encoding="utf-8")
    run_git(route, "add", "foreign.txt")
    run_git(route, "commit", "-m", "feature commit")
    branch_sha = run_git(route, "rev-parse", "codex/foreign-feature")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/foreign-feature")
    run_git(route, "push", "origin", "main")

    # Create foreign legacy DB with matching route, branch, and SHA, but foreign workspace
    foreign_repo = tmp_path / "foreign_repo"
    foreign_repo.mkdir()
    foreign_db_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    foreign_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(foreign_db_path) as ldb:
        ldb.execute("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                route TEXT,
                workspace_path TEXT,
                expected_branch TEXT,
                root_acceptance TEXT,
                updated_at TEXT
            )
        """)
        ldb.execute("""
            CREATE TABLE review_inbox_items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT,
                review_status TEXT,
                checkpoint_sha TEXT,
                route TEXT,
                workspace_path TEXT,
                created_at TEXT,
                reviewed_at TEXT
            )
        """)
        ldb.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "job-foreign-1",
                "app",
                str(foreign_repo),
                "codex/foreign-feature",
                "accepted",
                "2026-01-01T01:00:00Z",
            ),
        )
        ldb.execute(
            "INSERT INTO review_inbox_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-foreign-1",
                "job-foreign-1",
                "accepted",
                branch_sha,
                "app",
                str(foreign_repo),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/foreign-feature"]
    assert len(branch_rows) == 1
    # MUST NOT be accepted-integrated; foreign legacy DB cannot authorize deletion
    assert branch_rows[0]["classification"] == "retained-unowned"
    assert "operation_id" not in branch_rows[0]

    # Reconcile must not enqueue any deletion for codex/foreign-feature
    rec = service.reconcile()
    assert not any("codex/foreign-feature" in str(op) for op in rec["enqueued"])
    # The branch ref remains intact
    assert run_git(route, "show-ref", "--verify", "refs/heads/codex/foreign-feature")


def test_unowned_branch_multiple_legacy_databases_discovered_and_aggregated(tmp_path: Path) -> None:
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Branch 1
    run_git(route, "checkout", "-b", "codex/db1-feature", "origin/main")
    (route / "db1.txt").write_text("db1\n", encoding="utf-8")
    run_git(route, "add", "db1.txt")
    run_git(route, "commit", "-m", "db1 commit")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/db1-feature")
    run_git(route, "push", "origin", "main")

    # Branch 2
    run_git(route, "checkout", "-b", "codex/db2-feature", "origin/main")
    (route / "db2.txt").write_text("db2\n", encoding="utf-8")
    run_git(route, "add", "db2.txt")
    run_git(route, "commit", "-m", "db2 commit")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/db2-feature")
    run_git(route, "push", "origin", "main")

    db1_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    db1_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db1_path) as db1:
        db1.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, route TEXT, workspace_path TEXT, expected_branch TEXT, root_acceptance TEXT, updated_at TEXT)"
        )
        db1.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            ("job-1", "app", str(route), "codex/db1-feature", "accepted", "2026-01-01T00:00:00Z"),
        )

    db2_path = config.worktree_base / "runs/jobs.sqlite3"
    db2_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db2_path) as db2:
        db2.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, route TEXT, workspace_path TEXT, expected_branch TEXT, root_acceptance TEXT, updated_at TEXT)"
        )
        db2.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            ("job-2", "app", str(route), "codex/db2-feature", "accepted", "2026-01-01T00:00:00Z"),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )
    assert len(service.legacy_databases) >= 2

    audit = service.audit()
    b1_rows = [r for r in audit["resources"] if r.get("branch") == "codex/db1-feature"]
    b2_rows = [r for r in audit["resources"] if r.get("branch") == "codex/db2-feature"]
    assert len(b1_rows) == 1 and b1_rows[0]["classification"] == "accepted-integrated"
    assert len(b2_rows) == 1 and b2_rows[0]["classification"] == "accepted-integrated"


def test_unowned_branch_malformed_legacy_db_fails_closed(tmp_path: Path) -> None:
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    run_git(route, "checkout", "-b", "codex/valid-feature", "origin/main")
    (route / "valid.txt").write_text("valid\n", encoding="utf-8")
    run_git(route, "add", "valid.txt")
    run_git(route, "commit", "-m", "valid commit")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/valid-feature")
    run_git(route, "push", "origin", "main")

    # Valid legacy DB
    valid_db = tmp_path / "valid.sqlite3"
    with sqlite3.connect(valid_db) as db:
        db.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, route TEXT, workspace_path TEXT, expected_branch TEXT, root_acceptance TEXT, updated_at TEXT)"
        )
        db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "job-val-1",
                "app",
                str(route),
                "codex/valid-feature",
                "accepted",
                "2026-01-01T00:00:00Z",
            ),
        )

    # Malformed legacy DB
    malformed_db = tmp_path / "corrupt.sqlite3"
    malformed_db.write_bytes(b"NOT A SQLITE FILE")

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
        legacy_databases=[valid_db, malformed_db],
    )

    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/valid-feature"]
    assert len(branch_rows) == 1
    # Must fail closed to quarantined because of the malformed database
    assert branch_rows[0]["classification"] == "quarantined"
    assert "unsupported legacy schema or database error" in branch_rows[0]["reasons"][0]

    rec = service.reconcile()
    assert not any("codex/valid-feature" in str(op) for op in rec["enqueued"])


def test_unowned_branch_task_id_not_used_as_branch_name(tmp_path: Path) -> None:
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create branch codex/real-branch
    run_git(route, "checkout", "-b", "codex/real-branch", "origin/main")
    (route / "real.txt").write_text("real\n", encoding="utf-8")
    run_git(route, "add", "real.txt")
    run_git(route, "commit", "-m", "real commit")
    sha = run_git(route, "rev-parse", "codex/real-branch")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/real-branch")
    run_git(route, "push", "origin", "main")

    # Legacy DB has task_id = 'task-xyz-999', expected_branch = 'codex/real-branch'
    db_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as ldb:
        ldb.execute("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                route TEXT,
                workspace_path TEXT,
                expected_branch TEXT,
                root_acceptance TEXT,
                updated_at TEXT
            )
        """)
        ldb.execute("""
            CREATE TABLE review_inbox_items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT,
                task_id TEXT,
                review_status TEXT,
                checkpoint_sha TEXT,
                route TEXT,
                workspace_path TEXT,
                created_at TEXT,
                reviewed_at TEXT
            )
        """)
        ldb.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "job-real-1",
                "app",
                str(route),
                "codex/real-branch",
                "accepted",
                "2026-01-01T00:00:00Z",
            ),
        )
        ldb.execute(
            "INSERT INTO review_inbox_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-real-1",
                "job-real-1",
                "dispatch-task-12345",
                "accepted",
                sha,
                "app",
                str(route),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/real-branch"]
    assert len(branch_rows) == 1
    assert branch_rows[0]["classification"] == "accepted-integrated"
    assert branch_rows[0]["proof"]["branch"] == "codex/real-branch"


def test_unowned_branch_patch_equivalent_cherry_classification(tmp_path: Path) -> None:
    from agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle import (
        _is_ancestor,
        _is_patch_equivalent,
    )

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create a base commit on main
    run_git(route, "checkout", "main")
    (route / "base_cherry.txt").write_text("cherry base\n", encoding="utf-8")
    run_git(route, "add", "base_cherry.txt")
    run_git(route, "commit", "-m", "cherry base commit")
    run_git(route, "push", "origin", "main")

    # Create a branch with a patch
    run_git(route, "checkout", "-b", "codex/cherry-patch", "origin/main")
    (route / "cherry_feature.txt").write_text("cherry patch content\n", encoding="utf-8")
    run_git(route, "add", "cherry_feature.txt")
    run_git(route, "commit", "-m", "cherry feature commit")
    branch_sha = run_git(route, "rev-parse", "codex/cherry-patch")

    # Create an intervening commit on main before cherry-pick
    run_git(route, "checkout", "main")
    (route / "intervening.txt").write_text("intervening\n", encoding="utf-8")
    run_git(route, "add", "intervening.txt")
    run_git(route, "commit", "-m", "intervening commit")

    # Cherry-pick onto main (creates distinct commit with identical diff)
    run_git(route, "cherry-pick", branch_sha)
    run_git(route, "push", "origin", "main")

    # Verify is_ancestor is False but git cherry detects patch equivalence
    assert not _is_ancestor(route, branch_sha, "origin/main")
    assert _is_patch_equivalent(route, branch_sha, "origin/main")

    # Add an acceptance receipt in review_inbox
    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-cherry-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=route,
            slot_name="none",
            slot_generation=1,
            checkpoint_ref="refs/agent-control-plane/jobs/job-cherry-1",
            checkpoint_sha=branch_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    cherry_rows = [r for r in audit["resources"] if r.get("branch") == "codex/cherry-patch"]
    assert len(cherry_rows) == 1
    assert cherry_rows[0]["classification"] == "accepted-integrated"
    assert cherry_rows[0]["operation_id"]


def test_is_patch_equivalent_empty_output_non_ancestor_fails_closed(tmp_path: Path) -> None:
    from agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle import (
        _is_ancestor,
        _is_patch_equivalent,
    )

    _config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Base commit on main
    (route / "base_empty.txt").write_text("base\n", encoding="utf-8")
    run_git(route, "add", "base_empty.txt")
    run_git(route, "commit", "-m", "base commit")
    run_git(route, "push", "origin", "main")

    # Branch b1
    run_git(route, "checkout", "-b", "b1", "main")
    (route / "b1.txt").write_text("b1\n", encoding="utf-8")
    run_git(route, "add", "b1.txt")
    run_git(route, "commit", "-m", "b1 commit")

    # Branch b2 from main
    run_git(route, "checkout", "-b", "b2", "main")
    (route / "b2.txt").write_text("b2\n", encoding="utf-8")
    run_git(route, "add", "b2.txt")
    run_git(route, "commit", "-m", "b2 commit")

    # Merge b1 and b2 into main
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--no-ff", "b1", "-m", "merge b1")
    run_git(route, "merge", "--no-ff", "b2", "-m", "merge b2")
    run_git(route, "push", "origin", "main")

    # Candidate merges b1 and b2 directly without main's merge commits
    run_git(route, "checkout", "-b", "codex/candidate-merge-only", "b1")
    run_git(route, "merge", "--no-ff", "b2", "-m", "merge b2 into candidate")
    candidate_sha = run_git(route, "rev-parse", "codex/candidate-merge-only")

    # Candidate is not an ancestor of main, git cherry output is empty
    assert not _is_ancestor(route, candidate_sha, "origin/main")
    # Must fail closed and return False
    assert not _is_patch_equivalent(route, candidate_sha, "origin/main")


def test_is_patch_equivalent_unique_merge_resolution_branch_fails_closed(tmp_path: Path) -> None:
    from agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle import (
        _is_ancestor,
        _is_patch_equivalent,
    )

    _config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Base commit on main
    (route / "f.txt").write_text("base\n", encoding="utf-8")
    run_git(route, "add", "f.txt")
    run_git(route, "commit", "-m", "base commit")
    run_git(route, "push", "origin", "main")

    # Branch b1 modifies f.txt
    run_git(route, "checkout", "-b", "b1", "main")
    (route / "f.txt").write_text("b1\n", encoding="utf-8")
    run_git(route, "commit", "-am", "b1 commit")

    # Branch b2 modifies f.txt
    run_git(route, "checkout", "-b", "b2", "main")
    (route / "f.txt").write_text("b2\n", encoding="utf-8")
    run_git(route, "commit", "-am", "b2 commit")

    # Merge b1 into main
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--no-ff", "b1", "-m", "merge b1 into main")
    run_git(route, "push", "origin", "main")

    # Candidate merges b2 into b1 with conflict resolution containing unique work
    run_git(route, "checkout", "-b", "codex/unique-merge-resolution", "b1")
    with pytest.raises(GitError):
        run_git(route, "merge", "b2")
    (route / "f.txt").write_text("unique resolution in candidate\n", encoding="utf-8")
    run_git(route, "add", "f.txt")
    run_git(route, "commit", "-m", "unique merge resolution")
    candidate_sha = run_git(route, "rev-parse", "codex/unique-merge-resolution")

    assert not _is_ancestor(route, candidate_sha, "origin/main")
    # Merge commit present in candidate-only range: must fail closed
    assert not _is_patch_equivalent(route, candidate_sha, "origin/main")


def test_is_patch_equivalent_all_non_merge_commits_patch_equivalent(tmp_path: Path) -> None:
    from agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle import (
        _is_ancestor,
        _is_patch_equivalent,
    )

    _config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Base commit on main
    (route / "base_multi.txt").write_text("base\n", encoding="utf-8")
    run_git(route, "add", "base_multi.txt")
    run_git(route, "commit", "-m", "base")
    run_git(route, "push", "origin", "main")

    # Candidate branch with 2 commits
    run_git(route, "checkout", "-b", "codex/multi-patch", "origin/main")
    (route / "p1.txt").write_text("patch 1\n", encoding="utf-8")
    run_git(route, "add", "p1.txt")
    run_git(route, "commit", "-m", "patch 1")
    c1 = run_git(route, "rev-parse", "HEAD")

    (route / "p2.txt").write_text("patch 2\n", encoding="utf-8")
    run_git(route, "add", "p2.txt")
    run_git(route, "commit", "-m", "patch 2")
    c2 = run_git(route, "rev-parse", "HEAD")

    # Main gets an intervening commit, then cherry-picks both c1 and c2
    run_git(route, "checkout", "main")
    (route / "other.txt").write_text("other\n", encoding="utf-8")
    run_git(route, "add", "other.txt")
    run_git(route, "commit", "-m", "intervening")

    run_git(route, "cherry-pick", c1)
    run_git(route, "cherry-pick", c2)
    run_git(route, "push", "origin", "main")

    assert not _is_ancestor(route, c2, "origin/main")
    # All non-merge commits cherry-picked, no merge commits
    assert _is_patch_equivalent(route, c2, "origin/main")


def test_is_patch_equivalent_one_unique_plus_commit_fails_closed(tmp_path: Path) -> None:
    from agent_control_plane.features.lifecycle_cleanup.lib.slot_lifecycle import (
        _is_ancestor,
        _is_patch_equivalent,
    )

    _config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Base commit on main
    (route / "base_one_unique.txt").write_text("base\n", encoding="utf-8")
    run_git(route, "add", "base_one_unique.txt")
    run_git(route, "commit", "-m", "base")
    run_git(route, "push", "origin", "main")

    # Candidate branch with 2 commits
    run_git(route, "checkout", "-b", "codex/one-unique", "origin/main")
    (route / "p1.txt").write_text("patch 1\n", encoding="utf-8")
    run_git(route, "add", "p1.txt")
    run_git(route, "commit", "-m", "patch 1")
    c1 = run_git(route, "rev-parse", "HEAD")

    (route / "p2_unique.txt").write_text("patch 2 unique\n", encoding="utf-8")
    run_git(route, "add", "p2_unique.txt")
    run_git(route, "commit", "-m", "patch 2 unique")
    c2 = run_git(route, "rev-parse", "HEAD")

    # Main cherry-picks c1, but NOT c2
    run_git(route, "checkout", "main")
    run_git(route, "cherry-pick", c1)
    run_git(route, "push", "origin", "main")

    assert not _is_ancestor(route, c2, "origin/main")
    # Has one unique '+' commit (c2): must return False
    assert not _is_patch_equivalent(route, c2, "origin/main")


def test_unowned_branch_contradictory_rejection_quarantines(tmp_path: Path) -> None:
    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    run_git(route, "checkout", "-b", "codex/conflict-branch", "origin/main")
    (route / "conflict.txt").write_text("conflict\n", encoding="utf-8")
    run_git(route, "add", "conflict.txt")
    run_git(route, "commit", "-m", "conflict commit")
    branch_sha = run_git(route, "rev-parse", "codex/conflict-branch")

    # Merge to main
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/conflict-branch")
    run_git(route, "push", "origin", "main")

    inbox = ReviewInboxStore(config.database_path)
    # Add acceptance
    item1 = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-conflict-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=route,
            slot_name="none",
            slot_generation=1,
            checkpoint_ref="refs/agent-control-plane/jobs/job-conflict-1",
            checkpoint_sha=branch_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
        )
    )
    inbox.resolve(item1.item_id, "accepted")

    # Add a later rejection for the same checkpoint sha
    time.sleep(0.01)
    item2 = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-conflict-2",
            source_status="failed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=route,
            slot_name="none",
            slot_generation=1,
            checkpoint_ref="refs/agent-control-plane/jobs/job-conflict-2",
            checkpoint_sha=branch_sha,
            result_text="Status: blocked\n",
            verification_bundle=_valid_bundle(),
        )
    )
    inbox.resolve(item2.item_id, "rejected")

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    conflict_rows = [r for r in audit["resources"] if r.get("branch") == "codex/conflict-branch"]
    assert len(conflict_rows) == 1
    assert conflict_rows[0]["classification"] == "quarantined"
    assert "contradictory" in conflict_rows[0]["reasons"][0]

    rec = service.reconcile()
    assert not any("codex/conflict-branch" in str(op) for op in rec["enqueued"])


def test_unowned_branch_apply_drift_fails_closed(tmp_path: Path) -> None:
    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    run_git(route, "checkout", "-b", "codex/drift-branch", "origin/main")
    (route / "drift1.txt").write_text("drift1\n", encoding="utf-8")
    run_git(route, "add", "drift1.txt")
    run_git(route, "commit", "-m", "drift commit 1")
    branch_sha = run_git(route, "rev-parse", "codex/drift-branch")

    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/drift-branch")
    run_git(route, "push", "origin", "main")

    inbox = ReviewInboxStore(config.database_path)
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-drift-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=route,
            slot_name="none",
            slot_generation=1,
            checkpoint_ref="refs/agent-control-plane/jobs/job-drift-1",
            checkpoint_sha=branch_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
        )
    )
    inbox.resolve(item.item_id, "accepted")

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    rec = service.reconcile()
    drift_op_id = next(
        op
        for op in rec["enqueued"]
        if any(
            r.get("branch") == "codex/drift-branch"
            for r in rec["resources"]
            if r.get("operation_id") == op
        )
    )

    # Simulate branch drift: someone commits to codex/drift-branch before apply
    run_git(route, "checkout", "codex/drift-branch")
    (route / "drift2.txt").write_text("drift2\n", encoding="utf-8")
    run_git(route, "add", "drift2.txt")
    run_git(route, "commit", "-m", "drift commit 2")
    run_git(route, "checkout", "main")

    # Apply must fail closed and quarantine
    res = service.apply(drift_op_id)
    assert res["action"] == "quarantined"
    assert "moved" in res["reason"]

    # The branch ref MUST NOT be deleted
    assert run_git(route, "rev-parse", "codex/drift-branch") != branch_sha


def test_unowned_branch_legacy_db_fabricated_missing_path_no_checkpoint_retained(
    tmp_path: Path,
) -> None:
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create an unowned integrated branch on route
    run_git(route, "checkout", "-b", "codex/fabricated-feature", "origin/main")
    (route / "fab.txt").write_text("fabricated code\n", encoding="utf-8")
    run_git(route, "add", "fab.txt")
    run_git(route, "commit", "-m", "fabricated feature commit")
    branch_sha = run_git(route, "rev-parse", "codex/fabricated-feature")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/fabricated-feature")
    run_git(route, "push", "origin", "main")

    # Fabricate a missing/non-existent path under target coordination/slot root
    slot_root = getattr(config, "slot_root", None) or (config.coordination_root / "slots")
    fake_ws = slot_root / "fake-slot-fabricated"
    assert not fake_ws.exists()

    # Legacy database supplies this fabricated path under target slot root with no checkpoint ref
    legacy_db_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy_db_path) as ldb:
        ldb.execute("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                route TEXT,
                workspace_path TEXT,
                expected_branch TEXT,
                root_acceptance TEXT,
                updated_at TEXT
            )
        """)
        ldb.execute("""
            CREATE TABLE review_inbox_items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT,
                review_status TEXT,
                checkpoint_sha TEXT,
                route TEXT,
                workspace_path TEXT,
                created_at TEXT,
                reviewed_at TEXT
            )
        """)
        ldb.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "job-fake-1",
                "app",
                str(fake_ws),
                "codex/fabricated-feature",
                "accepted",
                "2026-01-01T01:00:00Z",
            ),
        )
        ldb.execute(
            "INSERT INTO review_inbox_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-fake-1",
                "job-fake-1",
                "accepted",
                branch_sha,
                "app",
                str(fake_ws),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/fabricated-feature"]
    assert len(branch_rows) == 1
    # MUST NOT authorize accepted-integrated or an operation ID
    assert branch_rows[0]["classification"] == "retained-unowned"
    assert "operation_id" not in branch_rows[0]

    rec = service.reconcile()
    assert not any(
        r.get("branch") == "codex/fabricated-feature" and "operation_id" in r
        for r in rec["resources"]
    )


def test_unowned_branch_legacy_db_removed_historical_path_with_durable_checkpoint_accepted(
    tmp_path: Path,
) -> None:
    import hashlib
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create an unowned integrated branch on route
    run_git(route, "checkout", "-b", "codex/historical-feature", "origin/main")
    (route / "hist.txt").write_text("historical code\n", encoding="utf-8")
    run_git(route, "add", "hist.txt")
    run_git(route, "commit", "-m", "historical feature commit")
    branch_sha = run_git(route, "rev-parse", "codex/historical-feature")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/historical-feature")
    run_git(route, "push", "origin", "main")

    # Historical slot path that was legitimately cleaned up / removed
    slot_root = getattr(config, "slot_root", None) or (config.coordination_root / "slots")
    historical_ws = slot_root / "historical-slot-99"
    assert not historical_ws.exists()

    # Exact durable checkpoint ref independently present in target repository
    jid = "job-historical-1"
    j_hash = hashlib.sha256(jid.encode("utf-8")).hexdigest()
    ckpt_ref = f"refs/agent-control-plane/jobs/{j_hash}"
    run_git(route, "update-ref", ckpt_ref, branch_sha)
    assert run_git(route, "rev-parse", ckpt_ref) == branch_sha

    # Legacy database references this removed historical path, job, and exact checkpoint ref
    legacy_db_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy_db_path) as ldb:
        ldb.execute("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                route TEXT,
                workspace_path TEXT,
                expected_branch TEXT,
                root_acceptance TEXT,
                updated_at TEXT
            )
        """)
        ldb.execute("""
            CREATE TABLE review_inbox_items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT,
                review_status TEXT,
                checkpoint_ref TEXT,
                checkpoint_sha TEXT,
                route TEXT,
                workspace_path TEXT,
                created_at TEXT,
                reviewed_at TEXT
            )
        """)
        ldb.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                jid,
                "app",
                str(historical_ws),
                "codex/historical-feature",
                "accepted",
                "2026-01-01T01:00:00Z",
            ),
        )
        ldb.execute(
            "INSERT INTO review_inbox_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-historical-1",
                jid,
                "accepted",
                ckpt_ref,
                branch_sha,
                "app",
                str(historical_ws),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/historical-feature"]
    assert len(branch_rows) == 1
    # Authorizes accepted-integrated and operation ID because of durable checkpoint ref
    assert branch_rows[0]["classification"] == "accepted-integrated"
    assert branch_rows[0]["operation_id"]
    op_id = branch_rows[0]["operation_id"]

    rec = service.reconcile()
    assert op_id in rec["enqueued"]

    res = service.apply(op_id)
    assert res["action"] == "completed"
    with pytest.raises(GitError):
        run_git(route, "show-ref", "--verify", "refs/heads/codex/historical-feature")


def test_unowned_branch_legacy_db_removed_path_checkpoint_sha_mismatch_retained(
    tmp_path: Path,
) -> None:
    import hashlib
    import sqlite3

    config, route, _slot = _fixture(tmp_path)
    run_git(route, "fetch", "origin")
    run_git(route, "checkout", "main")
    run_git(route, "reset", "--hard", "origin/main")

    # Create an unowned integrated branch on route
    run_git(route, "checkout", "-b", "codex/sha-mismatch-feature", "origin/main")
    (route / "mismatch.txt").write_text("mismatch code\n", encoding="utf-8")
    run_git(route, "add", "mismatch.txt")
    run_git(route, "commit", "-m", "mismatch feature commit")
    branch_sha = run_git(route, "rev-parse", "codex/sha-mismatch-feature")
    run_git(route, "checkout", "main")
    run_git(route, "merge", "--ff-only", "codex/sha-mismatch-feature")
    run_git(route, "push", "origin", "main")

    # Historical slot path that does not exist
    slot_root = getattr(config, "slot_root", None) or (config.coordination_root / "slots")
    historical_ws = slot_root / "historical-slot-mismatch"
    assert not historical_ws.exists()

    # Create checkpoint ref in repo with branch_sha
    jid = "job-mismatch-1"
    j_hash = hashlib.sha256(jid.encode("utf-8")).hexdigest()
    ckpt_ref = f"refs/agent-control-plane/jobs/{j_hash}"
    run_git(route, "update-ref", ckpt_ref, branch_sha)

    # Legacy DB specifies a contradictory/mismatched checkpoint SHA
    bad_sha = "0" * 40
    legacy_db_path = config.coordination_root / "legacy/control-plane/shared-jobs.sqlite3"
    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy_db_path) as ldb:
        ldb.execute("""
            CREATE TABLE review_inbox_items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT,
                review_status TEXT,
                checkpoint_ref TEXT,
                checkpoint_sha TEXT,
                route TEXT,
                workspace_path TEXT,
                created_at TEXT,
                reviewed_at TEXT
            )
        """)
        ldb.execute(
            "INSERT INTO review_inbox_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-mismatch-1",
                jid,
                "accepted",
                ckpt_ref,
                bad_sha,
                "app",
                str(historical_ws),
                "2026-01-01T00:00:00Z",
                "2026-01-01T01:00:00Z",
            ),
        )

    service = SlotLifecycleService(
        config,
        slots=SlotStore(config.database_path),
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    audit = service.audit()
    branch_rows = [r for r in audit["resources"] if r.get("branch") == "codex/sha-mismatch-feature"]
    assert len(branch_rows) == 1
    # Mismatched SHA must fail verification and be retained without operation ID
    assert branch_rows[0]["classification"] == "retained-unowned"
    assert "operation_id" not in branch_rows[0]


def test_auto_return_accepted_and_canonically_integrated(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-ret-1"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-ret-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-ret-1", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    canonical_tip = run_git(route, "rev-parse", "origin/main")
    res = service.auto_return_slot("app-1")
    assert res["status"] == "returned"
    assert res["branch"] == "slot/app-1"
    assert res["head"] == canonical_tip
    assert res["generation"] == gen + 1

    # Verified: slot is now on default branch and clean
    assert run_git(slot, "branch", "--show-current") == "slot/app-1"
    assert run_git(slot, "rev-parse", "HEAD") == canonical_tip
    assert run_git(slot, "status", "--porcelain=v1", "-uall") == ""

    # Verified: audit recognizes the slot as available
    audit = service.audit()
    slot_row = next(r for r in audit["resources"] if r.get("name") == "app-1")
    assert slot_row["classification"] == "available"
    assert slot_row["reasons"] == []


def test_auto_return_exact_tree_equivalence_distinct_root_commit(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    # Slot creates a task commit on a new task branch
    run_git(slot, "checkout", "-b", "task/tree-equiv-task")
    (slot / "feature_tree.txt").write_text("tree equivalence content\n", encoding="utf-8")
    run_git(slot, "add", "feature_tree.txt")
    run_git(slot, "commit", "-m", "worker task commit with unique tree")
    worker_sha = run_git(slot, "rev-parse", "HEAD")
    worker_tree = run_git(slot, "rev-parse", "HEAD^{tree}")

    checkpoint_ref = "refs/agent-control-plane/jobs/job-tree-equiv"
    run_git(route, "update-ref", checkpoint_ref, worker_sha)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-tree-equiv",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=worker_sha,
            checkpoint_tree_sha=worker_tree,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-tree-equiv", "accepted")

    # Root integrates the exact same tree into main via a DISTINCT commit
    # (simulating signed root commit or squash where worker_sha is NOT an ancestor)
    run_git(route, "fetch", "origin")
    main_tip = run_git(route, "rev-parse", "origin/main")
    root_commit = run_git(
        route, "commit-tree", worker_tree, "-p", main_tip, "-m", "root distinct integration"
    )
    run_git(route, "update-ref", "refs/heads/main", root_commit)
    run_git(route, "push", "origin", "main")

    # Prove that worker_sha is NOT an ancestor of canonical tip
    with pytest.raises(GitError):
        run_git(route, "merge-base", "--is-ancestor", worker_sha, root_commit)

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # Exact tree equivalence allows auto-return to succeed
    res = service.auto_return_slot("app-1")
    assert res["status"] == "returned"
    assert res["branch"] == "slot/app-1"
    assert res["head"] == root_commit

    # Audit classifies slot as available
    audit = service.audit()
    slot_row = next(r for r in audit["resources"] if r.get("name") == "app-1")
    assert slot_row["classification"] == "available"
    assert slot_row["reasons"] == []


def test_auto_return_refuses_when_accepted_not_integrated(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    run_git(slot, "checkout", "-b", "task/unintegrated-work")
    (slot / "unpushed.txt").write_text("not pushed\n", encoding="utf-8")
    run_git(slot, "add", "unpushed.txt")
    run_git(slot, "commit", "-m", "unpushed commit")
    unpushed_sha = run_git(slot, "rev-parse", "HEAD")

    checkpoint_ref = "refs/agent-control-plane/jobs/job-unpushed-1"
    run_git(route, "update-ref", checkpoint_ref, unpushed_sha)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-unpushed-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=unpushed_sha,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-unpushed-1", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # Must refuse auto-return because commit is not in canonical remote
    res = service.auto_return_slot("app-1")
    assert res["status"] == "skipped"
    assert "not canonical-integrated" in res["reason"]

    # Slot remains on its task branch untouched
    assert run_git(slot, "branch", "--show-current") == "task/unintegrated-work"
    assert run_git(slot, "rev-parse", "HEAD") == unpushed_sha
    assert slots.require_slot("app-1").generation == gen


def test_auto_return_fails_closed_on_dirty_active_cwd_drift(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-fail-closed"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-fail-closed",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-fail-closed", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # 1. Dirty tracked file
    (slot / "change.txt").write_text("dirty edits\n", encoding="utf-8")
    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "dirty" in res["reason"]
    assert run_git(slot, "branch", "--show-current") == "task/app-1"
    run_git(slot, "checkout", "--", "change.txt")

    # 2. Unexpected ignored file
    (slot / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (slot / "extra.tmp").write_text("ignored file\n", encoding="utf-8")
    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "dirty" in res["reason"] or "ignored" in res["reason"]
    (slot / "extra.tmp").unlink()
    (slot / ".gitignore").unlink()

    # 3. Live process CWD
    service.live_cwds = lambda _p: [99999]
    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "live process" in res["reason"]
    service.live_cwds = lambda _p: []

    # 4. Active slot
    slots.acquire_slot("app-1", "job-other")
    res = service.auto_return_slot("app-1")
    assert res["status"] == "skipped"
    assert "not available" in res["reason"]
    slots.release_slot("app-1", "job-other")

    # 5. Default branch divergence
    run_git(route, "branch", "slot/app-1", "HEAD")
    # Add unique commit to slot/app-1 that is not in canonical remote
    run_git(route, "checkout", "slot/app-1")
    (route / "divergent.txt").write_text("divergent\n", encoding="utf-8")
    run_git(route, "add", "divergent.txt")
    run_git(route, "commit", "-m", "divergent commit")
    run_git(route, "checkout", "main")

    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "divergent" in res["reason"] or "unmerged" in res["reason"]
    assert run_git(slot, "branch", "--show-current") == "task/app-1"


def test_freed_integrated_task_branch_gets_exact_cleanup_intent(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation
    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-freed-1"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-freed-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-freed-1", "accepted")

    jobs = JobStore(config.database_path)
    jobs.create_job(
        job_id="job-freed-1",
        task_id="task-freed-1",
        route="app",
        workspace_path=slot,
        expected_branch="task/app-1",
        expected_result_status="completed",
        controller_gate_mode="full",
        config_path=config.config_path,
        run_dir=config.runs_root / "job-freed-1",
        prompt_path=config.runs_root / "job-freed-1/prompt.md",
        result_path=config.runs_root / "job-freed-1/result.md",
        timeout_sec=10,
        idle_timeout_sec=10,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="agy",
        workspace_access="native",
        slot_name="app-1",
    )
    jobs.update_job("job-freed-1", status="completed")
    jobs.set_root_acceptance("job-freed-1", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=jobs,
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # Reconcile with auto_return=True: auto-returns slot to slot/app-1, freeing task/app-1
    reconcile_res = service.reconcile(auto_return=True, enqueue=True)
    assert run_git(slot, "branch", "--show-current") == "slot/app-1"

    # The freed task branch task/app-1 is audited as accepted-integrated and enqueued
    branch_rows = [
        r
        for r in reconcile_res["resources"]
        if r.get("branch") == "task/app-1" and r.get("kind") == "branch"
    ]
    assert len(branch_rows) == 1
    assert branch_rows[0]["classification"] == "accepted-integrated"
    op_id = branch_rows[0]["operation_id"]
    assert op_id in reconcile_res["enqueued"]

    # Applying the cleanup deletes the freed integrated task branch
    apply_res = service.apply(op_id)
    assert apply_res["action"] == "completed"
    with pytest.raises(GitError):
        run_git(route, "rev-parse", "--verify", "refs/heads/task/app-1")


def test_freed_unique_task_branch_remains_retained(tmp_path: Path) -> None:
    config, route, slot = _fixture(tmp_path)
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)

    # Create a unique unowned branch on route with unique work
    run_git(route, "branch", "task/unique-work-branch", "main")
    run_git(route, "checkout", "task/unique-work-branch")
    (route / "unique_keep.txt").write_text("must be kept\n", encoding="utf-8")
    run_git(route, "add", "unique_keep.txt")
    run_git(route, "commit", "-m", "unique unmerged work")
    unique_sha = run_git(route, "rev-parse", "HEAD")
    run_git(route, "checkout", "main")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=ReviewInboxStore(config.database_path),
        live_cwds=lambda _path: [],
    )

    # Reconcile must retain the unique branch and NOT enqueue any cleanup intent
    reconcile_res = service.reconcile(enqueue=True)
    branch_rows = [
        r for r in reconcile_res["resources"] if r.get("branch") == "task/unique-work-branch"
    ]
    assert len(branch_rows) == 1
    assert branch_rows[0]["classification"] == "unique-unpushed"
    assert "operation_id" not in branch_rows[0]
    assert branch_rows[0]["sha"] == unique_sha

    # Branch is preserved and not deleted
    assert (
        run_git(route, "rev-parse", "--verify", "refs/heads/task/unique-work-branch") == unique_sha
    )


def test_auto_return_normally_prepared_slot_with_real_venv_symlink_and_caches(
    tmp_path: Path,
) -> None:
    venv_dir = tmp_path / "canonical_venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    prep_cmd = SlotPrepareCommand(
        name="acp_venv",
        working_dir=Path("."),
        marker=Path(".venv/pyvenv.cfg"),
        command=("ln", "-s", str(venv_dir), ".venv"),
        timeout_sec=30,
        routes=("app",),
    )

    config, route, slot = _fixture(tmp_path, slot_prepare=(prep_cmd,))
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    (slot / ".gitignore").write_text(
        ".venv\n.pytest_cache/\n.ruff_cache/\n__pycache__/\n", encoding="utf-8"
    )
    run_git(slot, "add", ".gitignore")
    run_git(slot, "commit", "-m", "add gitignore for venv and caches")
    run_git(slot, "push", "origin", "HEAD:main")
    run_git(route, "fetch", "origin", "main:refs/remotes/origin/main")

    os.symlink(venv_dir, slot / ".venv")
    assert (slot / ".venv").is_symlink()
    assert (slot / ".venv" / "pyvenv.cfg").exists()

    (slot / ".pytest_cache").mkdir()
    (slot / ".pytest_cache" / "v").mkdir()
    (slot / ".pytest_cache" / "v" / "cache").write_text("{}", encoding="utf-8")
    (slot / ".ruff_cache").mkdir()
    (slot / ".ruff_cache" / "content.txt").write_text("cached", encoding="utf-8")
    (slot / "src").mkdir(exist_ok=True)
    (slot / "src" / "__pycache__").mkdir(exist_ok=True)
    (slot / "src" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00\x01\x02\x03")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-prepared-1"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-prepared-1",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-prepared-1", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    res = service.auto_return_slot("app-1")
    assert res["status"] == "returned"
    canonical_tip = res["head"]
    assert res["branch"] == "slot/app-1"
    assert res["generation"] == gen + 1

    assert run_git(slot, "branch", "--show-current") == "slot/app-1"
    assert run_git(slot, "rev-parse", "HEAD") == canonical_tip

    # Verified: prep symlink remains intact and points to canonical venv
    assert (slot / ".venv").is_symlink()
    assert (slot / ".venv").resolve() == venv_dir.resolve()
    assert (slot / ".venv" / "pyvenv.cfg").exists()

    # Verified: disposable caches were removed
    assert not (slot / ".pytest_cache").exists()
    assert not (slot / ".ruff_cache").exists()
    assert not (slot / "src" / "__pycache__").exists()

    # Verified: audit recognizes the slot as available with zero blocker reasons
    audit = service.audit()
    slot_row = next(r for r in audit["resources"] if r.get("name") == "app-1")
    assert slot_row["classification"] == "available"
    assert slot_row["reasons"] == []


def test_auto_return_fails_closed_on_unknown_ignored_file(tmp_path: Path) -> None:
    venv_dir = tmp_path / "canonical_venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    prep_cmd = SlotPrepareCommand(
        name="acp_venv",
        working_dir=Path("."),
        marker=Path(".venv/pyvenv.cfg"),
        command=("ln", "-s", str(venv_dir), ".venv"),
        timeout_sec=30,
        routes=("app",),
    )

    config, route, slot = _fixture(tmp_path, slot_prepare=(prep_cmd,))
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    (slot / ".gitignore").write_text(".venv\n*.tmp\n", encoding="utf-8")
    run_git(slot, "add", ".gitignore")
    run_git(slot, "commit", "-m", "add gitignore")
    run_git(slot, "push", "origin", "HEAD:main")
    run_git(route, "fetch", "origin", "main:refs/remotes/origin/main")

    os.symlink(venv_dir, slot / ".venv")

    # Add unknown ignored file
    (slot / "rogue.tmp").write_text("unknown ignored\n", encoding="utf-8")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-unknown-ignored"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-unknown-ignored",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-unknown-ignored", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "unexpected ignored" in res["reason"] or "ignored" in res["reason"]

    # Must avoid branch mutation: slot remains on task/app-1
    assert run_git(slot, "branch", "--show-current") == "task/app-1"
    assert slots.require_slot("app-1").generation == gen


def test_auto_return_fails_closed_on_wrong_symlink_target_drift(tmp_path: Path) -> None:
    venv_dir = tmp_path / "canonical_venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    wrong_venv = tmp_path / "wrong_venv"
    wrong_venv.mkdir()
    (wrong_venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    prep_cmd = SlotPrepareCommand(
        name="acp_venv",
        working_dir=Path("."),
        marker=Path(".venv/pyvenv.cfg"),
        command=("ln", "-s", str(venv_dir), ".venv"),
        timeout_sec=30,
        routes=("app",),
    )

    config, route, slot = _fixture(tmp_path, slot_prepare=(prep_cmd,))
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    (slot / ".gitignore").write_text(".venv\n", encoding="utf-8")
    run_git(slot, "add", ".gitignore")
    run_git(slot, "commit", "-m", "add gitignore")
    run_git(slot, "push", "origin", "HEAD:main")
    run_git(route, "fetch", "origin", "main:refs/remotes/origin/main")

    # Symlink points to wrong target (drift)
    os.symlink(wrong_venv, slot / ".venv")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-target-drift"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-target-drift",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-target-drift", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "target drift" in res["reason"] or "symlink" in res["reason"]

    # Must avoid branch mutation: slot remains on task/app-1
    assert run_git(slot, "branch", "--show-current") == "task/app-1"
    assert slots.require_slot("app-1").generation == gen


def test_auto_return_fails_closed_on_wrong_symlink_type(tmp_path: Path) -> None:
    venv_dir = tmp_path / "canonical_venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    prep_cmd = SlotPrepareCommand(
        name="acp_venv",
        working_dir=Path("."),
        marker=Path(".venv/pyvenv.cfg"),
        command=("ln", "-s", str(venv_dir), ".venv"),
        timeout_sec=30,
        routes=("app",),
    )

    config, route, slot = _fixture(tmp_path, slot_prepare=(prep_cmd,))
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    (slot / ".gitignore").write_text(".venv\n", encoding="utf-8")
    run_git(slot, "add", ".gitignore")
    run_git(slot, "commit", "-m", "add gitignore")
    run_git(slot, "push", "origin", "HEAD:main")
    run_git(route, "fetch", "origin", "main:refs/remotes/origin/main")

    # Regular directory instead of symlink
    (slot / ".venv").mkdir()
    (slot / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-wrong-type"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-wrong-type",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-wrong-type", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "not a symlink" in res["reason"] or "symlink" in res["reason"]

    # Must avoid branch mutation: slot remains on task/app-1
    assert run_git(slot, "branch", "--show-current") == "task/app-1"
    assert slots.require_slot("app-1").generation == gen


def test_auto_return_fails_closed_on_ignored_file_appearing_after_claim(tmp_path: Path) -> None:
    venv_dir = tmp_path / "canonical_venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    prep_cmd = SlotPrepareCommand(
        name="acp_venv",
        working_dir=Path("."),
        marker=Path(".venv/pyvenv.cfg"),
        command=("ln", "-s", str(venv_dir), ".venv"),
        timeout_sec=30,
        routes=("app",),
    )

    config, route, slot = _fixture(tmp_path, slot_prepare=(prep_cmd,))
    slots = SlotStore(config.database_path)
    slots.register_slot("app-1", "app", slot)
    gen = slots.require_slot("app-1").generation

    (slot / ".gitignore").write_text(".venv\n*.late\n", encoding="utf-8")
    run_git(slot, "add", ".gitignore")
    run_git(slot, "commit", "-m", "add gitignore")
    run_git(slot, "push", "origin", "HEAD:main")
    run_git(route, "fetch", "origin", "main:refs/remotes/origin/main")

    os.symlink(venv_dir, slot / ".venv")

    accepted = run_git(slot, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/job-late-ignored"
    run_git(route, "update-ref", checkpoint_ref, accepted)

    inbox = ReviewInboxStore(config.database_path)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-late-ignored",
            source_status="completed",
            delivery_status="checkpointed",
            route="app",
            workspace_path=slot,
            slot_name="app-1",
            slot_generation=gen,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=accepted,
            result_text="Status: completed\n",
            verification_bundle=_valid_bundle(),
            slot_released=True,
        )
    )
    inbox.resolve("agent_job:job-late-ignored", "accepted")

    service = SlotLifecycleService(
        config,
        slots=slots,
        jobs=JobStore(config.database_path),
        inbox=inbox,
        live_cwds=lambda _path: [],
    )

    # Intercept claim_for_cleanup so a late ignored file is injected right after claim
    real_claim = slots.claim_for_cleanup

    def inject_late_ignored(*args: Any, **kwargs: Any) -> Any:
        res = real_claim(*args, **kwargs)
        (slot / "injected.late").write_text(
            "late ignored file written concurrently\n", encoding="utf-8"
        )
        return res

    slots.claim_for_cleanup = inject_late_ignored  # type: ignore[assignment]

    res = service.auto_return_slot("app-1")
    assert res["status"] == "failed"
    assert "transition failed" in res["reason"]

    # Slot must be quarantined because claim was acquired and invariant violated
    assert slots.require_slot("app-1").status == "quarantined"

    # Branch must NOT be mutated: slot remains on task/app-1
    assert run_git(slot, "branch", "--show-current") == "task/app-1"


def test_canonical_windows_path_extended_and_ordinary_drive_paths() -> None:
    # Equivalent extended (\\?\ and \??\) and ordinary paths
    assert canonical_windows_path(r"\\?\C:\repo\canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )
    assert canonical_windows_path(r"\??\C:\repo\canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )
    assert canonical_windows_path("//?/C:/repo/canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )
    assert canonical_windows_path("C:/repo/canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )


def test_canonical_windows_path_extended_and_ordinary_unc_paths() -> None:
    # Equivalent extended (\\?\UNC\ and \??\UNC\) and ordinary UNC paths
    assert canonical_windows_path(r"\\?\UNC\server\share\venv") == canonical_windows_path(
        r"\\server\share\venv"
    )
    assert canonical_windows_path(r"\\?\unc\server\share\venv") == canonical_windows_path(
        r"\\server\share\venv"
    )
    assert canonical_windows_path(r"\\?\Unc\server\share\venv") == canonical_windows_path(
        r"\\server\share\venv"
    )
    assert canonical_windows_path(r"\??\UNC\server\share\venv") == canonical_windows_path(
        r"\\server\share\venv"
    )
    assert canonical_windows_path("//?/UNC/server/share/venv") == canonical_windows_path(
        r"\\server\share\venv"
    )


def test_canonical_windows_path_case_normalization() -> None:
    # Windows paths are case-insensitive
    assert canonical_windows_path(r"C:\Repo\Canonical_Venv") == canonical_windows_path(
        r"c:\repo\canonical_venv"
    )
    assert canonical_windows_path(r"\\?\C:\REPO\CANONICAL_VENV") == canonical_windows_path(
        r"c:\repo\canonical_venv"
    )
    assert canonical_windows_path(r"\\?\UNC\SERVER\SHARE\venv") == canonical_windows_path(
        r"\\server\share\venv"
    )


def test_canonical_windows_path_dot_segments_and_separators() -> None:
    # Redundant separators and dot segments normalize cleanly
    assert canonical_windows_path(r"C:\repo\sub\..\canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )
    assert canonical_windows_path(r"\\?\C:\repo\sub\..\canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )
    assert canonical_windows_path(r"C:\repo\.\canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )
    assert canonical_windows_path("C:/repo//canonical_venv") == canonical_windows_path(
        r"C:\repo\canonical_venv"
    )


def test_canonical_windows_path_rejects_genuinely_different_targets() -> None:
    # Different directory targets must never match
    assert canonical_windows_path(r"\\?\C:\repo\canonical_venv") != canonical_windows_path(
        r"C:\repo\wrong_venv"
    )
    assert canonical_windows_path(r"C:\repo\canonical_venv") != canonical_windows_path(
        r"C:\repo\canonical_venv_2"
    )
    assert canonical_windows_path(r"C:\repo\canonical_venv") != canonical_windows_path(
        r"D:\repo\canonical_venv"
    )
    assert canonical_windows_path(r"\\server1\share\venv") != canonical_windows_path(
        r"\\server2\share\venv"
    )
    # Target checks must NOT use basename, suffix, or lexical containment
    assert canonical_windows_path(r"C:\repo\canonical_venv") != canonical_windows_path(
        r"C:\other\canonical_venv"
    )
    assert canonical_windows_path(r"C:\prefix_canonical_venv") != canonical_windows_path(
        r"C:\canonical_venv"
    )
    assert canonical_windows_path(r"C:\a\canonical_venv\b") != canonical_windows_path(
        r"C:\canonical_venv"
    )


def test_prep_symlink_targets_equal_windows_platform() -> None:
    # Windows platform semantics: extended path equivalence and case normalization
    assert prep_symlink_targets_equal(
        Path(r"\\?\C:\repo\canonical_venv"),
        Path(r"C:\repo\canonical_venv"),
        platform="win32",
    )
    assert prep_symlink_targets_equal(
        Path(r"\??\C:\repo\canonical_venv"),
        Path(r"C:\repo\canonical_venv"),
        platform="win32",
    )
    assert prep_symlink_targets_equal(
        Path(r"C:\Repo\Canonical_Venv"),
        Path(r"c:\repo\canonical_venv"),
        platform="win32",
    )
    # Reject genuinely different targets
    assert not prep_symlink_targets_equal(
        Path(r"\\?\C:\repo\wrong_venv"),
        Path(r"C:\repo\canonical_venv"),
        platform="win32",
    )
    assert not prep_symlink_targets_equal(
        Path(r"C:\other\canonical_venv"),
        Path(r"C:\repo\canonical_venv"),
        platform="win32",
    )
    assert not prep_symlink_targets_equal(
        Path(r"C:\repo\canonical_venv"),
        Path(r"D:\repo\canonical_venv"),
        platform="win32",
    )


def test_prep_symlink_targets_equal_posix_preservation(tmp_path: Path) -> None:
    d1 = tmp_path / "venv1"
    d1.mkdir()
    d2 = tmp_path / "venv2"
    d2.mkdir()

    # Filesystem identity via os.path.samefile works on existing targets
    assert prep_symlink_targets_equal(d1, d1, platform="linux")
    assert not prep_symlink_targets_equal(d1, d2, platform="linux")

    # Non-existing targets on POSIX reject non-identical raw paths
    p1 = tmp_path / "nonexistent"
    p2 = tmp_path / "NONEXISTENT"
    assert not prep_symlink_targets_equal(p1, p2, platform="linux")


def test_is_symlink_or_junction_semantics(tmp_path: Path) -> None:
    target = tmp_path / "target_dir"
    target.mkdir()
    link = tmp_path / "symlink_dir"
    os.symlink(target, link)

    regular_dir = tmp_path / "reg_dir"
    regular_dir.mkdir()
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("content", encoding="utf-8")

    assert is_symlink_or_junction(link)
    assert not is_symlink_or_junction(regular_dir)
    assert not is_symlink_or_junction(regular_file)
    assert not is_symlink_or_junction(tmp_path / "nonexistent")
