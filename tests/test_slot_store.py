from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_control_plane.entities.slot import SlotStore, SlotStoreError


class SlotStoreTest(unittest.TestCase):
    def test_slot_acquire_release_tracks_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", root / "slots" / "main-1")

            active = store.acquire_slot("main-1", "job-1")

            self.assertEqual(active.status, "active")
            self.assertEqual(active.active_job_id, "job-1")
            self.assertEqual(active.use_count, 1)
            self.assertIsNotNone(active.last_used_at)

            with self.assertRaises(SlotStoreError):
                store.acquire_slot("main-1", "job-2")

            released = store.release_slot("main-1", "job-1")
            self.assertEqual(released.status, "available")
            self.assertIsNone(released.active_job_id)

    def test_release_can_preserve_dirty_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("dev-1", "dev", root / "slots" / "dev-1")
            store.acquire_slot("dev-1", "job-1")

            released = store.release_slot(
                "dev-1",
                "job-1",
                status="dirty_after_failure",
                note="job failed with dirty workspace",
            )

            self.assertEqual(released.status, "dirty_after_failure")
            self.assertIsNone(released.active_job_id)
            self.assertEqual(released.note, "job failed with dirty workspace")

    def test_inactive_status_updates_cannot_clear_a_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", root / "slots" / "main-1")
            store.acquire_slot("main-1", "job-1")

            with self.assertRaises(SlotStoreError):
                store.mark_available("main-1")

            active = store.require_slot("main-1")
            self.assertEqual(active.status, "active")
            self.assertEqual(active.active_job_id, "job-1")

    def test_deleted_or_quarantined_slot_cannot_be_acquired_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SlotStore(root / "jobs.sqlite3")
            store.register_slot("main-1", "main", root / "slots" / "main-1")
            store.mark_deleted("main-1")

            with self.assertRaises(SlotStoreError):
                store.acquire_slot("main-1", "job-1")

    def test_two_concurrent_acquisitions_produce_exactly_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            SlotStore(database).register_slot("main-1", "main", root / "slots" / "main-1")

            def acquire(job_id: str) -> str:
                try:
                    return SlotStore(database).acquire_slot("main-1", job_id).active_job_id or ""
                except SlotStoreError:
                    return "rejected"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(acquire, ("job-1", "job-2")))

            self.assertEqual(outcomes.count("rejected"), 1)
            owner = SlotStore(database).require_slot("main-1").active_job_id
            self.assertIn(owner, {"job-1", "job-2"})

    def test_config_sync_does_not_erase_lifecycle_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SlotStore(root / "jobs.sqlite3")
            path = root / "slots" / "main-1"
            store.register_slot("main-1", "main", path, note="configured")
            store.acquire_slot("main-1", "job-1")
            store.release_slot("main-1", "job-1", note="checkpoint ref abc")

            synced = store.register_slot("main-1", "main", path, note="configured")

            self.assertEqual(synced.note, "checkpoint ref abc")


if __name__ == "__main__":
    unittest.main()
