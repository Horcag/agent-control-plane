from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.quota_broker import (
    CodexRateLimitReader,
    GlobalQuotaBroker,
    RateLimitSnapshot,
)


class GlobalQuotaBrokerTest(unittest.TestCase):
    def test_respects_global_concurrency_across_broker_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            first = GlobalQuotaBroker(database, max_concurrent_jobs=1)
            second = GlobalQuotaBroker(database, max_concurrent_jobs=1)

            acquired = first.try_acquire("job-1", worker_pid=os.getpid())
            blocked = second.try_acquire("job-2", worker_pid=os.getpid())

            self.assertTrue(acquired.acquired)
            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "concurrency_limit")
            self.assertEqual(blocked.active_jobs, 1)

    def test_reclaims_a_lease_owned_by_a_dead_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = GlobalQuotaBroker(database, max_concurrent_jobs=1)

            with patch(
                "agent_control_plane.features.agent_runner.lib.quota_broker._pid_alive",
                side_effect=lambda pid: pid == 202,
            ):
                self.assertTrue(broker.try_acquire("job-1", worker_pid=101).acquired)
                decision = broker.try_acquire("job-2", worker_pid=202)

            self.assertTrue(decision.acquired)
            self.assertEqual(decision.active_jobs, 1)

    def test_rate_limit_soft_cap_defers_without_acquiring_a_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            snapshot = RateLimitSnapshot(
                used_percent=81.0,
                resets_at=2_000.0,
                observed_at=1_000.0,
            )
            broker = GlobalQuotaBroker(
                database,
                max_concurrent_jobs=2,
                soft_limit_percent=75.0,
                rate_limit_reader=lambda: snapshot,
                clock=lambda: 1_100.0,
            )

            decision = broker.try_acquire("job-1", worker_pid=101)

            self.assertFalse(decision.acquired)
            self.assertEqual(decision.reason, "rate_limit_soft_cap")
            self.assertEqual(decision.retry_after_sec, 900.0)
            self.assertEqual(decision.active_jobs, 0)

    def test_release_makes_capacity_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = GlobalQuotaBroker(database, max_concurrent_jobs=1)

            self.assertTrue(broker.try_acquire("job-1", worker_pid=101).acquired)
            broker.release("job-1")

            self.assertTrue(broker.try_acquire("job-2", worker_pid=202).acquired)


class CodexRateLimitReaderTest(unittest.TestCase):
    def test_reads_latest_primary_snapshot_from_recent_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions" / "2026" / "07" / "11"
            sessions.mkdir(parents=True)
            rollout = sessions / "rollout-thread.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-11T06:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 79.0,
                                    "resets_at": 2_000,
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            snapshot = CodexRateLimitReader(Path(temp) / "sessions").latest()

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.used_percent, 79.0)
            self.assertEqual(snapshot.resets_at, 2_000.0)


if __name__ == "__main__":
    unittest.main()
