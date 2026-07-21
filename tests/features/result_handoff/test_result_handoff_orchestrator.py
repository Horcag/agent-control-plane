from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane, PolicyError
from agent_control_plane.entities.job import ReviewMetricsStore
from agent_control_plane.entities.plan import PlanTaskDefinition
from agent_control_plane.entities.review_inbox import ReviewInboxDraft
from agent_control_plane.features.result_handoff import SlotCheckpointError
from agent_control_plane.shared.codex_session_usage import TokenUsage
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    NativeQualityGateConfig,
    RouteConfig,
    SlotConfig,
)
from agent_control_plane.shared.git_tools import run_git, workspace_state
from agent_control_plane.shared.native_quality import (
    NativeQualityContract,
    resolve_native_quality_contract,
    write_native_quality_contract,
)
from agent_control_plane.shared.sqlite_runtime import control_database


def test_terminal_dirty_job_is_checkpointed_delivered_and_slot_becomes_available(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="checkpoint")
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-1", status_result="completed")
    job.result_path.write_text(
        """Status: completed

Changed files:
- worker.txt

What changed:
- Added the durable worker result.

Verification performed:
- focused test passed

Not verified / remaining risks:
- none
""",
        encoding="utf-8",
    )
    job.result_path.with_name("verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [{"path": "worker.txt", "change": "added"}],
                "checks": [
                    {
                        "command": "pytest -q tests/test_worker.py",
                        "cwd": ".",
                        "outcome": "passed",
                        "exit_code": 0,
                        "summary": "focused test passed",
                    }
                ],
                "unverified": [],
            }
        ),
        encoding="utf-8",
    )
    base_sha = run_git(slot, "rev-parse", "HEAD")
    (slot / "worker.txt").write_text("durable result\n", encoding="utf-8")

    finished = control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get("agent_job:job-1")
    slot_state = control.slots.inspect_slot("app-1")
    assert finished.status == "completed"
    assert item.delivery_status == "checkpointed"
    assert item.checkpoint_ref is not None
    assert item.checkpoint_sha is not None
    assert item.slot_released is True
    assert item.verification_bundle is not None
    assert item.verification_bundle["review_ready"] is True
    assert item.verification_bundle["result"]["format_valid"] is True
    assert item.verification_bundle["worker_verification"]["state"] == "valid"
    assert item.verification_bundle["changed_files_actual"] == [
        {"path": "worker.txt", "status": "A"}
    ]
    assert item.verification_bundle["artifact"]["checkpoint_verified"] is True
    assert run_git(slot, "show", f"{item.checkpoint_sha}:worker.txt") == "durable result"
    assert run_git(slot, "rev-parse", "HEAD") == base_sha
    assert workspace_state(slot).porcelain == ""
    assert slot_state.status == "available"
    assert slot_state.active_job_id is None
    causal = control.store.get_job(job.job_id)
    assert causal.workspace_disposition == "dirty_after_job"
    assert causal.checkpoint_disposition == "salvage"
    assert causal.root_acceptance == "pending"

    control.finish_job(job.job_id, "completed")
    repeated_item = control.review_inbox.get("agent_job:job-1")
    assert repeated_item.checkpoint_sha == item.checkpoint_sha
    assert repeated_item.delivery_status == "checkpointed"
    assert repeated_item.slot_released is True
    assert control.store.get_job(job.job_id).checkpoint_disposition == "salvage"


@pytest.mark.parametrize(
    "artifact",
    ("new.rej", "new.orig", "tmp_fix.patch", "single_fix.patch"),
)
def test_temporary_artifact_checkpoint_is_preserved_and_quarantined(
    tmp_path: Path,
    artifact: str,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "print('controller gate ran')")
    control = AgentControlPlane(
        _config(
            tmp_path,
            route,
            slot,
            terminal_slot_policy="checkpoint",
            native_quality_policy="controller",
            native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
        )
    )
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        f"job-temporary-artifact-{artifact}",
        status_result="completed",
        workspace_access="native",
    )
    control.create_plan(
        plan_id=f"temporary-artifact-plan-{artifact}",
        title="Temporary artifact plan",
        tasks=(PlanTaskDefinition("task", "Task"),),
    )
    control.bind_plan_job(f"temporary-artifact-plan-{artifact}", "task", job.job_id)
    _write_completed_result(job.result_path, command=shlex.join(command), changed_file=artifact)
    (slot / artifact).write_text("temporary patch output\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    slot_state = control.slots.inspect_slot("app-1")
    assert bundle is not None
    assert item.delivery_status == "checkpoint_contaminated"
    assert item.slot_released is False
    assert bundle["review_ready"] is False
    assert bundle["artifact"]["checkpoint_verified"] is True
    assert bundle["artifact"]["disposition"] == "contaminated"
    assert bundle["artifact"]["matched_paths"] == [artifact]
    assert bundle["controller_quality"]["state"] == "not_required"
    assert run_git(slot, "show", f"{item.checkpoint_sha}:{artifact}") == "temporary patch output"
    assert (slot / artifact).exists()
    assert workspace_state(slot).dirty
    assert slot_state.status == "contaminated"
    assert slot_state.active_job_id is None
    assert artifact in (slot_state.note or "")
    causal = control.store.get_job(job.job_id)
    assert causal.workspace_disposition == "dirty_after_job"
    assert causal.checkpoint_disposition == "contaminated"
    assert causal.root_acceptance == "pending"
    assert not (job.run_dir / "native-quality.json").exists()
    reviews = ReviewMetricsStore(control.config.database_path)
    span_id = reviews.start_span(
        name="Continuation", session_path=tmp_path / "rollout.jsonl", usage=TokenUsage(0, 0, 0, 0)
    )
    with pytest.raises(ValueError, match="cannot verify continuation"):
        control.verify_continuation_handoff(
            f"temporary-artifact-plan-{artifact}",
            "task",
            review_span_id=span_id,
            checkpoint_sha=item.checkpoint_sha or "",
        )
    assert reviews.report(span_id)["job_outcomes"] == []
    with pytest.raises(PolicyError, match="review-ready"):
        control.accept_plan_task(f"temporary-artifact-plan-{artifact}", "task")

    control.finish_job(job.job_id, "completed")
    repeated = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert repeated.checkpoint_sha == item.checkpoint_sha
    assert repeated.checkpoint_ref == item.checkpoint_ref
    assert repeated.delivery_status == "checkpoint_contaminated"
    assert control.store.get_job(job.job_id).checkpoint_disposition == "contaminated"


def test_dirty_runner_failure_is_preserved_when_checkpointed(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot, terminal_slot_policy="checkpoint"))
    job = _active_slot_job(control, tmp_path, slot, "job-runner-failure", status_result="partial")
    control.store.set_runner_failure(job.job_id, "tool_call_budget")
    (slot / "worker.txt").write_text("preserve failure cause\n", encoding="utf-8")

    control.finish_job(job.job_id, "stopped_dirty_after_failure")

    causal = control.store.get_job(job.job_id)
    assert causal.runner_failure == "tool_call_budget"
    assert causal.workspace_disposition == "dirty_after_failure"
    assert causal.checkpoint_disposition == "salvage"
    assert causal.root_acceptance == "pending"


def test_scoped_patch_does_not_contaminate_checkpoint(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot, terminal_slot_policy="checkpoint"))
    job = _active_slot_job(control, tmp_path, slot, "job-scoped-patch", status_result="completed")
    _write_completed_result(
        job.result_path, command="worker check", changed_file="scoped-change.patch"
    )
    (slot / "scoped-change.patch").write_text("ordinary patch\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert item.delivery_status == "checkpointed"
    assert item.verification_bundle is not None
    assert item.verification_bundle["artifact"]["disposition"] == "normal"
    assert item.verification_bundle["artifact"]["matched_paths"] == []


def test_modified_preexisting_orig_does_not_contaminate_checkpoint(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    (slot / "legacy.orig").write_text("base\n", encoding="utf-8")
    _git(slot, "add", "legacy.orig")
    _git(slot, "commit", "-m", "track legacy artifact")
    control = AgentControlPlane(_config(tmp_path, route, slot, terminal_slot_policy="checkpoint"))
    job = _active_slot_job(
        control, tmp_path, slot, "job-existing-artifact", status_result="completed"
    )
    _write_completed_result(job.result_path, command="worker check", changed_file="legacy.orig")
    (slot / "legacy.orig").write_text("modified\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert item.delivery_status == "checkpointed"
    assert item.verification_bundle is not None
    assert item.verification_bundle["artifact"]["disposition"] == "normal"


def test_native_controller_quality_is_rerun_and_bound_to_checkpoint(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "print('controller-ok')")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-native-quality",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(job.result_path, command=shlex.join(command))
    (slot / "worker.txt").write_text("quality checked\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is True
    assert bundle["quality_contract"]["policy"] == "controller"
    assert bundle["worker_quality"]["status"] == "passed"
    assert bundle["controller_quality"]["state"] == "valid"
    assert bundle["controller_quality"]["payload"]["status"] == "passed"
    assert (
        bundle["controller_quality"]["payload"]["checkpoint_tree_sha"] == item.checkpoint_tree_sha
    )
    assert item.slot_released is True
    assert workspace_state(slot).porcelain == ""


def test_controller_only_gate_is_not_duplicated_by_worker(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    controller_command = (sys.executable, "-c", "print('controller-ok')")
    worker_command = f"{shlex.quote(sys.executable)} -c \"print('worker-fast-check')\""
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(
            NativeQualityGateConfig(
                name="controller",
                command=controller_command,
                run_on="controller",
            ),
        ),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-controller-only-quality",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(job.result_path, command=worker_command)
    (slot / "worker.txt").write_text("quality checked once by controller\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is True
    assert bundle["worker_quality"]["required_gates"] == []
    assert bundle["worker_quality"]["status"] == "passed"
    assert [check["name"] for check in bundle["controller_quality"]["payload"]["checks"]] == [
        "controller"
    ]


def test_focused_controller_mode_runs_only_shared_gates_and_cannot_be_review_ready(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    controller_command = (sys.executable, "-c", "raise SystemExit(8)")
    shared_command = (sys.executable, "-c", "print('shared-ok')")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(
            NativeQualityGateConfig(
                name="controller", command=controller_command, run_on="controller"
            ),
            NativeQualityGateConfig(name="shared", command=shared_command, run_on="both"),
        ),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-focused-quality",
        status_result="completed",
        workspace_access="native",
        controller_gate_mode="focused",
    )
    _write_completed_result(job.result_path, command=shlex.join(shared_command))
    (slot / "worker.txt").write_text("focused quality\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    bundle = control.review_inbox.get(f"agent_job:{job.job_id}").verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is False
    assert bundle["result_contract"] == {
        "expected_status": "completed",
        "reported_status": "completed",
        "matches": True,
    }
    assert bundle["controller_gate_mode"] == "focused"
    assert [check["name"] for check in bundle["controller_quality"]["payload"]["checks"]] == [
        "shared"
    ]


def test_partial_result_does_not_run_completed_only_controller_gates(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "raise SystemExit(8)")
    control = AgentControlPlane(
        _config(
            tmp_path,
            route,
            slot,
            terminal_slot_policy="checkpoint",
            native_quality_policy="controller",
            native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
        )
    )
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-partial-no-controller-gates",
        status_result="partial",
        workspace_access="native",
        expected_result_status="partial",
    )
    job.result_path.write_text(
        """Status: partial

Changed files:
- worker.txt

What changed:
- Recorded the partial outcome.

Verification performed:
- none

Not verified / remaining risks:
- remaining work
""",
        encoding="utf-8",
    )
    job.result_path.with_name("verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "partial",
                "changed_files": [{"path": "worker.txt", "change": "added"}],
                "checks": [],
                "unverified": ["remaining work"],
            }
        ),
        encoding="utf-8",
    )
    (slot / "worker.txt").write_text("partial result\n", encoding="utf-8")

    control.finish_job(job.job_id, "partial")

    bundle = control.review_inbox.get(f"agent_job:{job.job_id}").verification_bundle
    assert bundle is not None
    assert bundle["controller_quality"]["state"] == "not_required"
    assert bundle["review_ready"] is False
    causal = control.store.get_job(job.job_id)
    assert causal.workspace_disposition == "dirty_after_job"
    assert causal.checkpoint_disposition == "salvage"
    assert causal.root_acceptance == "pending"


@pytest.mark.parametrize("disposition", ("continuation_verified", "final_accepted"))
def test_checkpoint_replay_preserves_stronger_disposition(tmp_path: Path, disposition: str) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot, terminal_slot_policy="checkpoint"))
    job = _active_slot_job(control, tmp_path, slot, "job-strong-replay", status_result="completed")
    (slot / "worker.txt").write_text("durable result\n", encoding="utf-8")
    control.finish_job(job.job_id, "completed")
    checkpoint_sha = control.review_inbox.get(f"agent_job:{job.job_id}").checkpoint_sha
    control.store.set_checkpoint_disposition(job.job_id, disposition)

    control.finish_job(job.job_id, "completed")

    assert control.review_inbox.get(f"agent_job:{job.job_id}").checkpoint_sha == checkpoint_sha
    assert control.store.get_job(job.job_id).checkpoint_disposition == disposition


def test_none_controller_gate_mode_does_not_run_or_write_a_quality_report(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "raise SystemExit(8)")
    control = AgentControlPlane(
        _config(
            tmp_path,
            route,
            slot,
            terminal_slot_policy="checkpoint",
            native_quality_policy="controller",
            native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
        )
    )
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-none-controller-gates",
        status_result="completed",
        workspace_access="native",
        controller_gate_mode="none",
    )
    _write_completed_result(job.result_path, command=shlex.join(command))
    (slot / "worker.txt").write_text("no controller gates\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    bundle = control.review_inbox.get(f"agent_job:{job.job_id}").verification_bundle
    assert bundle is not None
    assert not (job.run_dir / "native-quality.json").exists()
    assert bundle["controller_quality"]["state"] == "not_required"
    assert bundle["review_ready"] is False


def test_result_contract_mismatch_does_not_run_controller_gates(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "raise SystemExit(8)")
    control = AgentControlPlane(
        _config(
            tmp_path,
            route,
            slot,
            terminal_slot_policy="checkpoint",
            native_quality_policy="controller",
            native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
        )
    )
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-mismatch-no-controller-gates",
        status_result="completed",
        workspace_access="native",
        expected_result_status="partial",
    )
    _write_completed_result(job.result_path, command="worker check")
    (slot / "worker.txt").write_text("mismatched result\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    bundle = control.review_inbox.get(f"agent_job:{job.job_id}").verification_bundle
    assert bundle is not None
    assert bundle["result_contract"]["matches"] is False
    assert bundle["controller_quality"]["state"] == "not_required"
    assert bundle["review_ready"] is False


def test_shared_gate_uses_expanded_changed_python_files_for_both_stages(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command_template = (
        sys.executable,
        "-c",
        "import sys; raise SystemExit(0 if sys.argv[1:] == ['./worker.py'] else 7)",
        "{changed_python_files}",
    )
    expanded_command = (*command_template[:-1], "./worker.py")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(
            NativeQualityGateConfig(
                name="python-files",
                command=command_template,
                include_globs=("*.py", "**/*.py"),
                run_on="both",
            ),
        ),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-shared-quality",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(
        job.result_path,
        command=shlex.join(expanded_command),
        changed_file="worker.py",
    )
    (slot / "worker.py").write_text("print('quality checked')\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is True
    assert bundle["worker_quality"]["required_gates"] == ["python-files"]
    assert bundle["controller_quality"]["payload"]["checks"][0]["command"] == list(expanded_command)


def test_deleted_python_file_skips_file_placeholder_but_keeps_controller_coverage(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    (slot / "legacy.py").write_text("print('legacy')\n", encoding="utf-8")
    _git(slot, "add", "legacy.py")
    _git(
        slot,
        "-c",
        "user.name=ACP Test",
        "-c",
        "user.email=acp-test@example.invalid",
        "commit",
        "-m",
        "add legacy file",
    )
    controller_command = (sys.executable, "-c", "print('deletion-covered')")
    placeholder_command = (
        sys.executable,
        "-c",
        "raise SystemExit(9)",
        "{changed_python_files}",
    )
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(
            NativeQualityGateConfig(
                name="affected-tests",
                command=controller_command,
                run_on="controller",
            ),
            NativeQualityGateConfig(
                name="ruff-changed",
                command=placeholder_command,
                include_globs=("*.py", "**/*.py"),
                run_on="both",
            ),
        ),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-python-deletion",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(
        job.result_path,
        command=f"{shlex.quote(sys.executable)} -c \"print('worker-deletion-check')\"",
        changed_file="legacy.py",
    )
    (slot / "legacy.py").unlink()

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is True
    assert bundle["worker_quality"]["required_gates"] == []
    report = bundle["controller_quality"]["payload"]
    assert report["command_files"] == []
    assert [check["name"] for check in report["checks"]] == ["affected-tests"]


def test_native_controller_failure_blocks_acceptance_but_releases_clean_slot(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "raise SystemExit(7)")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-native-quality-fail",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(job.result_path, command=shlex.join(command))
    (slot / "worker.txt").write_text("durable but rejected\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is False
    assert bundle["worker_quality"]["status"] == "passed"
    assert bundle["controller_quality"]["payload"]["status"] == "failed"
    assert item.slot_released is True
    assert workspace_state(slot).porcelain == ""


def test_native_quality_requires_worker_changed_files_to_match_checkpoint(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (sys.executable, "-c", "print('controller-ok')")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(NativeQualityGateConfig(name="controller", command=command),),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-native-quality-files",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(job.result_path, command=shlex.join(command))
    verification_path = job.result_path.with_name("verification.json")
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    verification["changed_files"] = [{"path": "other.txt", "change": "added"}]
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    (slot / "worker.txt").write_text("actual checkpoint file\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    bundle = item.verification_bundle
    assert bundle is not None
    assert bundle["review_ready"] is False
    assert bundle["worker_quality"]["status"] == "failed"
    assert bundle["worker_quality"]["changed_files_missing"] == ["worker.txt"]
    assert bundle["worker_quality"]["changed_files_unobserved"] == ["other.txt"]
    assert bundle["controller_quality"]["payload"]["status"] == "passed"


def test_native_controller_gate_that_mutates_workspace_quarantines_slot(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('gate-mutated.txt').write_text('unexpected')",
    )
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(NativeQualityGateConfig(name="mutating", command=command),),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-native-quality-mutates",
        status_result="completed",
        workspace_access="native",
    )
    _write_completed_result(job.result_path, command=shlex.join(command))
    (slot / "worker.txt").write_text("checkpointed original\n", encoding="utf-8")

    finished = control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert finished.finalization_status == "failed"
    assert item.delivery_status == "checkpoint_failed"
    assert item.verification_bundle is not None
    assert item.verification_bundle["review_ready"] is False
    assert "Workspace changed after checkpoint" in (item.checkpoint_error or "")
    assert item.slot_released is False
    assert workspace_state(slot).dirty
    assert run_git(slot, "show", f"{item.checkpoint_sha}:worker.txt") == "checkpointed original"


def test_persisted_quality_contract_drift_is_not_executed(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    expected_command = (sys.executable, "-c", "print('expected')")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(NativeQualityGateConfig(name="expected", command=expected_command),),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-native-quality-drift",
        status_result="completed",
        workspace_access="native",
    )
    drifted_command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('drift-executed.txt').write_text('bad')",
    )
    write_native_quality_contract(
        job.run_dir,
        NativeQualityContract(
            policy="controller",
            gates=(NativeQualityGateConfig(name="drifted", command=drifted_command),),
        ),
    )
    _write_completed_result(job.result_path, command=shlex.join(expected_command))
    (slot / "worker.txt").write_text("durable result\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert not (slot / "drift-executed.txt").exists()
    assert item.slot_released is True
    assert item.verification_bundle is not None
    assert item.verification_bundle["review_ready"] is False
    assert "drifted" in (item.verification_bundle["quality_contract"]["error"] or "")


def test_missing_strict_quality_contract_is_not_replaced_at_finalization(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    expected_command = (sys.executable, "-c", "print('must-not-run')")
    config = _config(
        tmp_path,
        route,
        slot,
        terminal_slot_policy="checkpoint",
        native_quality_policy="controller",
        native_quality_gates=(NativeQualityGateConfig(name="expected", command=expected_command),),
    )
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-native-quality-missing",
        status_result="completed",
        workspace_access="native",
    )
    (job.run_dir / "native-quality-contract.json").unlink()
    _write_completed_result(job.result_path, command=shlex.join(expected_command))
    (slot / "worker.txt").write_text("durable result\n", encoding="utf-8")

    control.finish_job(job.job_id, "completed")

    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert not (job.run_dir / "native-quality.json").exists()
    assert item.slot_released is True
    assert item.verification_bundle is not None
    assert item.verification_bundle["review_ready"] is False
    assert "missing" in (item.verification_bundle["quality_contract"]["error"] or "")


def test_plan_acceptance_requires_valid_structured_verification(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot, terminal_slot_policy="preserve"))
    job = _active_slot_job(control, tmp_path, slot, "job-verify", status_result="completed")
    control.create_plan(
        plan_id="verified-plan",
        title="Verified plan",
        tasks=(PlanTaskDefinition("task", "Task"),),
    )
    control.bind_plan_job("verified-plan", "task", job.job_id)
    control.store.mark_finished(job.job_id, "completed")
    control.store.mark_finalization_completed(job.job_id)

    with pytest.raises(PolicyError, match="verification"):
        control.accept_plan_task("verified-plan", "task")

    job.result_path.with_name("verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [],
                "checks": [],
                "unverified": [],
            }
        ),
        encoding="utf-8",
    )

    accepted = control.accept_plan_task("verified-plan", "task")
    assert accepted["completed"][0]["task_id"] == "task"


def test_accept_handoff_atomically_resolves_inbox_plan_and_review(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="checkpoint")
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-atomic-accept", status_result="completed")
    _finish_review_ready_plan(control, slot, job, plan_id="atomic-plan")
    reviews = ReviewMetricsStore(config.database_path)
    span_id = reviews.start_span(
        span_id="review-atomic",
        name="Atomic handoff review",
        session_path=tmp_path / "rollout.jsonl",
        usage=TokenUsage(0, 0, 0, 0),
    )
    checkpoint_sha = control.review_inbox.get(f"agent_job:{job.job_id}").checkpoint_sha

    accepted = control.accept_handoff(
        "atomic-plan",
        "task",
        review_span_id=span_id,
        defects_found=1,
        notes="root verified the checkpoint",
    )

    assert accepted["status"] == "accepted"
    assert accepted["job_id"] == job.job_id
    assert accepted["accepted_sha"] == checkpoint_sha
    assert control.review_inbox.get(f"agent_job:{job.job_id}").review_status == "accepted"
    assert control.plan_snapshot("atomic-plan")["status"] == "completed"
    assert control.store.get_job(job.job_id).checkpoint_disposition == "final_accepted"
    assert control.store.get_job(job.job_id).root_acceptance == "accepted"
    outcome = reviews.report(span_id)["job_outcomes"][0]
    assert outcome["outcome"] == "accepted"
    assert outcome["root_verified"] is True
    assert outcome["accepted_sha"] == checkpoint_sha
    assert outcome["defects_found"] == 1


def test_accept_handoff_rolls_back_every_decision_when_review_attach_fails(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="checkpoint")
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control, tmp_path, slot, "job-atomic-rollback", status_result="completed"
    )
    _finish_review_ready_plan(control, slot, job, plan_id="rollback-plan")
    reviews = ReviewMetricsStore(config.database_path)
    span_id = reviews.start_span(
        span_id="review-rollback",
        name="Rollback handoff review",
        session_path=tmp_path / "rollout.jsonl",
        usage=TokenUsage(0, 0, 0, 0),
    )
    pre_accept_job = control.store.get_job(job.job_id)
    pre_accept_disposition = pre_accept_job.checkpoint_disposition
    pre_accept_root_acceptance = pre_accept_job.root_acceptance

    with pytest.raises(KeyError, match="Attempt not found"):
        control.accept_handoff(
            "rollback-plan",
            "task",
            review_span_id=span_id,
            attempt_no=99,
        )

    assert control.review_inbox.get(f"agent_job:{job.job_id}").review_status == "pending"
    snapshot = control.plan_snapshot("rollback-plan")
    assert snapshot["status"] == "active"
    assert snapshot["awaiting_review"][0]["task_id"] == "task"
    assert reviews.report(span_id)["job_outcomes"] == []
    post_accept_job = control.store.get_job(job.job_id)
    assert post_accept_job.checkpoint_disposition == pre_accept_disposition
    assert post_accept_job.root_acceptance == pre_accept_root_acceptance
    assert post_accept_job.checkpoint_disposition != "final_accepted"
    assert post_accept_job.root_acceptance != "accepted"


def test_continuation_handoff_is_atomic_idempotent_and_does_not_accept(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="checkpoint")
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control,
        tmp_path,
        slot,
        "job-continuation",
        status_result="partial",
        expected_result_status="partial",
        controller_gate_mode="focused",
    )
    _finish_review_ready_plan(
        control, slot, job, plan_id="continuation-plan", result_status="partial"
    )
    reviews = ReviewMetricsStore(config.database_path)
    span_id = reviews.start_span(
        name="Continuation", session_path=tmp_path / "rollout.jsonl", usage=TokenUsage(0, 0, 0, 0)
    )
    item = control.review_inbox.get(f"agent_job:{job.job_id}")
    assert (
        control.plan_snapshot("continuation-plan")["requires_root_decision"][0]["state"]
        == "partial"
    )
    assert item.delivery_status == "salvage_checkpointed"
    checkpoint_sha = item.checkpoint_sha
    assert checkpoint_sha is not None

    verified = control.verify_continuation_handoff(
        "continuation-plan", "task", review_span_id=span_id, checkpoint_sha=checkpoint_sha
    )
    replay = control.verify_continuation_handoff(
        "continuation-plan", "task", review_span_id=span_id, checkpoint_sha=checkpoint_sha
    )

    assert verified["status"] == replay["status"] == "continuation_verified"
    assert (
        control.review_inbox.get(f"agent_job:{job.job_id}").review_status == "continuation_verified"
    )
    snapshot = control.plan_snapshot("continuation-plan")
    assert snapshot["status"] == "active"
    assert snapshot["requires_root_decision"][0]["task_id"] == "task"
    assert snapshot["requires_root_decision"][0]["state"] == "partial"
    assert control.store.get_job(job.job_id).checkpoint_disposition == "continuation_verified"
    assert control.store.get_job(job.job_id).root_acceptance == "pending"
    outcome = reviews.report(span_id)["job_outcomes"][0]
    assert outcome["checkpoint_sha"] == checkpoint_sha
    assert outcome["accepted_sha"] is None


def test_continuation_handoff_fails_closed_for_mismatched_or_contaminated_checkpoint(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="checkpoint")
    control = AgentControlPlane(config)
    job = _active_slot_job(
        control, tmp_path, slot, "job-continuation-fail", status_result="partial"
    )
    _finish_review_ready_plan(control, slot, job, plan_id="continuation-fail-plan")
    reviews = ReviewMetricsStore(config.database_path)
    span_id = reviews.start_span(
        name="Continuation", session_path=tmp_path / "rollout.jsonl", usage=TokenUsage(0, 0, 0, 0)
    )
    item_id = f"agent_job:{job.job_id}"

    with pytest.raises(ValueError, match="exactly match"):
        control.verify_continuation_handoff(
            "continuation-fail-plan", "task", review_span_id=span_id, checkpoint_sha="wrong"
        )
    with control_database(config.database_path) as db:
        db.execute(
            "update review_inbox_items set verification_bundle_json = null, slot_released = 0 where item_id = ?",
            (item_id,),
        )
    checkpoint_sha = control.review_inbox.get(item_id).checkpoint_sha
    assert checkpoint_sha is not None
    with pytest.raises(ValueError, match="cannot verify continuation"):
        control.verify_continuation_handoff(
            "continuation-fail-plan",
            "task",
            review_span_id=span_id,
            checkpoint_sha=checkpoint_sha,
        )

    assert control.review_inbox.get(item_id).review_status == "pending"
    assert (
        control.plan_snapshot("continuation-fail-plan")["awaiting_review"][0]["task_id"] == "task"
    )
    assert reviews.report(span_id)["job_outcomes"] == []


def test_checkpoint_cleanup_failure_keeps_slot_dirty_and_review_ref_visible(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="checkpoint")
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-1", status_result="cancelled")
    control.store.set_runner_failure(job.job_id, "tool_call_budget")
    (slot / "worker.txt").write_text("salvage me\n", encoding="utf-8")

    with patch(
        "agent_control_plane.app.runtime.finalization_service.clean_checkpointed_workspace",
        side_effect=SlotCheckpointError("cleanup failed safely"),
    ):
        control.finish_job(job.job_id, "cancelled")

    item = control.review_inbox.get("agent_job:job-1")
    slot_state = control.slots.inspect_slot("app-1")
    assert item.delivery_status == "checkpoint_failed"
    assert item.checkpoint_ref is not None
    assert item.checkpoint_sha is not None
    assert "cleanup failed safely" in (item.checkpoint_error or "")
    assert item.slot_released is False
    assert workspace_state(slot).dirty
    assert slot_state.status == "dirty_after_job"
    assert slot_state.active_job_id is None
    causal = control.store.get_job(job.job_id)
    assert causal.runner_failure == "tool_call_budget"
    assert causal.workspace_disposition == "dirty_after_failure"
    assert causal.checkpoint_disposition == "salvage"


def test_manual_checkpoint_recovers_an_inactive_dirty_terminal_slot(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="preserve")
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-1", status_result="completed")
    (slot / "worker.txt").write_text("recover existing work\n", encoding="utf-8")
    control.finish_job(job.job_id, "completed")
    assert control.slots.inspect_slot("app-1").status == "dirty_after_job"

    payload = control.checkpoint_slot("app-1", job_id=job.job_id)

    assert payload["slot"]["status"] == "available"
    assert payload["inbox"]["delivery_status"] == "checkpointed"
    assert payload["inbox"]["slot_released"] is True
    assert workspace_state(slot).porcelain == ""

    repeated = control.checkpoint_slot("app-1", job_id=job.job_id)
    assert repeated["inbox"]["checkpoint_sha"] == payload["inbox"]["checkpoint_sha"]
    assert repeated["inbox"]["delivery_status"] == "checkpointed"
    assert repeated["inbox"]["slot_released"] is True


def test_existing_checkpoint_ref_damage_blocks_slot_reuse(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="preserve")
    control = AgentControlPlane(config)
    job = _active_slot_job(control, tmp_path, slot, "job-1", status_result="completed")
    (slot / "worker.txt").write_text("recover existing work\n", encoding="utf-8")
    control.finish_job(job.job_id, "completed")
    first = control.checkpoint_slot("app-1", job_id=job.job_id)
    checkpoint_ref = first["inbox"]["checkpoint_ref"]
    assert isinstance(checkpoint_ref, str)
    run_git(slot, "update-ref", checkpoint_ref, "HEAD")

    payload = control.checkpoint_slot("app-1", job_id=job.job_id)

    assert payload["slot"]["status"] == "checkpoint_failed"
    assert payload["inbox"]["delivery_status"] == "checkpoint_failed"
    assert payload["inbox"]["slot_released"] is False
    assert "no longer matches" in payload["inbox"]["checkpoint_error"]


def test_subagent_sync_imports_result_into_the_same_review_inbox(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="preserve")
    rollout = config.defaults.codex_sessions_root / "2026" / "07" / "15" / "rollout.jsonl"  # type: ignore[operator]
    _write_subagent_rollout(rollout, cwd=route)
    control = AgentControlPlane(config)

    first = control.sync_subagent_results(since_hours=None, max_files=20)
    second = control.sync_subagent_results(since_hours=None, max_files=20)

    item = control.review_inbox.get("codex_subagent:subagent-1")
    assert first["imported"] == 1
    assert second["imported"] == 1
    assert first["items"] == [
        {
            "item_id": "codex_subagent:subagent-1",
            "source_completed_at": "1970-01-01T00:00:01+00:00",
            "parent_thread_id": "parent-aborted",
            "agent_path": "/root/reviewer",
        }
    ]
    assert first["items_truncated"] is False
    assert item.parent_thread_id == "parent-aborted"
    assert item.route == "app"
    assert item.result_excerpt == "durable review verdict"
    assert item.rollout_path == rollout
    assert len(control.review_inbox.list_items(review_status=None)) == 1


def test_inbox_list_and_sync_payloads_are_compact_but_show_keeps_the_durable_excerpt(
    tmp_path: Path,
) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    control = AgentControlPlane(_config(tmp_path, route, slot, terminal_slot_policy="preserve"))
    control.review_inbox.upsert(
        ReviewInboxDraft(
            source_kind="codex_subagent",
            source_id="large-result",
            source_status="completed",
            delivery_status="ready",
            result_excerpt="x" * 5000,
        )
    )

    listed = control.list_review_inbox(review_status="pending")
    shown = control.get_review_inbox_item("codex_subagent:large-result")

    assert len(listed[0]["result_excerpt"]) == 600
    assert listed[0]["result_excerpt_truncated"] is True
    assert len(shown["result_excerpt"]) == 4000


def test_subagent_sync_returns_only_five_lightweight_item_references(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    slot = _committed_repo(tmp_path / "slots" / "app-1")
    config = _config(tmp_path, route, slot, terminal_slot_policy="preserve")
    sessions_root = config.defaults.codex_sessions_root
    assert sessions_root is not None
    for index in range(7):
        _write_subagent_rollout(
            sessions_root / f"rollout-{index}.jsonl",
            cwd=route,
            thread_id=f"subagent-{index}",
            completed_at=index + 1,
        )
    control = AgentControlPlane(config)

    payload = control.sync_subagent_results(since_hours=None, max_files=20)

    assert payload["imported"] == 7
    assert len(payload["items"]) == 5
    assert payload["items_truncated"] is True
    assert payload["items"][0]["item_id"] == "codex_subagent:subagent-6"
    assert "result_excerpt" not in payload["items"][0]


def _active_slot_job(
    control: AgentControlPlane,
    root: Path,
    slot: Path,
    job_id: str,
    *,
    status_result: str,
    workspace_access: str = "ide_mcp",
    expected_result_status: str = "completed",
    controller_gate_mode: str = "full",
):
    run_dir = root / "runs" / job_id
    run_dir.mkdir(parents=True)
    result_path = root / ".agent-work" / "tasks" / "task-1" / "result.md"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(f"Status: {status_result}\n", encoding="utf-8")
    job = control.store.create_job(
        job_id=job_id,
        task_id="task-1",
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
        workspace_access=workspace_access,
        slot_name="app-1",
        expected_result_status=expected_result_status,
        controller_gate_mode=controller_gate_mode,
    )
    control.slots.sync_configured_slots()
    control.slot_store.acquire_slot("app-1", job.job_id)
    quality_contract = resolve_native_quality_contract(
        control.config,
        job.route,
        workspace_access=job.workspace_access,
        read_only=job.read_only,
    )
    write_native_quality_contract(job.run_dir, quality_contract)
    return job


def _finish_review_ready_plan(
    control: AgentControlPlane,
    slot: Path,
    job,
    *,
    plan_id: str,
    result_status: str = "completed",
) -> None:
    control.create_plan(
        plan_id=plan_id,
        title="Atomic acceptance plan",
        tasks=(PlanTaskDefinition("task", "Task"),),
    )
    control.bind_plan_job(plan_id, "task", job.job_id)
    job.result_path.write_text(
        f"""Status: {result_status}

Changed files:
- worker.txt

What changed:
- Added the reviewed worker result.

Verification performed:
- focused test passed

Not verified / remaining risks:
- none
""",
        encoding="utf-8",
    )
    job.result_path.with_name("verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": result_status,
                "changed_files": [{"path": "worker.txt", "change": "added"}],
                "checks": [
                    {
                        "command": "pytest -q tests/test_worker.py",
                        "cwd": ".",
                        "outcome": "passed",
                        "exit_code": 0,
                        "summary": "focused test passed",
                    }
                ],
                "unverified": [],
            }
        ),
        encoding="utf-8",
    )
    (slot / "worker.txt").write_text("reviewed result\n", encoding="utf-8")
    control.finish_job(job.job_id, result_status)


def _config(
    root: Path,
    route: Path,
    slot: Path,
    *,
    terminal_slot_policy: str,
    native_quality_policy: str | None = None,
    native_quality_gates: tuple[NativeQualityGateConfig, ...] = (),
) -> ControlConfig:
    defaults = ControlDefaults(
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        prepare_slots=False,
        guardrail_poll_sec=1.0,
        forbidden_status_globs=(),
        codex_sessions_root=root / "sessions",
    )
    defaults = replace(defaults, terminal_slot_policy=terminal_slot_policy)
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
                    native_quality_policy=native_quality_policy,
                    native_quality_gates=native_quality_gates,
                )
            }
        ),
        slots=MappingProxyType({"app-1": SlotConfig(name="app-1", route="app", path=slot)}),
        slot_prepare=(),
    )


def _write_completed_result(
    result_path: Path,
    *,
    command: str,
    changed_file: str = "worker.txt",
) -> None:
    result_path.write_text(
        f"""Status: completed

Changed files:
- {changed_file}

What changed:
- Added a worker result.

Verification performed:
- mandatory quality gate passed

Not verified / remaining risks:
- none
""",
        encoding="utf-8",
    )
    result_path.with_name("verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [{"path": changed_file, "change": "added"}],
                "checks": [
                    {
                        "command": command,
                        "cwd": ".",
                        "outcome": "passed",
                        "exit_code": 0,
                        "summary": "worker reported the mandatory gate passed",
                    }
                ],
                "unverified": [],
            }
        ),
        encoding="utf-8",
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


def _write_subagent_rollout(
    path: Path,
    *,
    cwd: Path,
    thread_id: str = "subagent-1",
    completed_at: int = 1,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": "2026-07-15T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": "parent-aborted",
                "cwd": str(cwd),
                "thread_source": "subagent",
                "agent_path": "/root/reviewer",
                "agent_nickname": "Reviewer",
            },
        },
        {
            "timestamp": "2026-07-15T10:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "durable review verdict",
                "completed_at": completed_at,
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
