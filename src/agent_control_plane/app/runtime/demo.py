from __future__ import annotations

import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane, StartOptions
from agent_control_plane.entities.plan import PlanTaskDefinition
from agent_control_plane.features.agent_runner import AgentRunResult
from agent_control_plane.shared.codex_session_usage import TokenUsage
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
)
from agent_control_plane.shared.git_tools import GitError, run_git

_MANIFEST_NAME = "manifest.json"
_PLAN_ID = "offline-demo-plan"
_TASK_ID = "offline-demo-task"
_SLOT_NAME = "offline-demo-slot"


class OfflineDemoError(ValueError):
    pass


class _DemoRunner:
    def __init__(self, *, fail_first_attempt: bool) -> None:
        self.fail_first_attempt = fail_first_attempt
        self.attempt_count = 0

    def run(self, spec, *, cancel_requested, pid_observed) -> AgentRunResult:
        self.attempt_count += 1
        pid_observed(None)
        spec.log_path.write_text(f"offline demo attempt {self.attempt_count}\n", encoding="utf-8")
        if self.fail_first_attempt and self.attempt_count == 1:
            return AgentRunResult(
                status="failed",
                completed=False,
                exit_code=1,
                result_status=None,
                message="Injected offline demo failure",
            )
        artifact = spec.workspace_path / "demo-artifact.txt"
        artifact.write_text("offline demo artifact\n", encoding="utf-8")
        spec.result_path.write_text(
            "Status: completed\n\n"
            "Changed files:\n- demo-artifact.txt\n\n"
            "What changed:\n- Created a deterministic offline demo artifact.\n\n"
            "Verification performed:\n- Offline demo runner completed after its retry.\n\n"
            "Not verified / remaining risks:\n- none\n",
            encoding="utf-8",
        )
        spec.result_path.with_name("verification.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "completed",
                    "changed_files": [{"path": "demo-artifact.txt", "change": "added"}],
                    "checks": [
                        {
                            "command": "offline-demo deterministic mock runner",
                            "cwd": ".",
                            "outcome": "passed",
                            "exit_code": 0,
                            "summary": "Created the demo artifact after the injected retry.",
                        }
                    ],
                    "unverified": [],
                }
            ),
            encoding="utf-8",
        )
        return AgentRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="Offline demo completed",
        )


def run_demo(output: Path, *, no_failure: bool = False) -> dict[str, Any]:
    root = output.resolve(strict=False)
    if root.exists() and not root.is_dir():
        raise OfflineDemoError(f"Demo output path must be a directory: {root}")
    if root.exists() and any(root.iterdir()):
        raise OfflineDemoError(f"Demo output directory must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    route = root / "generated-repository"
    slot = root / "slots" / _SLOT_NAME
    _create_repository(route)
    _create_slot(route, slot)
    brief = root / ".agent-work" / "tasks" / _TASK_ID / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("Create the deterministic offline demo artifact.\n", encoding="utf-8")
    control = AgentControlPlane(_demo_config(root, route, slot, no_failure=no_failure))
    control.slots.sync_configured_slots()
    runner = _DemoRunner(fail_first_attempt=not no_failure)
    control.job_execution.runner_factory = lambda _backend: runner
    control._launch_worker = lambda _job_id, _instance_id: os.getpid()  # type: ignore[assignment]
    control.create_plan(
        plan_id=_PLAN_ID,
        title="Offline demo plan",
        objective="Exercise the durable ACP handoff pipeline without external services.",
        tasks=(PlanTaskDefinition(_TASK_ID, "Run deterministic offline demo"),),
    )
    job = control.start_job(
        StartOptions(
            task_id=_TASK_ID,
            route="demo",
            backend="codex",
            codex_model="offline-demo",
            codex_reasoning_effort="low",
            slot=_SLOT_NAME,
            workspace_access="native",
            plan_id=_PLAN_ID,
            plan_task_id=_TASK_ID,
            max_restarts=0 if no_failure else 1,
        )
    )
    if job.worker_instance_id is None:
        raise OfflineDemoError("Demo JobLauncher did not assign a worker identity")
    completed = control.run_job(job.job_id, job.worker_instance_id)
    if completed.status != "completed":
        raise OfflineDemoError(f"Demo job did not complete: {completed.status}")
    manifest = {
        "schema_version": 1,
        "job_id": completed.job_id,
        "plan_id": _PLAN_ID,
        "task_id": _TASK_ID,
        "slot_name": _SLOT_NAME,
        "attempt_count": runner.attempt_count,
    }
    _manifest_path(root).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"status": "completed", "output": str(root), **manifest}


def show_demo(root: Path) -> dict[str, Any]:
    output, manifest = _load_manifest(root)
    control = _open_demo_control(output)
    job = control.status_job(_manifest_value(manifest, "job_id"))
    job_id = _manifest_value(manifest, "job_id")
    inbox = control.get_review_inbox_item(f"agent_job:{job_id}")
    inbox["review_ready"] = bool(
        isinstance(inbox.get("verification_bundle"), dict)
        and inbox["verification_bundle"].get("review_ready") is True
    )
    return {
        "output": str(output),
        "job": job,
        "result": control.result_job(job_id),
        "checkpoint": {
            "ref": inbox.get("checkpoint_ref"),
            "sha": inbox.get("checkpoint_sha"),
            "tree_sha": inbox.get("checkpoint_tree_sha"),
        },
        "plan": control.plan_snapshot(_manifest_value(manifest, "plan_id")),
        "slot": control.slots.inspect_slot(_manifest_value(manifest, "slot_name")).as_dict(),
        "inbox": inbox,
        "attempt_count": manifest["attempt_count"],
    }


def accept_demo(root: Path) -> dict[str, Any]:
    output, manifest = _load_manifest(root)
    control = _open_demo_control(output)
    job_id = _manifest_value(manifest, "job_id")
    inbox = control.get_review_inbox_item(f"agent_job:{job_id}")
    if inbox["review_status"] == "accepted":
        return {
            "status": "accepted",
            "plan_id": _manifest_value(manifest, "plan_id"),
            "task_id": _manifest_value(manifest, "task_id"),
            "job_id": job_id,
            "item_id": inbox["item_id"],
            "accepted_sha": inbox.get("checkpoint_sha"),
            "idempotent": True,
        }
    span_id = control.review_metrics.start_span(
        name="Offline demo acceptance",
        session_path=output / "offline-demo-review.jsonl",
        usage=TokenUsage(0, 0, 0, 0),
    )
    try:
        return control.accept_handoff(
            _manifest_value(manifest, "plan_id"),
            _manifest_value(manifest, "task_id"),
            review_span_id=span_id,
            notes="Accepted by the deterministic offline demo.",
        )
    finally:
        control.review_metrics.finish_span(span_id, usage=TokenUsage(0, 0, 0, 0))


def _open_demo_control(root: Path) -> AgentControlPlane:
    route = root / "generated-repository"
    slot = root / "slots" / _SLOT_NAME
    if not route.is_dir() or not slot.is_dir():
        raise OfflineDemoError(f"Demo root is incomplete: {root}")
    control = AgentControlPlane(_demo_config(root, route, slot, no_failure=False))
    control.slots.sync_configured_slots()
    return control


def _load_manifest(root: Path) -> tuple[Path, dict[str, Any]]:
    output = root.resolve(strict=False)
    if not output.is_dir():
        raise OfflineDemoError(f"Demo root is unavailable: {output}")
    try:
        manifest = json.loads(_manifest_path(output).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise OfflineDemoError(f"Demo manifest is unavailable: {output}") from exc
    if not isinstance(manifest, dict) or not all(
        isinstance(manifest.get(key), str) and manifest[key]
        for key in ("job_id", "plan_id", "task_id", "slot_name")
    ):
        raise OfflineDemoError(f"Demo manifest is invalid: {output}")
    if not isinstance(manifest.get("attempt_count"), int):
        raise OfflineDemoError(f"Demo manifest is invalid: {output}")
    return output, manifest


def _manifest_value(manifest: dict[str, Any], key: str) -> str:
    value = manifest[key]
    if not isinstance(value, str):
        raise OfflineDemoError("Demo manifest is invalid")
    return value


def _manifest_path(root: Path) -> Path:
    return root / _MANIFEST_NAME


def _demo_config(root: Path, route: Path, slot: Path, *, no_failure: bool) -> ControlConfig:
    defaults = ControlDefaults(
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0 if no_failure else 1,
        yolo=False,
        allow_dirty=False,
        prepare_slots=False,
        guardrail_poll_sec=1.0,
        forbidden_status_globs=(),
        workspace_access="native",
        native_quality_policy="off",
        terminal_slot_policy="checkpoint",
        runs_layout="flat",
    )
    return ControlConfig(
        config_path=root / "offline-demo.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=route,
        slot_root=root / "slots",
        agy_command="unavailable-agy",
        codex_command="unavailable-codex",
        defaults=defaults,
        routes=MappingProxyType(
            {
                "demo": RouteConfig(
                    name="demo",
                    path=route,
                    required_branch="main",
                    worktree_root=root / "worktrees",
                    worktree_base=route,
                    source_roots=(Path("."),),
                    test_roots=(),
                    exclude_dirs=(),
                    workspace_access="native",
                    native_quality_policy="off",
                )
            }
        ),
        slots=MappingProxyType({_SLOT_NAME: SlotConfig(_SLOT_NAME, "demo", slot)}),
        slot_prepare=(),
    )


def _create_repository(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "checkout", "-b", "main")
    (path / "README.txt").write_text("offline demo base\n", encoding="utf-8")
    _git(path, "add", "README.txt")
    _git(
        path,
        "-c",
        "user.name=ACP Offline Demo",
        "-c",
        "user.email=demo@example.invalid",
        "commit",
        "-m",
        "base",
    )


def _create_slot(route: Path, slot: Path) -> None:
    _git(route, "worktree", "add", "-b", "offline-demo-slot", str(slot), "main")


def _git(path: Path, *args: str) -> None:
    try:
        run_git(path, *args)
    except GitError as exc:
        raise OfflineDemoError("Unable to initialize the local offline demo repository") from exc
