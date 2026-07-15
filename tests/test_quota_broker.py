from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.quota_broker import (
    CodexRateLimitReader,
    GlobalQuotaBroker,
    RateLimitSnapshot,
    codex_job_capacity_units,
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
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
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

    def test_model_and_effort_map_to_stable_capacity_units(self) -> None:
        self.assertEqual(codex_job_capacity_units("gpt-5.6-luna", "high"), 6)
        self.assertEqual(codex_job_capacity_units("gpt-5.6-terra", "medium"), 10)
        self.assertEqual(codex_job_capacity_units("gpt-5.6-sol", "high"), 30)
        self.assertEqual(codex_job_capacity_units("gpt-5.6-luna", "minimal"), 6)
        self.assertEqual(codex_job_capacity_units("unknown-model", "low"), 30)

    def test_rejects_a_zero_burst_limit(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(ValueError, "max_burst_jobs must be positive"),
        ):
            GlobalQuotaBroker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=1,
                max_burst_jobs=0,
            )

    def test_default_burst_allows_extra_cheap_jobs_past_four_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broker = GlobalQuotaBroker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=2,
            )
            decisions = [
                broker.try_acquire(
                    f"luna-{index}",
                    worker_pid=os.getpid(),
                    capacity_units=codex_job_capacity_units("gpt-5.6-luna", "high"),
                )
                for index in range(8)
            ]

            self.assertTrue(all(decision.acquired for decision in decisions))
            self.assertEqual(decisions[4].active_jobs, 5)
            self.assertEqual(decisions[-1].active_jobs, 8)
            self.assertEqual(decisions[-1].active_capacity_units, 48)
            self.assertEqual(decisions[-1].max_capacity_units, 60)

            blocked = broker.try_acquire(
                "luna-9",
                worker_pid=os.getpid(),
                capacity_units=6,
            )
            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "burst_job_limit")

    def test_expensive_jobs_fill_weighted_capacity_before_burst_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broker = GlobalQuotaBroker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=2,
                max_burst_jobs=4,
            )
            self.assertTrue(
                broker.try_acquire("sol-1", worker_pid=os.getpid(), capacity_units=30).acquired
            )
            self.assertTrue(
                broker.try_acquire("sol-2", worker_pid=os.getpid(), capacity_units=30).acquired
            )

            blocked = broker.try_acquire("sol-3", worker_pid=os.getpid(), capacity_units=30)

            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
            self.assertEqual(blocked.active_jobs, 2)
            self.assertEqual(blocked.active_capacity_units, 60)

    def test_existing_lease_resizes_atomically_for_model_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broker = GlobalQuotaBroker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=1,
                max_burst_jobs=2,
            )
            self.assertTrue(
                broker.try_acquire("escalating", worker_pid=os.getpid(), capacity_units=6).acquired
            )
            self.assertTrue(
                broker.try_acquire("other", worker_pid=os.getpid(), capacity_units=6).acquired
            )

            blocked = broker.try_acquire(
                "escalating",
                worker_pid=os.getpid(),
                capacity_units=30,
            )
            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
            self.assertEqual(blocked.active_capacity_units, 12)

            broker.release("other")
            resized = broker.try_acquire(
                "escalating",
                worker_pid=os.getpid(),
                capacity_units=30,
            )
            self.assertTrue(resized.acquired)
            self.assertEqual(resized.active_jobs, 1)
            self.assertEqual(resized.active_capacity_units, 30)

    def test_legacy_leases_migrate_as_full_cost_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            db = sqlite3.connect(database)
            try:
                db.execute(
                    """
                    create table leases
                    (
                        job_id       text primary key,
                        worker_pid   integer not null,
                        acquired_at  real    not null,
                        heartbeat_at real    not null
                    )
                    """
                )
                db.execute(
                    "insert into leases values (?, ?, ?, ?)",
                    ("legacy-sol", os.getpid(), 1.0, 1.0),
                )
                db.commit()
            finally:
                db.close()

            broker = GlobalQuotaBroker(
                database,
                max_concurrent_jobs=1,
                max_burst_jobs=2,
            )
            blocked = broker.try_acquire(
                "new-luna",
                worker_pid=os.getpid(),
                capacity_units=6,
            )

            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
            self.assertEqual(blocked.active_capacity_units, 30)


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
