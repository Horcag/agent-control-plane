from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from agent_control_plane.entities.workspace import StartRequest, WorkspacePolicy
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
)


class WorkspacePolicyTest(unittest.TestCase):
    def test_route_workspace_on_expected_branch_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = _git_repo(root / "repo", "main")
            config = _config(root, repo)
            _brief(config.coordination_root, "task-1")

            check = WorkspacePolicy(config).check_start(
                StartRequest(task_id="task-1", route="main")
            )

            self.assertTrue(check.ok)
            self.assertEqual(check.actual_branch, "main")

    def test_wrong_branch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = _git_repo(root / "repo", "feature")
            config = _config(root, repo)
            _brief(config.coordination_root, "task-1")

            check = WorkspacePolicy(config).check_start(
                StartRequest(task_id="task-1", route="main")
            )

            self.assertFalse(check.ok)
            self.assertTrue(any("Wrong branch" in reason for reason in check.reasons))

    def test_dirty_workspace_is_blocked_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = _git_repo(root / "repo", "main")
            (repo / "new-file.txt").write_text("dirty\n", encoding="utf-8")
            config = _config(root, repo)
            _brief(config.coordination_root, "task-1")

            check = WorkspacePolicy(config).check_start(
                StartRequest(task_id="task-1", route="main")
            )

            self.assertFalse(check.ok)
            self.assertTrue(any("dirty" in reason.lower() for reason in check.reasons))

    def test_worktree_under_allowed_root_can_use_review_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worktree = _git_repo(root / "worktrees" / "task-1", "review/pr-1")
            config = _config(root, root / "route-repo")
            _brief(config.coordination_root, "task-1")

            check = WorkspacePolicy(config).check_start(
                StartRequest(
                    task_id="task-1",
                    route="main",
                    workspace_path=worktree,
                    expected_branch="review/pr-1",
                )
            )

            self.assertTrue(check.ok)

    def test_configured_slot_for_route_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot = _git_repo(root / "slots" / "dev-1", "slot/dev-1")
            config = _config(root, root / "route-repo", slots={"dev-1": ("main", slot)})
            _brief(config.coordination_root, "task-1")

            check = WorkspacePolicy(config).check_start(
                StartRequest(
                    task_id="task-1",
                    route="main",
                    workspace_path=slot,
                    expected_branch="slot/dev-1",
                )
            )

            self.assertTrue(check.ok)

    def test_configured_slot_for_other_route_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot = _git_repo(root / "slots" / "dev-1", "slot/dev-1")
            config = _config(root, root / "route-repo", slots={"dev-1": ("dev", slot)})
            _brief(config.coordination_root, "task-1")

            check = WorkspacePolicy(config).check_start(
                StartRequest(
                    task_id="task-1",
                    route="main",
                    workspace_path=slot,
                    expected_branch="slot/dev-1",
                )
            )

            self.assertFalse(check.ok)
            self.assertTrue(any("allowed worktree root" in reason for reason in check.reasons))


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
    (task_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")


def _config(
    root: Path,
    route_path: Path,
    *,
    slots: dict[str, tuple[str, Path]] | None = None,
) -> ControlConfig:
    coordination_root = root / ".agent-work"
    worktree_root = root / "worktrees"
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=coordination_root,
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=worktree_root,
        worktree_base=route_path,
        slot_root=root / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=ControlDefaults(
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=0,
            yolo=False,
            allow_dirty=False,
            prepare_slots=True,
            guardrail_poll_sec=2.0,
            forbidden_status_globs=("uv.lock", ".venv/**"),
        ),
        routes=MappingProxyType(
            {
                "main": RouteConfig(
                    name="main",
                    path=route_path,
                    required_branch="main",
                    worktree_root=worktree_root,
                    worktree_base=route_path,
                    source_roots=(Path("backend"), Path("frontend/src")),
                    test_roots=(Path("backend/tests"),),
                    exclude_dirs=(),
                )
            }
        ),
        slots=MappingProxyType(
            {
                name: SlotConfig(name=name, route=route, path=path)
                for name, (route, path) in (slots or {}).items()
            }
        ),
        slot_prepare=(),
    )


if __name__ == "__main__":
    unittest.main()
