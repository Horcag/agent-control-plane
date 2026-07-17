from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.features.slot_lifecycle.lib.slot_manager import SlotError, SlotManager
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
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

            decisions = manager.cleanup(max_per_route=1, apply=False, route="main")

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

    def test_inspect_slot_is_read_only_and_explicit_reconcile_marks_clean_slot(self) -> None:
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

            status = manager.inspect_slot("main-1")

            self.assertEqual(status.status, "dirty_after_job")
            self.assertEqual(store.require_slot("main-1").status, "dirty_after_job")

            reconciled = manager.reconcile_clean_slot("main-1")

            self.assertEqual(reconciled.status, "available")
            self.assertEqual(reconciled.note, "reconciled clean workspace")
            self.assertEqual(store.require_slot("main-1").status, "available")

    def test_inspect_slot_preserves_clean_dirty_after_failure_status(self) -> None:
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

            status = manager.inspect_slot("main-1")

            self.assertEqual(status.status, "dirty_after_failure")
            self.assertEqual(store.require_slot("main-1").status, "dirty_after_failure")

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
            store.register_slot("active", "main", _git_repo(slot_root / "active", "slot/active"))
            store.register_slot(
                "deleted",
                "main",
                _git_repo(slot_root / "deleted", "slot/deleted"),
            )
            store.mark_deleted("deleted", note="deleted")
            manager = SlotManager(config, store)

            visible_names = [status.name for status in manager.list_slots()]
            all_statuses = manager.list_slots(include_deleted=True)

            self.assertEqual(visible_names, ["active"])
            self.assertEqual([status.name for status in all_statuses], ["active", "deleted"])
            self.assertEqual(all_statuses[1].status, "deleted")

    def test_configured_inventory_is_read_only_and_uses_current_config_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            configured_path = _git_repo(slot_root / "main-1", "slot/main-1")
            persisted_path = _git_repo(slot_root / "previous-main-1", "slot/previous-main-1")
            config = _config(
                root,
                slot_root,
                slots={"main-1": SlotConfig("main-1", "main", configured_path)},
            )
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", persisted_path)
            persisted = store.require_slot("main-1")
            manager = SlotManager(config, store)

            status = manager.list_slots(route="main")[0]

            self.assertEqual(status.scope, "configured")
            self.assertEqual(status.path, configured_path)
            self.assertIn("persisted path differs", status.problems[0])
            self.assertEqual(store.require_slot("main-1"), persisted)

    def test_configured_slot_without_registry_row_is_visible_without_a_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            configured_path = _git_repo(slot_root / "main-1", "slot/main-1")
            config = _config(
                root,
                slot_root,
                slots={"main-1": SlotConfig("main-1", "main", configured_path)},
            )
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            manager = SlotManager(config, store)

            status = manager.list_slots(route="main")[0]

            self.assertEqual(status.status, "unregistered")
            self.assertEqual(status.scope, "configured")
            self.assertEqual(store.list_slots(), [])

    def test_list_slots_hides_stale_rows_unless_explicitly_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            stale_path = slot_root / "historical"
            config = _config(root, slot_root)
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("historical", "main", stale_path)
            manager = SlotManager(config, store)

            self.assertEqual(manager.list_slots(route="main"), [])
            audited = manager.list_slots(route="main", include_stale=True)

            self.assertEqual(audited[0].scope, "stale")
            self.assertEqual(audited[0].status, "stale")
            self.assertIn("stale registry record", audited[0].problems)

    def test_list_slots_filters_routes_before_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            acp_path = _git_repo(slot_root / "acp-1", "slot/acp-1")
            config = _config(
                root,
                slot_root,
                route_names=("main", "acp"),
                slots={
                    "main-1": SlotConfig("main-1", "main", slot_root / "main-1"),
                    "acp-1": SlotConfig("acp-1", "acp", acp_path),
                },
            )
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-dynamic", "main", slot_root / "main-dynamic")
            store.register_slot("acp-dynamic", "acp", acp_path)
            manager = SlotManager(config, store)

            with patch.object(manager, "inspect_slot", wraps=manager.inspect_slot) as inspect_slot:
                statuses = manager.list_slots(route="acp")

            self.assertEqual([status.name for status in statuses], ["acp-1", "acp-dynamic"])
            self.assertEqual(
                [call.args[0] for call in inspect_slot.call_args_list],
                ["acp-1", "acp-dynamic"],
            )

    def test_acquire_registers_only_selected_configured_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            acp_path = _git_repo(slot_root / "acp-1", "slot/acp-1")
            previous_acp_path = _git_repo(slot_root / "previous-acp-1", "slot/previous-acp-1")
            main_path = _git_repo(slot_root / "main-1", "slot/main-1")
            config = _config(
                root,
                slot_root,
                route_names=("main", "acp"),
                slots={
                    "main-1": SlotConfig("main-1", "main", main_path),
                    "acp-1": SlotConfig("acp-1", "acp", acp_path),
                },
            )
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", main_path)
            store.register_slot("acp-1", "main", previous_acp_path)
            unchanged_main = store.require_slot("main-1")
            manager = SlotManager(config, store)

            record = manager.acquire_for_job("acp-1", job_id="job-1", route="acp")

            self.assertEqual(record.status, "active")
            self.assertEqual(record.route, "acp")
            self.assertEqual(record.path, acp_path)
            self.assertEqual(store.require_slot("main-1"), unchanged_main)

    def test_cleanup_requires_scope_and_only_inspects_the_selected_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            main_path = _git_repo(slot_root / "main-1", "slot/main-1")
            acp_path = _git_repo(slot_root / "acp-1", "slot/acp-1")
            config = _config(root, slot_root, route_names=("main", "acp"))
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            store.register_slot("main-1", "main", main_path)
            store.register_slot("acp-1", "acp", acp_path)
            manager = SlotManager(config, store)

            with self.assertRaisesRegex(SlotError, "scope"):
                manager.cleanup(max_per_route=0)
            with patch.object(manager, "inspect_slot", wraps=manager.inspect_slot) as inspect_slot:
                decisions = manager.cleanup(max_per_route=0, route="acp")

            self.assertEqual([decision.name for decision in decisions], ["acp-1"])
            self.assertEqual([call.args[0] for call in inspect_slot.call_args_list], ["acp-1"])


def _config(
    root: Path,
    slot_root: Path,
    reports_repo: Path | None = None,
    slot_prepare: tuple[SlotPrepareCommand, ...] = (),
    route_names: tuple[str, ...] = ("main",),
    slots: dict[str, SlotConfig] | None = None,
) -> ControlConfig:
    routes = {
        route_name: RouteConfig(
            name=route_name,
            path=root / "repo",
            required_branch="main",
            worktree_root=root / "worktrees",
            worktree_base=root / "repo",
            source_roots=(Path("backend"), Path("frontend/src")),
            test_roots=(Path("backend/tests"),),
            exclude_dirs=(),
        )
        for route_name in route_names
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
        slots=MappingProxyType(slots or {}),
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
