from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from agent_control_plane.app.runtime.orchestrator import (
    AgentControlPlane,
    PolicyError,
    StartOptions,
)
from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.features.agent_runner import (
    AGY_BACKEND,
    CODEX_BACKEND,
    QuotaDecision,
)
from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunResult
from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
)


class OrchestratorRunnerResultTest(unittest.TestCase):
    def test_start_job_initializes_codex_coordination_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="task-1",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            task_dir = control.config.coordination_root / "tasks" / "task-1"
            progress_text = (task_dir / "agent-progress.md").read_text(encoding="utf-8")
            result_text = (task_dir / "result.md").read_text(encoding="utf-8")
            result_state = inspect_result(task_dir / "result.md", 0.0)

            self.assertEqual(job.status, "queued")
            self.assertIn(job.job_id, progress_text)
            self.assertIn(str(workspace), progress_text)
            self.assertIn("Awaiting agent execution", result_text)
            self.assertFalse(result_state.done)

    def test_mechanical_quality_tier_starts_on_luna_without_changing_deep_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-mechanical")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="task-mechanical",
                        route="main",
                        backend=CODEX_BACKEND,
                        codex_quality_tier="mechanical",
                    )
                )

            self.assertEqual(job.codex_quality_tier, "mechanical")
            self.assertEqual(job.codex_model, "gpt-5.6-luna")
            self.assertEqual(job.codex_reasoning_effort, "low")
            self.assertEqual(control.config.defaults.codex_quality_tier, "deep")

    def test_capacity_escalates_mechanical_job_to_terra_on_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-capacity",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-luna",
                codex_reasoning_effort="low",
                codex_quality_tier="mechanical",
            )
            runner = _CapacityThenCompletedRunner()

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(finished.codex_model, "gpt-5.6-terra")
            self.assertEqual(finished.codex_reasoning_effort, "medium")
            self.assertEqual(
                [spec.codex_model for spec in runner.specs],
                [
                    "gpt-5.6-luna",
                    "gpt-5.6-terra",
                ],
            )
            self.assertIsNone(runner.specs[0].codex_resume_thread_id)
            self.assertEqual(runner.specs[1].codex_resume_thread_id, "thread-capacity")

    def test_quota_wait_retries_without_starting_model_until_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-quota",
                backend=CODEX_BACKEND,
            )
            broker = _SequenceQuotaBroker()
            control.quota_broker = broker  # type: ignore[assignment]

            with patch("agent_control_plane.app.runtime.orchestrator.time.sleep"):
                acquired = control._wait_for_codex_quota(job)

            self.assertTrue(acquired)
            self.assertEqual(broker.calls, 2)
            self.assertEqual(control.store.get_job(job.job_id).status, "running")

    def test_read_only_slot_job_skips_ide_and_dependency_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_path = _git_repo(root / "repo", "main")
            slot_path = _git_repo(root / "slots" / "main-1", "main")
            control = AgentControlPlane(_config_with_slot(root, route_path, slot_path))
            _brief(control.config.coordination_root, "task-read-only")

            with (
                patch.object(control.slots, "ensure_ide_root_module") as ensure_ide_root,
                patch.object(control.slots, "prepare_slot") as prepare_slot,
                patch.object(control, "_launch_worker", return_value=123),
            ):
                job = control.start_job(
                    StartOptions(
                        task_id="task-read-only",
                        route="main",
                        slot="main-1",
                        read_only=True,
                    )
                )

            self.assertEqual(job.status, "queued")
            ensure_ide_root.assert_not_called()
            prepare_slot.assert_not_called()

    def test_blocked_start_does_not_prepare_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_path = _git_repo(root / "repo", "main")
            slot_path = _git_repo(root / "slots" / "main-1", "main")
            control = AgentControlPlane(_config_with_slot(root, route_path, slot_path))

            with (
                patch.object(control.slots, "ensure_ide_root_module") as ensure_ide_root,
                patch.object(control.slots, "prepare_slot") as prepare_slot,
                self.assertRaisesRegex(PolicyError, "Task brief not found"),
            ):
                control.start_job(
                    StartOptions(
                        task_id="missing-brief",
                        route="main",
                        slot="main-1",
                    )
                )

            ensure_ide_root.assert_not_called()
            prepare_slot.assert_not_called()

    def test_reusing_task_id_does_not_overwrite_coordination_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-duplicate")
            result_path = (
                control.config.coordination_root / "tasks" / "task-duplicate" / "result.md"
            )

            with patch.object(control, "_launch_worker", return_value=123):
                control.start_job(StartOptions(task_id="task-duplicate", route="main"))
                result_path.write_text("sentinel\n", encoding="utf-8")

                with self.assertRaisesRegex(PolicyError, "Task ID already exists"):
                    control.start_job(StartOptions(task_id="task-duplicate", route="main"))

            self.assertEqual(result_path.read_text(encoding="utf-8"), "sentinel\n")

    def test_blocked_runner_result_finishes_job_as_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1")

            with patch(
                "agent_control_plane.app.runtime.orchestrator.PtyAgyRunner",
                return_value=_BlockedRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "blocked")
            self.assertIn("workspace trust prompt", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("workspace trust prompt", result_text)

    def test_guardrail_detects_changes_to_preexisting_forbidden_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            (workspace / "uv.lock").write_text("before\n", encoding="utf-8")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1", allow_dirty=True)

            with patch(
                "agent_control_plane.app.runtime.orchestrator.PtyAgyRunner",
                return_value=_MutatingForbiddenFileRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("uv.lock", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("uv.lock", result_text)

    def test_codex_dirty_diff_guardrail_stops_large_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            tracked_file = workspace / "tracked.py"
            tracked_file.write_text("base\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], workspace)
            _run(
                [
                    "git",
                    "-c",
                    "user.name=Agy Test",
                    "-c",
                    "user.email=agy@example.test",
                    "commit",
                    "-m",
                    "seed",
                ],
                workspace,
            )
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-1",
                backend=CODEX_BACKEND,
            )

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=_LargeDirtyCodexRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("Codex dirty diff exceeded", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("tracked.py", result_text)
            self.assertIn("guardrail.patch", result_text)
            self.assertTrue((job.run_dir / "guardrail.patch").exists())

    def test_codex_dirty_diff_guardrail_limits_growth_for_resumed_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            tracked_file = workspace / "tracked.py"
            tracked_file.write_text("base\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], workspace)
            _run(
                [
                    "git",
                    "-c",
                    "user.name=Agy Test",
                    "-c",
                    "user.email=agy@example.test",
                    "commit",
                    "-m",
                    "seed",
                ],
                workspace,
            )
            tracked_file.write_text(
                "".join(f"baseline {index}\n" for index in range(10)),
                encoding="utf-8",
            )
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-1",
                backend=CODEX_BACKEND,
                allow_dirty=True,
            )

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=_LargeDirtyCodexRunner(),
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("Codex dirty diff exceeded", finished.last_error or "")
            self.assertIn("baseline", finished.last_error or "")
            self.assertIn("growth", finished.last_error or "")

    def test_slot_job_guardrail_detects_route_root_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_root = _git_repo(root / "repo", "main")
            slot = _git_repo(root / "worktrees" / "slot-1", "main")
            control = AgentControlPlane(_config(root, route_root))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                slot,
                "job-1",
                backend=CODEX_BACKEND,
                slot_name="dev-1",
            )

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=_MutatingRouteRootRunner(route_root),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("route root outside assigned workspace", finished.last_error or "")
            self.assertIn("wrong-root.py", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("wrong-root.py", result_text)
            self.assertTrue((job.run_dir / "route-root-guardrail-status.txt").exists())
            self.assertTrue((job.run_dir / "route-root-guardrail.patch").exists())

    def test_failed_runner_attempts_write_blocked_result_before_finishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1")

            with patch(
                "agent_control_plane.app.runtime.orchestrator.PtyAgyRunner",
                return_value=_TimeoutRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "failed")
            self.assertIn("No progress before timeout", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("No progress before timeout", result_text)

    def test_slot_release_status_preserves_dirty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1")
            (workspace / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            status, note = control._slot_release_status(job, "cancelled")

            self.assertEqual(status, "dirty_after_job")
            self.assertIn("finished cancelled with dirty workspace", note or "")
            self.assertIn("dirty.txt", note or "")


class _CapacityThenCompletedRunner:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    def run(self, spec: Any, **_kwargs: Any) -> AgyRunResult:
        self.specs.append(spec)
        if len(self.specs) == 1:
            return AgyRunResult(
                status="capacity",
                completed=False,
                exit_code=1,
                result_status=None,
                message="usage limit reached",
                metrics=_attempt_metrics(thread_id="thread-capacity"),
            )
        spec.result_path.parent.mkdir(parents=True, exist_ok=True)
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="completed after escalation",
            metrics=_attempt_metrics(thread_id="thread-capacity"),
        )


class _SequenceQuotaBroker:
    def __init__(self) -> None:
        self.calls = 0

    def try_acquire(self, _job_id: str, *, worker_pid: int) -> QuotaDecision:
        self.calls += 1
        if self.calls == 1:
            return QuotaDecision(
                acquired=False,
                reason="concurrency_limit",
                active_jobs=2,
            )
        return QuotaDecision(acquired=True, reason=None, active_jobs=1)


def _attempt_metrics(*, thread_id: str) -> AttemptMetrics:
    return AttemptMetrics(
        duration_sec=1.0,
        thread_id=thread_id,
        event_count=1,
        turn_completed=False,
        usage_available=False,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        tool_calls=0,
        failed_tool_calls=0,
        error_events=0,
        tool_counts=(),
        estimated_credits=None,
        estimated_api_usd=None,
        rate_card_version="test",
        event_log_path=None,
    )


class _BlockedRunner:
    def run(self, *args: Any, **kwargs: Any) -> AgyRunResult:
        return AgyRunResult(
            status="blocked",
            completed=False,
            exit_code=None,
            result_status=None,
            message="Antigravity CLI is waiting for the workspace trust prompt.",
        )


class _MutatingForbiddenFileRunner:
    def run(self, spec: Any, **kwargs: Any) -> AgyRunResult:
        (spec.workspace_path / "uv.lock").write_text("after\n", encoding="utf-8")
        kwargs["cancel_requested"]()
        return AgyRunResult(
            status="cancelled",
            completed=False,
            exit_code=None,
            result_status=None,
            message="runner stopped after guardrail check",
        )


class _LargeDirtyCodexRunner:
    def run(self, spec: Any, **kwargs: Any) -> AgyRunResult:
        changed_lines = "".join(f"changed {index}\n" for index in range(520))
        (spec.workspace_path / "tracked.py").write_text(changed_lines, encoding="utf-8")
        kwargs["cancel_requested"]()
        return AgyRunResult(
            status="cancelled",
            completed=False,
            exit_code=None,
            result_status=None,
            message="runner stopped after Codex dirty diff guardrail check",
        )


class _MutatingRouteRootRunner:
    def __init__(self, route_root: Path) -> None:
        self._route_root = route_root

    def run(self, _spec: Any, **kwargs: Any) -> AgyRunResult:
        (self._route_root / "wrong-root.py").write_text("wrong\n", encoding="utf-8")
        kwargs["cancel_requested"]()
        return AgyRunResult(
            status="cancelled",
            completed=False,
            exit_code=None,
            result_status=None,
            message="runner stopped after route-root guardrail check",
        )


class _TimeoutRunner:
    def run(self, *args: Any, **kwargs: Any) -> AgyRunResult:
        return AgyRunResult(
            status="timeout",
            completed=False,
            exit_code=None,
            result_status=None,
            message="No progress before timeout",
        )


def _git_repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], path)
    _run(["git", "checkout", "-b", branch], path)
    return path


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


def _brief(coordination_root: Path, task_id: str) -> None:
    task_dir = coordination_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (coordination_root / "agent-protocol.md").write_text("# Protocol\n", encoding="utf-8")
    (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
    (task_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")


def _create_job(
    control: AgentControlPlane,
    root: Path,
    workspace: Path,
    job_id: str,
    *,
    allow_dirty: bool = False,
    backend: str = AGY_BACKEND,
    slot_name: str | None = None,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    codex_quality_tier: str | None = None,
):
    run_dir = root / "runs" / job_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text("Do work and write result.md", encoding="utf-8")
    return control.store.create_job(
        job_id=job_id,
        task_id="task-1",
        route="main",
        workspace_path=workspace,
        expected_branch="main",
        config_path=root / "workspaces.toml",
        run_dir=run_dir,
        prompt_path=prompt_path,
        result_path=root / ".agent-work" / "tasks" / "task-1" / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=1,
        yolo=False,
        allow_dirty=allow_dirty,
        read_only=False,
        backend=backend,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        codex_quality_tier=codex_quality_tier,
        slot_name=slot_name,
    )


def _config(root: Path, route_path: Path) -> ControlConfig:
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=route_path,
        slot_root=root / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=ControlDefaults(
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=1,
            yolo=False,
            allow_dirty=False,
            prepare_slots=False,
            guardrail_poll_sec=2.0,
            forbidden_status_globs=("uv.lock", ".venv/**"),
        ),
        routes=MappingProxyType(
            {
                "main": RouteConfig(
                    name="main",
                    path=route_path,
                    required_branch="main",
                    worktree_root=root / "worktrees",
                    worktree_base=route_path,
                    source_roots=(Path("src"),),
                    test_roots=(Path("tests"),),
                    exclude_dirs=(),
                )
            }
        ),
        slots=MappingProxyType({}),
        slot_prepare=(),
    )


def _config_with_slot(root: Path, route_path: Path, slot_path: Path) -> ControlConfig:
    config = _config(root, route_path)
    return replace(
        config,
        defaults=replace(config.defaults, prepare_slots=True),
        slots=MappingProxyType(
            {
                "main-1": SlotConfig(
                    name="main-1",
                    route="main",
                    path=slot_path,
                )
            }
        ),
    )


if __name__ == "__main__":
    unittest.main()
