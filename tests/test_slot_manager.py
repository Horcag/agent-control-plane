from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.features.slot_lifecycle.lib.slot_manager import SlotError, SlotManager
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotPrepareCommand,
)


class SlotManagerTest(unittest.TestCase):
    def test_checkout_slot_switches_clean_inactive_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            slot_path = _git_repo(slot_root / "main-1", "slot/main-1")
            _run(["git", "checkout", "-b", "feature/pr"], slot_path)
            _run(["git", "checkout", "slot/main-1"], slot_path)
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", slot_path)
            manager = SlotManager(config, store)

            status = manager.checkout_slot("main-1", branch="feature/pr")

            self.assertEqual(status.branch, "feature/pr")
            self.assertEqual(status.dirty, "")

    def test_cleanup_picks_never_used_slot_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            used_path = _git_repo(slot_root / "used", "slot/used")
            unused_path = _git_repo(slot_root / "unused", "slot/unused")
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("used", "main", used_path)
            store.register_slot("unused", "main", unused_path)
            store.acquire_slot("used", "job-1")
            store.release_slot("used", "job-1")
            manager = SlotManager(config, store)

            decisions = manager.cleanup(max_per_route=1, apply=False)

            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].name, "unused")
            self.assertEqual(decisions[0].action, "would_delete")

    def test_create_slot_uses_route_specific_worktree_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            _git_repo(root / "repo", "main", readme="main\n")
            reports_repo = _git_repo(root / "reports", "release", readme="reports\n")
            config = _config(root, slot_root, reports_repo=reports_repo)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            manager = SlotManager(config, store)

            status = manager.create_slot(
                "reports-1",
                route="reports",
                branch="slot/reports-1",
                start_point="release",
            )

            self.assertEqual(status.branch, "slot/reports-1")
            self.assertEqual((status.path / "README.md").read_text(encoding="utf-8"), "reports\n")

    def test_prepare_slot_skips_prepare_commands_for_other_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            slot_path = _git_repo(slot_root / "reports-1", "slot/reports-1")
            command = SlotPrepareCommand(
                name="frontend_node_modules",
                working_dir=Path("frontend"),
                marker=Path("frontend/node_modules"),
                command=("missing-command",),
                timeout_sec=10,
                routes=("main",),
            )
            config = _config(root, slot_root, slot_prepare=(command,))
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("reports-1", "reports", slot_path)
            manager = SlotManager(config, store)

            result = manager.prepare_slot("reports-1")

            self.assertEqual(result, [])

    def test_acquire_for_job_rejects_dirty_available_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            slot_path = _git_repo(slot_root / "main-1", "slot/main-1")
            (slot_path / "README.md").write_text("dirty\n", encoding="utf-8")
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", slot_path)
            manager = SlotManager(config, store)

            self.assertEqual(manager.inspect_slot("main-1").status, "dirty")
            with self.assertRaisesRegex(SlotError, "dirty"):
                manager.acquire_for_job("main-1", job_id="job-1", route="main")

    def test_acquire_for_job_rejects_dirty_after_failure_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            slot_path = _git_repo(slot_root / "main-1", "slot/main-1")
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", slot_path)
            store.acquire_slot("main-1", "job-1")
            store.release_slot("main-1", "job-1", status="dirty_after_failure")
            manager = SlotManager(config, store)

            with self.assertRaisesRegex(SlotError, "not available"):
                manager.acquire_for_job("main-1", job_id="job-2", route="main")

    def test_acquire_for_job_can_resume_clean_dirty_after_job_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            slot_path = _git_repo(slot_root / "main-1", "slot/main-1")
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", slot_path)
            store.acquire_slot("main-1", "job-1")
            store.release_slot("main-1", "job-1", status="dirty_after_job")
            manager = SlotManager(config, store)

            record = manager.acquire_for_job("main-1", job_id="job-2", route="main")

            self.assertEqual(record.status, "active")
            self.assertEqual(record.active_job_id, "job-2")

    def test_acquire_for_job_can_resume_explicit_dirty_after_job_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            slot_path = _git_repo(slot_root / "main-1", "slot/main-1")
            (slot_path / "README.md").write_text("dirty\n", encoding="utf-8")
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", slot_path)
            store.acquire_slot("main-1", "job-1")
            store.release_slot("main-1", "job-1", status="dirty_after_job")
            manager = SlotManager(config, store)

            record = manager.acquire_for_job(
                "main-1",
                job_id="job-2",
                route="main",
                allow_dirty=True,
            )

            self.assertEqual(record.status, "active")
            self.assertEqual(record.active_job_id, "job-2")

    def test_list_slots_hides_deleted_records_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("active", "main", slot_root / "active")
            store.register_slot("deleted", "main", slot_root / "deleted")
            store.mark_deleted("deleted", note="deleted")
            manager = SlotManager(config, store)

            visible_names = [status.name for status in manager.list_slots()]
            all_statuses = manager.list_slots(include_deleted=True)

            self.assertEqual(visible_names, ["active"])
            self.assertEqual([status.name for status in all_statuses], ["active", "deleted"])
            self.assertEqual(all_statuses[1].status, "deleted")


def _config(
    root: Path,
    slot_root: Path,
    reports_repo: Path | None = None,
    slot_prepare: tuple[SlotPrepareCommand, ...] = (),
) -> ControlConfig:
    routes = {
        "main": RouteConfig(
            name="main",
            path=root / "repo",
            required_branch="main",
            worktree_root=root / "worktrees",
            worktree_base=root / "repo",
            source_roots=(Path("backend"), Path("frontend/src")),
            test_roots=(Path("backend/tests"),),
            exclude_dirs=(),
        )
    }
    if reports_repo is not None:
        routes["reports"] = RouteConfig(
            name="reports",
            path=reports_repo,
            required_branch="release",
            worktree_root=root / "worktrees",
            worktree_base=reports_repo,
            source_roots=(Path("backend/src"), Path("frontend/src")),
            test_roots=(Path("backend/tests"), Path("frontend/tests")),
            exclude_dirs=(),
        )
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=root / "repo",
        slot_root=slot_root,
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
        routes=MappingProxyType(routes),
        slots=MappingProxyType({}),
        slot_prepare=slot_prepare,
    )


def _git_repo(path: Path, branch: str, readme: str = "test\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], path)
    _run(["git", "config", "user.email", "test@example.local"], path)
    _run(["git", "config", "user.name", "Test User"], path)
    (path / "README.md").write_text(readme, encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "initial"], path)
    _run(["git", "checkout", "-B", branch], path)
    return path


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


if __name__ == "__main__":
    unittest.main()
