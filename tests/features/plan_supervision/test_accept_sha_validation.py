from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.plan import PlanExecutionSpec, PlanStore, PlanTaskDefinition
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.features.plan_supervision import PlanService
from agent_control_plane.shared.config import ControlConfig, ControlDefaults, RouteConfig


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _committed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "checkout", "-b", "main")
    _git(path, "config", "user.name", "ACP Test")
    _git(path, "config", "user.email", "acp-test@example.invalid")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    return path


def _head_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _config(root: Path, route: Path) -> ControlConfig:
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
                )
            }
        ),
        slots=MappingProxyType({}),
        slot_prepare=(),
    )


def _service(
    tmp_path: Path, *, database_path: Path, config: ControlConfig | None
) -> tuple[PlanService, JobStore, PlanStore]:
    job_store = JobStore(database_path)
    plan_store = PlanStore(database_path)
    service = PlanService(
        coordination_root=tmp_path / ".agent-work",
        job_store=job_store,
        plan_store=plan_store,
        review_inbox=ReviewInboxStore(database_path),
        launch=lambda _claim: pytest.fail("launch should not be called"),
        cancel_job=lambda _job_id: None,
        accept_handoff=lambda *_args, **_kwargs: {},
        verify_continuation_handoff=lambda *_args, **_kwargs: {},
        reconcile_jobs=lambda _job_id=None: {},
        process_is_alive=lambda _pid: False,
        policy_error=RuntimeError,
        config=config,
    )
    return service, job_store, plan_store


def _prepare_reviewable_task(
    tmp_path: Path,
    job_store: JobStore,
    plan_store: PlanStore,
    *,
    plan_id: str,
    route: str = "app",
) -> str:
    job_id = f"job-{plan_id}"
    run_dir = tmp_path / "runs" / job_id
    run_dir.mkdir(parents=True)
    result_path = tmp_path / ".agent-work" / "tasks" / plan_id / "result.md"
    result_path.parent.mkdir(parents=True)
    result_path.write_text("Status: completed\n", encoding="utf-8")
    result_path.with_name("verification.json").write_text(
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
    job = job_store.create_job(
        job_id=job_id,
        task_id=plan_id,
        route=route,
        workspace_path=tmp_path / "workspace",
        expected_branch="main",
        config_path=tmp_path / "workspaces.toml",
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
    )
    plan_store.create_plan(
        plan_id=plan_id,
        title="Plan",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route=route, brief="Implement the task."),
            ),
        ),
    )
    plan_store.bind_job(plan_id, "task", job.job_id)
    job_store.mark_finished(job.job_id, "completed")
    job_store.mark_finalization_completed(job.job_id)
    return job.job_id


def test_accept_plan_task_accepts_a_sha_that_resolves_in_the_route_repo(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    database_path = tmp_path / "jobs.sqlite3"
    service, job_store, plan_store = _service(
        tmp_path, database_path=database_path, config=_config(tmp_path, route)
    )
    _prepare_reviewable_task(tmp_path, job_store, plan_store, plan_id="plan-valid")
    sha = _head_sha(route)

    result = service.accept_plan_task("plan-valid", "task", accepted_sha=sha)

    task = plan_store.get_task("plan-valid", "task")
    assert task["state"] == "completed"
    assert "accept_sha_warning" not in result


def test_accept_plan_task_rejects_a_fabricated_sha(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    database_path = tmp_path / "jobs.sqlite3"
    service, job_store, plan_store = _service(
        tmp_path, database_path=database_path, config=_config(tmp_path, route)
    )
    _prepare_reviewable_task(tmp_path, job_store, plan_store, plan_id="plan-fabricated")
    fabricated_sha = "a" * 40

    with pytest.raises(ValueError, match="does not resolve to a commit"):
        service.accept_plan_task("plan-fabricated", "task", accepted_sha=fabricated_sha)

    task = plan_store.get_task("plan-fabricated", "task")
    assert task["state"] != "completed"


def test_accept_plan_task_rejects_an_abbreviated_sha(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    database_path = tmp_path / "jobs.sqlite3"
    service, job_store, plan_store = _service(
        tmp_path, database_path=database_path, config=_config(tmp_path, route)
    )
    _prepare_reviewable_task(tmp_path, job_store, plan_store, plan_id="plan-short")
    short_sha = _head_sha(route)[:7]

    with pytest.raises(ValueError, match="full 40-hex"):
        service.accept_plan_task("plan-short", "task", accepted_sha=short_sha)

    task = plan_store.get_task("plan-short", "task")
    assert task["state"] != "completed"


def test_accept_plan_task_with_no_sha_keeps_current_behavior(tmp_path: Path) -> None:
    route = _committed_repo(tmp_path / "repo")
    database_path = tmp_path / "jobs.sqlite3"
    service, job_store, plan_store = _service(
        tmp_path, database_path=database_path, config=_config(tmp_path, route)
    )
    _prepare_reviewable_task(tmp_path, job_store, plan_store, plan_id="plan-none")

    result = service.accept_plan_task("plan-none", "task")

    task = plan_store.get_task("plan-none", "task")
    assert task["state"] == "completed"
    assert "accept_sha_warning" not in result


def test_accept_plan_task_warns_when_task_has_no_execution_route(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service, job_store, plan_store = _service(tmp_path, database_path=database_path, config=None)
    job_id = "job-plan-logical"
    run_dir = tmp_path / "runs" / job_id
    run_dir.mkdir(parents=True)
    result_path = tmp_path / ".agent-work" / "tasks" / "plan-logical" / "result.md"
    result_path.parent.mkdir(parents=True)
    result_path.write_text("Status: completed\n", encoding="utf-8")
    result_path.with_name("verification.json").write_text(
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
    job = job_store.create_job(
        job_id=job_id,
        task_id="plan-logical",
        route="app",
        workspace_path=tmp_path / "workspace",
        expected_branch="main",
        config_path=tmp_path / "workspaces.toml",
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
    )
    plan_store.create_plan(
        plan_id="plan-logical",
        title="Plan",
        tasks=(PlanTaskDefinition("task", "Task"),),
    )
    plan_store.bind_job("plan-logical", "task", job.job_id)
    job_store.mark_finished(job.job_id, "completed")
    job_store.mark_finalization_completed(job.job_id)
    fabricated_sha = "b" * 40

    result = service.accept_plan_task("plan-logical", "task", accepted_sha=fabricated_sha)

    task = plan_store.get_task("plan-logical", "task")
    assert task["state"] == "completed"
    assert "not verified" in result["accept_sha_warning"]
