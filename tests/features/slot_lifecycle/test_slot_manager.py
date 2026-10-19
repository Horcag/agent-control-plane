from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import MappingProxyType
from unittest.mock import patch

from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.features.slot_lifecycle.lib.slot_manager import (
    SlotError,
    SlotManager,
    SlotStatus,
)
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
    SlotPrepareCommand,
)
from agent_control_plane.shared.direct_session_claims import direct_session_lock


class SlotManagerTest(unittest.TestCase):
    def test_direct_claim_holds_clean_slot_until_explicit_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "main-1", "slot/main-1")
            config = _config(root, root / "slots")
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", path)
            manager = SlotManager(config, store)
            claim = {
                "slot": "main-1",
                "route": "main",
                "path": str(path),
                "chatId": "chat-1",
                "task": "task",
                "branch": "slot/main-1",
                "createdAt": 1,
            }
            held = manager.claim_direct_session("main-1", claim=claim)
            self.assertEqual(held.status, "direct_owned")
            self.assertEqual(manager.list_slots()[0].as_dict()["direct_owner"]["chatId"], "chat-1")
            self.assertEqual(store.require_slot("main-1").status, "available")
            for mutation in (
                lambda: manager.acquire_for_job(
                    "main-1", job_id="job-1", route="main", allow_dirty=True
                ),
                lambda: manager.checkout_slot("main-1", branch="main"),
                lambda: manager.delete_slot("main-1", force=True),
                lambda: manager.create_slot("main-1", route="main"),
                lambda: manager.prepare_slot("main-1"),
                lambda: manager.reconcile_clean_slot("main-1"),
            ):
                with (
                    self.subTest(mutation=mutation),
                    self.assertRaisesRegex(SlotError, "direct-session"),
                ):
                    mutation()
            decisions = manager.cleanup(max_per_route=0, route="main", force=True, apply=True)
            self.assertEqual(decisions[0].action, "skip")
            with self.assertRaisesRegex(SlotError, "changed"):
                manager.release_direct_session(
                    "main-1", expected_claim={**claim, "chatId": "other"}
                )
            self.assertEqual(
                manager.release_direct_session("main-1", expected_claim=claim).status, "available"
            )
            self.assertEqual(
                manager.acquire_for_job("main-1", job_id="job-1", route="main").active_job_id,
                "job-1",
            )

    def test_invalid_claims_fail_closed_and_do_not_hide_acp_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "main-1", "slot/main-1")
            config = _config(root, root / "slots")
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", path)
            manager = SlotManager(config, store)
            claims = config.coordination_root / "direct-session-claims"
            claims.mkdir(parents=True)
            claim_path = claims / "main-1.json"
            base = {
                "slot": "main-1",
                "route": "main",
                "path": str(path),
                "chatId": "chat",
                "task": "task",
                "branch": "main",
            }
            for payload in (
                "{",
                json.dumps({**base, "path": str(root / "wrong")}),
                json.dumps({**base, "route": "other"}),
                "[]",
            ):
                claim_path.write_text(payload)
                with self.subTest(payload=payload):
                    status = manager.inspect_slot("main-1")
                    self.assertEqual(status.status, "ownership_blocked")
                    self.assertIn("error", status.direct_owner)
                    with self.assertRaisesRegex(SlotError, "direct-session"):
                        manager.delete_slot("main-1", force=True)
            store.acquire_slot("main-1", "job-existing")
            status = manager.inspect_slot("main-1")
            self.assertEqual(status.active_job_id, "job-existing")
            self.assertEqual(status.status, "active")
            with self.assertRaisesRegex(SlotError, "not available"):
                manager.claim_direct_session("main-1", claim=base)

    def test_legacy_chat_alias_is_visible_but_disagreeing_identity_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "main-1", "slot/main-1")
            config = _config(root, root / "slots")
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", path)
            manager = SlotManager(config, store)
            claim_path = config.coordination_root / "direct-session-claims" / "main-1.json"
            claim_path.parent.mkdir(parents=True)
            claim = {
                "slot": "main-1",
                "route": "main",
                "path": str(path),
                "chat": "legacy-chat",
                "task": "task",
                "branch": "main",
            }
            claim_path.write_text(json.dumps(claim))
            status = manager.inspect_slot("main-1")
            self.assertEqual(status.status, "direct_owned")
            self.assertEqual(status.direct_owner["chatId"], "legacy-chat")
            claim_path.write_text(json.dumps({**claim, "chatId": "other-chat"}))
            self.assertEqual(manager.inspect_slot("main-1").status, "ownership_blocked")
            claim_path.unlink()
            claim_path.symlink_to(root / "missing-claim")
            self.assertEqual(manager.inspect_slot("main-1").status, "ownership_blocked")
            with self.assertRaisesRegex(SlotError, "symlink"):
                manager.release_direct_session("main-1", expected_claim=claim)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_special_claim_file_is_a_nonblocking_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "main-1", "slot/main-1")
            config = _config(root, root / "slots")
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", path)
            manager = SlotManager(config, store)
            claim_path = config.coordination_root / "direct-session-claims" / "main-1.json"
            claim_path.parent.mkdir(parents=True)
            os.mkfifo(claim_path)
            self.assertEqual(manager.inspect_slot("main-1").status, "ownership_blocked")
            with self.assertRaisesRegex(SlotError, "regular file"):
                manager.release_direct_session("main-1", expected_claim={})

    def test_mutation_rechecks_claim_after_waiting_for_owner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "main-1", "slot/main-1")
            config = _config(root, root / "slots")
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", path)
            manager = SlotManager(config, store)
            started = Event()

            def checkout() -> None:
                started.set()
                manager.checkout_slot("main-1", branch="new-branch", start_point="HEAD")

            with ThreadPoolExecutor(max_workers=1) as executor:
                with direct_session_lock(config.coordination_root, "main-1"):
                    future = executor.submit(checkout)
                    self.assertTrue(started.wait(timeout=5))
                    self.assertFalse(future.done())
                    claim_path = config.coordination_root / "direct-session-claims" / "main-1.json"
                    claim_path.parent.mkdir(parents=True)
                    claim_path.write_text(
                        json.dumps(
                            {
                                "slot": "main-1",
                                "route": "main",
                                "path": str(path),
                                "chatId": "owner",
                                "task": "task",
                                "branch": "main",
                            }
                        )
                    )
                with self.assertRaisesRegex(SlotError, "direct-session"):
                    future.result(timeout=5)
            self.assertEqual(manager.inspect_slot("main-1").branch, "slot/main-1")

    def test_unclaimed_process_cwd_holds_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "main-1", "slot/main-1")
            manager = SlotManager(_config(root, root / "slots"), SlotStore(root / "jobs.sqlite3"))
            manager._store.register_slot("main-1", "main", path)
            process = subprocess.Popen(["sleep", "30"], cwd=path)
            try:
                status = manager.inspect_slot("main-1")
                self.assertEqual(status.status, "process_owned")
                self.assertIn(process.pid, status.live_process_cwds)
                with self.assertRaisesRegex(SlotError, "process CWDs"):
                    manager.acquire_for_job("main-1", job_id="job-1", route="main")
            finally:
                process.terminate()
                process.wait(timeout=5)
            self.assertEqual(manager.inspect_slot("main-1").status, "available")

    def test_config_remap_preserves_old_workspace_with_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = _git_repo(root / "slots" / "old", "slot/old")
            new = _git_repo(root / "slots" / "new", "slot/new")
            config = _config(
                root, root / "slots", slots={"main-1": SlotConfig("main-1", "main", new)}
            )
            store = SlotStore(root / "jobs.sqlite3")
            before = store.register_slot("main-1", "main", old)
            manager = SlotManager(config, store)
            process = subprocess.Popen(["sleep", "30"], cwd=old)
            try:
                for operation in (
                    lambda: manager.sync_configured_slots(),
                    lambda: manager.acquire_for_job("main-1", job_id="job", route="main"),
                ):
                    with (
                        self.subTest(operation=operation),
                        self.assertRaisesRegex(SlotError, "process CWDs"),
                    ):
                        operation()
                    self.assertEqual(store.require_slot("main-1"), before)
            finally:
                process.terminate()
                process.wait(timeout=5)
            manager.sync_configured_slots()
            self.assertEqual(store.require_slot("main-1").path, new)

    def test_existing_alias_records_cannot_bypass_another_names_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "owner", "slot/owner")
            other = _git_repo(root / "slots" / "alias", "slot/alias")
            config = _config(root, root / "slots")
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("owner", "main", path)
            manager = SlotManager(config, store)
            claim = {
                "slot": "owner",
                "route": "main",
                "path": str(path),
                "chatId": "owner-chat",
                "task": "task",
                "branch": "slot/owner",
            }
            manager.claim_direct_session("owner", claim=claim)
            store.register_slot("alias", "main", other)
            # Seed a legacy duplicate without weakening current registration validation.
            with sqlite3.connect(store.database_path) as db:
                db.execute("update slots set path = ? where name = 'alias'", (str(path),))
            before = store.list_slots()
            self.assertEqual(manager.inspect_slot("alias").status, "ownership_blocked")
            for operation in (
                lambda: manager.checkout_slot("alias", branch="bypass", start_point="HEAD"),
                lambda: manager.acquire_for_job(
                    "alias", job_id="job", route="main", allow_dirty=True
                ),
                lambda: manager.delete_slot("alias", force=True),
                lambda: manager.prepare_slot("alias"),
                lambda: manager.create_slot("alias", route="main"),
            ):
                with (
                    self.subTest(operation=operation),
                    self.assertRaisesRegex(SlotError, "duplicate physical"),
                ):
                    operation()
            self.assertTrue(
                all(
                    item.action == "skip"
                    for item in manager.cleanup(
                        max_per_route=0, route="main", force=True, apply=True
                    )
                )
            )
            self.assertEqual(store.list_slots(), before)
            self.assertEqual(manager.inspect_slot("owner").branch, "slot/owner")
            self.assertEqual(manager.inspect_slot("owner").direct_owner["chatId"], "owner-chat")

    def test_duplicate_configured_paths_cannot_be_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _git_repo(root / "slots" / "same", "slot/same")
            config = _config(
                root,
                root / "slots",
                slots={name: SlotConfig(name, "main", path) for name in ("first", "second")},
            )
            store = SlotStore(root / "jobs.sqlite3")
            manager = SlotManager(config, store)
            with self.assertRaisesRegex(SlotError, "duplicate physical"):
                manager.sync_configured_slots()
            self.assertEqual(store.list_slots(), [])

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

    def test_create_slot_uses_route_slot_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_root = root / "slots"
            reports_slot_root = root / "reports-slots"
            _git_repo(root / "repo", "main", readme="main\n")
            reports_repo = _git_repo(root / "reports", "release", readme="reports\n")
            config = _config(
                root,
                slot_root,
                reports_repo=reports_repo,
                reports_slot_root=reports_slot_root,
            )
            store = SlotStore(root / "runs" / "jobs.sqlite3")
            manager = SlotManager(config, store)

            status = manager.create_slot(
                "reports-1",
                route="reports",
                branch="slot/reports-1",
                start_point="release",
            )

            self.assertEqual(status.path, reports_slot_root / "reports-1")
            self.assertFalse((slot_root / "reports-1").exists())
            self.assertEqual(tuple(status.problems), ())

            # A dynamic record under the route's own slot root stays valid, not stale.
            listed = manager.list_slots(route="reports")
            self.assertEqual([item.name for item in listed], ["reports-1"])
            self.assertEqual(listed[0].scope, "dynamic")

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

    def test_cleanup_apply_fails_closed_when_full_preflight_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SlotManager(_config(root, root / "slots"), SlotStore(root / "jobs.sqlite3"))
            statuses = [_cleanup_status(root, index) for index in range(21)]
            with (
                patch.object(manager, "list_slots", return_value=statuses),
                patch.object(manager, "delete_slot") as delete_slot,
            ):
                with self.assertRaisesRegex(SlotError, "exceeds mutation limit"):
                    manager.cleanup(max_per_route=0, limit=20, apply=True, route="main")
                with self.assertRaisesRegex(SlotError, "exceeds mutation limit"):
                    manager.cleanup(max_per_route=0, limit=20, apply=True, route="main")
            delete_slot.assert_not_called()


def _config(
    root: Path,
    slot_root: Path,
    reports_repo: Path | None = None,
    reports_slot_root: Path | None = None,
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
            slot_root=reports_slot_root,
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


def _cleanup_status(root: Path, index: int) -> SlotStatus:
    return SlotStatus(
        name=f"main-{index}",
        route="main",
        path=root / "slots" / f"main-{index}",
        status="available",
        scope="configured",
        configured=True,
        exists=True,
        is_git_workspace=True,
        branch="main",
        dirty="",
        active_job_id=None,
        use_count=0,
        last_used_at=None,
        note=None,
        problems=(),
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
