from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
