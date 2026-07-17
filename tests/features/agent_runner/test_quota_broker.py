from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModelMetadata,
    ModelCatalog,
)
from agent_control_plane.features.agent_runner.lib.quota_broker import (
    CodexRateLimitReader,
    GlobalQuotaBroker,
    QuotaDomain,
    RateLimitSnapshot,
    codex_job_capacity_units,
)


def build_turn_context_event(model: str | None, *, timestamp: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if model is not None:
        payload["model"] = model
    return {
        "type": "turn_context",
        "timestamp": timestamp,
        "payload": payload,
    }


def build_token_count_event(
    used_percent: float,
    resets_at: float,
    *,
    timestamp: str,
    payload_model: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "token_count",
        "rate_limits": {
            "primary": {"used_percent": used_percent, "resets_at": resets_at},
        },
    }
    if payload_model is not None:
        payload["model"] = payload_model
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": payload,
    }


def write_jsonl_events(path: Path, *events: dict[str, Any]) -> None:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _catalog(*metadata: CatalogModelMetadata) -> ModelCatalog:
    return ModelCatalog(
        models={},
        metadata={item.model.lower(): item for item in metadata},
        cache_status="loaded",
    )


class GlobalQuotaBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = _catalog(
            CatalogModelMetadata(
                model="gpt-5.6-luna",
                capacity_units=(("minimal", 6), ("high", 6)),
            ),
            CatalogModelMetadata(
                model="gpt-5.6-terra",
                capacity_units=(("medium", 10),),
            ),
            CatalogModelMetadata(
                model="gpt-5.6-sol",
                capacity_units=(("high", 30),),
            ),
            CatalogModelMetadata(
                model="gpt-5.3-codex-spark",
                quota_domain="spark",
            ),
        )

    def _broker(self, *args: Any, **kwargs: Any) -> GlobalQuotaBroker:
        kwargs.setdefault("catalog", self.catalog)
        return GlobalQuotaBroker(*args, **kwargs)

    def test_catalog_metadata_defines_an_arbitrary_quota_domain(self) -> None:
        catalog = _catalog(
            CatalogModelMetadata(
                model="future-codex",
                quota_domain="expedited",
                capacity_units=(("max", 12),),
            )
        )
        domains = (
            QuotaDomain(
                "primary", max_concurrent_jobs=1, max_burst_jobs=2, soft_limit_percent=75.0
            ),
            QuotaDomain(
                "expedited",
                max_concurrent_jobs=2,
                max_burst_jobs=3,
                soft_limit_percent=90.0,
            ),
        )
        snapshot = RateLimitSnapshot(
            used_percent=10.0,
            resets_at=2_000.0,
            observed_at=1_000.0,
            quota_domain="expedited",
        )
        with tempfile.TemporaryDirectory() as temp:
            broker = GlobalQuotaBroker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=1,
                catalog=catalog,
                quota_domains=domains,
                rate_limit_reader=lambda domain: snapshot if domain == "expedited" else None,
                clock=lambda: 1_100.0,
            )

            decision = broker.try_acquire(
                "future-job",
                worker_pid=os.getpid(),
                model="future-codex",
                capacity_units=codex_job_capacity_units("future-codex", "max", catalog),
            )

        self.assertTrue(decision.acquired)
        self.assertEqual(decision.quota_domain, "expedited")
        self.assertEqual(decision.active_capacity_units, 12)
        self.assertEqual(decision.max_capacity_units, 60)

    def test_respects_global_concurrency_across_broker_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            first = self._broker(database, max_concurrent_jobs=1)
            second = self._broker(database, max_concurrent_jobs=1)

            acquired = first.try_acquire("job-1", worker_pid=os.getpid())
            blocked = second.try_acquire("job-2", worker_pid=os.getpid())

            self.assertTrue(acquired.acquired)
            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
            self.assertEqual(blocked.active_jobs, 1)

    def test_reclaims_a_lease_owned_by_a_dead_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(database, max_concurrent_jobs=1)
            live_pid = os.getpid()
            dead_pid = live_pid + 1

            with patch(
                "agent_control_plane.features.agent_runner.lib.quota_broker._pid_alive",
                side_effect=lambda pid: pid == dead_pid,
            ):
                self.assertTrue(broker.try_acquire("job-1", worker_pid=live_pid).acquired)
                decision = broker.try_acquire("job-2", worker_pid=dead_pid)

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
            broker = self._broker(
                database,
                max_concurrent_jobs=2,
                soft_limit_percent=75.0,
                rate_limit_reader=lambda _domain: snapshot,
                clock=lambda: 1_100.0,
            )

            decision = broker.try_acquire("job-1", worker_pid=os.getpid())

            self.assertFalse(decision.acquired)
            self.assertEqual(decision.reason, "rate_limit_soft_cap")
            self.assertEqual(decision.retry_after_sec, 900.0)
            self.assertEqual(decision.active_jobs, 0)

    def test_primary_snapshot_does_not_throttle_spark_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            snapshot = RateLimitSnapshot(
                used_percent=81.0,
                resets_at=2_000.0,
                observed_at=1_000.0,
            )
            broker = self._broker(
                database,
                max_concurrent_jobs=2,
                soft_limit_percent=75.0,
                spark_soft_limit_percent=100.0,
                rate_limit_reader=lambda _domain: snapshot,
                clock=lambda: 1_100.0,
            )

            primary = broker.try_acquire(
                "job-primary",
                worker_pid=os.getpid(),
                model="gpt-5.6-terra",
            )
            spark = broker.try_acquire(
                "job-spark",
                worker_pid=os.getpid(),
                model="gpt-5.3-codex-spark",
            )

            self.assertFalse(primary.acquired)
            self.assertEqual(primary.reason, "rate_limit_soft_cap")
            self.assertEqual(primary.quota_domain, "primary")
            self.assertTrue(spark.acquired)
            self.assertEqual(spark.quota_domain, "spark")
            self.assertEqual(spark.active_jobs, 1)

    def test_rate_limit_soft_cap_is_domain_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"

            snapshots = {
                "primary": RateLimitSnapshot(
                    used_percent=81.0,
                    resets_at=2_000.0,
                    observed_at=1_000.0,
                    quota_domain="primary",
                ),
                "spark": RateLimitSnapshot(
                    used_percent=70.0,
                    resets_at=2_000.0,
                    observed_at=1_000.0,
                    quota_domain="spark",
                ),
            }

            def reader(quota_domain: str) -> RateLimitSnapshot:
                return snapshots[quota_domain]

            broker = self._broker(
                database,
                max_concurrent_jobs=2,
                soft_limit_percent=75.0,
                spark_soft_limit_percent=75.0,
                rate_limit_reader=reader,
                clock=lambda: 1_100.0,
            )

            primary = broker.try_acquire(
                "job-primary",
                worker_pid=os.getpid(),
                model="gpt-5.6-terra",
            )
            spark = broker.try_acquire(
                "job-spark",
                worker_pid=os.getpid(),
                model="gpt-5.3-codex-spark",
            )

            self.assertFalse(primary.acquired)
            self.assertEqual(primary.reason, "rate_limit_soft_cap")
            self.assertEqual(primary.quota_domain, "primary")
            self.assertTrue(spark.acquired)
            self.assertEqual(spark.quota_domain, "spark")

    def test_lease_records_quota_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(
                database,
                max_concurrent_jobs=1,
            )
            worker_pid = os.getpid()
            self.assertTrue(
                broker.try_acquire(
                    "spark-1", worker_pid=worker_pid, model="gpt-5.3-codex-spark"
                ).acquired
            )

            db = sqlite3.connect(database)
            try:
                row = db.execute(
                    "select quota_domain from leases where job_id = ?",
                    ("spark-1",),
                ).fetchone()
            finally:
                db.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[0], "spark")

    def test_release_makes_capacity_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(database, max_concurrent_jobs=1)
            worker_pid = os.getpid()

            self.assertTrue(broker.try_acquire("job-1", worker_pid=worker_pid).acquired)
            broker.release("job-1")
            self.assertTrue(broker.try_acquire("job-2", worker_pid=worker_pid).acquired)

    def test_model_and_effort_map_to_stable_capacity_units(self) -> None:
        self.assertEqual(codex_job_capacity_units("gpt-5.6-luna", "high", self.catalog), 6)
        self.assertEqual(codex_job_capacity_units("gpt-5.6-terra", "medium", self.catalog), 10)
        self.assertEqual(codex_job_capacity_units("gpt-5.6-sol", "high", self.catalog), 30)
        self.assertEqual(codex_job_capacity_units("gpt-5.6-luna", "minimal", self.catalog), 6)
        self.assertEqual(codex_job_capacity_units("unknown-model", "low", self.catalog), 30)

    def test_rejects_a_zero_burst_limit(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(ValueError, "max_burst_jobs must be positive"),
        ):
            self._broker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=1,
                max_burst_jobs=0,
            )

    def test_default_burst_allows_extra_cheap_jobs_past_four_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broker = self._broker(
                Path(temp) / "global-quota.sqlite3",
                max_concurrent_jobs=2,
            )
            decisions = [
                broker.try_acquire(
                    f"luna-{index}",
                    worker_pid=os.getpid(),
                    capacity_units=codex_job_capacity_units(
                        "gpt-5.6-luna",
                        "high",
                        self.catalog,
                    ),
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
            broker = self._broker(
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
            broker = self._broker(
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
                    "insert into leases (job_id, worker_pid, acquired_at, heartbeat_at) "
                    "values (?, ?, ?, ?)",
                    ("legacy-sol", os.getpid(), 1.0, 1.0),
                )
                db.commit()
            finally:
                db.close()

            broker = self._broker(
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

    def test_domain_soft_caps_can_be_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cases = [
                (
                    "primary_full_spark_free",
                    RateLimitSnapshot(
                        used_percent=100.0,
                        resets_at=2_000.0,
                        observed_at=1_000.0,
                        quota_domain="primary",
                    ),
                    RateLimitSnapshot(
                        used_percent=10.0,
                        resets_at=2_000.0,
                        observed_at=1_000.0,
                        quota_domain="spark",
                    ),
                ),
                (
                    "spark_full_primary_free",
                    RateLimitSnapshot(
                        used_percent=10.0,
                        resets_at=2_000.0,
                        observed_at=1_000.0,
                        quota_domain="primary",
                    ),
                    RateLimitSnapshot(
                        used_percent=100.0,
                        resets_at=2_000.0,
                        observed_at=1_000.0,
                        quota_domain="spark",
                    ),
                ),
            ]
            for domain, primary_snapshot, spark_snapshot in cases:
                with self.subTest(domain=domain):
                    case_database = Path(temp) / f"{domain}-global-quota.sqlite3"
                    reader = {
                        "primary": primary_snapshot,
                        "spark": spark_snapshot,
                    }.__getitem__

                    broker = self._broker(
                        case_database,
                        max_concurrent_jobs=2,
                        soft_limit_percent=75.0,
                        spark_soft_limit_percent=75.0,
                        rate_limit_reader=reader,
                        clock=lambda: 1_100.0,
                    )
                    primary = broker.try_acquire(
                        f"{domain}-primary",
                        worker_pid=os.getpid(),
                        model="gpt-5.6-terra",
                    )
                    spark = broker.try_acquire(
                        f"{domain}-spark",
                        worker_pid=os.getpid(),
                        model="gpt-5.3-codex-spark",
                    )

                    self.assertEqual(primary.quota_domain, "primary")
                    self.assertEqual(spark.quota_domain, "spark")
                    if domain == "primary_full_spark_free":
                        self.assertFalse(primary.acquired)
                        self.assertEqual(primary.reason, "rate_limit_soft_cap")
                        self.assertTrue(spark.acquired)
                    else:
                        self.assertFalse(spark.acquired)
                        self.assertEqual(spark.reason, "rate_limit_soft_cap")
                        self.assertTrue(primary.acquired)

    def test_full_primary_pool_still_admits_spark_to_local_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(
                database,
                max_concurrent_jobs=1,
                max_burst_jobs=4,
                spark_max_concurrent_jobs=8,
            )
            self.assertTrue(
                broker.try_acquire(
                    "primary",
                    worker_pid=os.getpid(),
                    model="gpt-5.6-terra",
                ).acquired
            )
            spark_decisions = [
                broker.try_acquire(
                    f"spark-{index}",
                    worker_pid=os.getpid(),
                    model="gpt-5.3-codex-spark",
                )
                for index in range(8)
            ]

            self.assertTrue(all(decision.acquired for decision in spark_decisions))
            self.assertEqual(spark_decisions[-1].active_jobs, 8)
            self.assertEqual(spark_decisions[-1].max_capacity_units, 240)

            blocked = broker.try_acquire(
                "spark-8",
                worker_pid=os.getpid(),
                model="gpt-5.3-codex-spark",
            )
            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
            self.assertEqual(blocked.active_jobs, 8)

    def test_full_spark_pool_still_allows_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(
                database,
                max_concurrent_jobs=1,
                spark_max_concurrent_jobs=8,
            )
            spark_decisions = [
                broker.try_acquire(
                    f"spark-{index}",
                    worker_pid=os.getpid(),
                    model="gpt-5.3-codex-spark",
                )
                for index in range(8)
            ]
            self.assertTrue(all(decision.acquired for decision in spark_decisions))

            primary = broker.try_acquire(
                "primary",
                worker_pid=os.getpid(),
                model="gpt-5.6-terra",
            )
            self.assertTrue(primary.acquired)
            self.assertEqual(primary.quota_domain, "primary")

    def test_ninth_spark_job_is_blocked_by_spark_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(
                database,
                max_concurrent_jobs=2,
                spark_max_concurrent_jobs=8,
            )
            first_spark = [
                broker.try_acquire(
                    f"spark-{index}",
                    worker_pid=os.getpid(),
                    model="gpt-5.3-codex-spark",
                )
                for index in range(8)
            ]
            self.assertTrue(all(decision.acquired for decision in first_spark))

            blocked = broker.try_acquire(
                "spark-8",
                worker_pid=os.getpid(),
                model="gpt-5.3-codex-spark",
            )
            self.assertFalse(blocked.acquired)
            self.assertEqual(blocked.reason, "weighted_capacity_limit")
            self.assertEqual(blocked.active_jobs, 8)
            self.assertEqual(blocked.max_capacity_units, 240)

    def test_primary_burst_count_ignores_spark_leases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(
                database,
                max_concurrent_jobs=1,
                max_burst_jobs=2,
                spark_max_concurrent_jobs=8,
            )
            self.assertTrue(
                broker.try_acquire(
                    "primary-1",
                    worker_pid=os.getpid(),
                    capacity_units=6,
                    model="gpt-5.6-luna",
                ).acquired
            )
            self.assertTrue(
                broker.try_acquire(
                    "primary-2",
                    worker_pid=os.getpid(),
                    capacity_units=6,
                    model="gpt-5.6-luna",
                ).acquired
            )

            burst_blocked = broker.try_acquire(
                "primary-3",
                worker_pid=os.getpid(),
                capacity_units=6,
                model="gpt-5.6-luna",
            )
            self.assertFalse(burst_blocked.acquired)
            self.assertEqual(burst_blocked.reason, "burst_job_limit")

            spark_decisions = [
                broker.try_acquire(
                    f"spark-{index}",
                    worker_pid=os.getpid(),
                    model="gpt-5.3-codex-spark",
                    capacity_units=6,
                )
                for index in range(8)
            ]
            self.assertTrue(all(decision.acquired for decision in spark_decisions))

    def test_existing_lease_domain_switch_does_not_double_count_target_totals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            broker = self._broker(
                database,
                max_concurrent_jobs=1,
                max_burst_jobs=4,
                spark_max_concurrent_jobs=8,
            )
            self.assertTrue(
                broker.try_acquire(
                    "spark-existing",
                    worker_pid=os.getpid(),
                    model="gpt-5.3-codex-spark",
                    capacity_units=6,
                ).acquired
            )
            self.assertTrue(
                broker.try_acquire(
                    "primary-switching",
                    worker_pid=os.getpid(),
                    model="gpt-5.6-terra",
                    capacity_units=6,
                ).acquired
            )

            switched = broker.try_acquire(
                "primary-switching",
                worker_pid=os.getpid(),
                model="gpt-5.3-codex-spark",
                capacity_units=6,
            )
            self.assertTrue(switched.acquired)
            self.assertEqual(switched.quota_domain, "spark")
            self.assertEqual(switched.active_jobs, 2)
            self.assertEqual(switched.active_capacity_units, 12)

    def test_migrates_v1_db_and_records_v2_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "global-quota.sqlite3"
            db = sqlite3.connect(database)
            try:
                db.execute(
                    """
                    create table schema_migrations (
                        component text not null,
                        version integer not null,
                        checksum text not null,
                        applied_at text not null,
                        primary key(component, version)
                    )
                    """
                )
                db.execute(
                    """
                    create table leases (
                        job_id text primary key,
                        worker_pid integer not null,
                        acquired_at real not null,
                        heartbeat_at real not null,
                        capacity_units integer not null default 30
                    )
                    """
                )
                db.execute(
                    "insert into schema_migrations values (?, ?, ?, ?)",
                    (
                        "global_quota_broker",
                        1,
                        "global-quota-broker-v1-20260715",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                db.execute(
                    "insert into leases (job_id, worker_pid, acquired_at, heartbeat_at, capacity_units) "
                    "values (?, ?, ?, ?, 30)",
                    ("legacy", os.getpid(), 1.0, 1.0),
                )
                db.commit()
            finally:
                db.close()

            self._broker(database, max_concurrent_jobs=1, max_burst_jobs=1)
            db_read = sqlite3.connect(database)
            try:
                row = db_read.execute(
                    "select quota_domain from leases where job_id = ?",
                    ("legacy",),
                ).fetchone()
                count_v2 = db_read.execute(
                    "select count(*) from schema_migrations where component = ? and version = ?",
                    ("global_quota_broker", 2),
                ).fetchone()
            finally:
                db_read.close()
            if row is None:
                self.fail("Migration v1 row not found after upgrade to schema version 2")
            if count_v2 is None:
                self.fail("Schema migration row for version 2 was not added")
            self.assertEqual(row[0], "primary")
            self.assertEqual(count_v2[0], 1)
            self._broker(database, max_concurrent_jobs=1, max_burst_jobs=1)
            db_read = sqlite3.connect(database)
            try:
                checksum_row = db_read.execute(
                    "select checksum from schema_migrations where component = ? and version = ?",
                    ("global_quota_broker", 2),
                ).fetchone()
            finally:
                db_read.close()
            if checksum_row is None:
                self.fail("Missing migration checksum entry for schema version 2")
            self.assertEqual(checksum_row[0], "global-quota-broker-v2-20260716")


class CodexRateLimitReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = _catalog(
            CatalogModelMetadata(
                model="gpt-5.3-codex-spark",
                quota_domain="spark",
            )
        )

    def _reader(self, sessions_root: Path) -> CodexRateLimitReader:
        return CodexRateLimitReader(sessions_root, catalog=self.catalog)

    def test_latest_prefers_observed_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions" / "2026" / "07" / "11"
            sessions.mkdir(parents=True)
            newer = sessions / "newer.jsonl"
            older = sessions / "older.jsonl"
            now = time.time()
            reader = self._reader(Path(temp) / "sessions")

            with self.subTest("newer_spark_older_terra"):
                write_jsonl_events(
                    newer,
                    build_turn_context_event(
                        "gpt-5.3-codex-spark", timestamp="2026-07-11T06:00:05Z"
                    ),
                    build_token_count_event(2.0, 2_000.0, timestamp="2026-07-11T06:00:05Z"),
                    build_turn_context_event("gpt-5.6-terra", timestamp="2026-07-11T06:00:10Z"),
                    build_token_count_event(5.0, 2_000.0, timestamp="2026-07-11T06:00:10Z"),
                )
                write_jsonl_events(
                    older,
                    build_turn_context_event(
                        "gpt-5.3-codex-spark", timestamp="2026-07-11T06:00:20Z"
                    ),
                    build_token_count_event(80.0, 2_000.0, timestamp="2026-07-11T06:00:20Z"),
                    build_turn_context_event("gpt-5.6-terra", timestamp="2026-07-11T06:00:25Z"),
                    build_token_count_event(100.0, 2_000.0, timestamp="2026-07-11T06:00:25Z"),
                )
                os.utime(newer, (now, now))
                os.utime(older, (now - 5_000.0, now - 5_000.0))
                self.assertEqual(reader.latest("spark").used_percent, 80.0)
                self.assertEqual(reader.latest("primary").used_percent, 100.0)

            with self.subTest("newer_terra_older_spark"):
                write_jsonl_events(
                    newer,
                    build_turn_context_event(
                        "gpt-5.3-codex-spark", timestamp="2026-07-11T06:01:20Z"
                    ),
                    build_token_count_event(5.0, 2_000.0, timestamp="2026-07-11T06:01:20Z"),
                    build_turn_context_event("gpt-5.6-terra", timestamp="2026-07-11T06:01:21Z"),
                    build_token_count_event(2.0, 2_000.0, timestamp="2026-07-11T06:01:21Z"),
                )
                write_jsonl_events(
                    older,
                    build_turn_context_event(
                        "gpt-5.3-codex-spark", timestamp="2026-07-11T06:01:05Z"
                    ),
                    build_token_count_event(100.0, 2_000.0, timestamp="2026-07-11T06:01:05Z"),
                    build_turn_context_event("gpt-5.6-terra", timestamp="2026-07-11T06:01:15Z"),
                    build_token_count_event(80.0, 2_000.0, timestamp="2026-07-11T06:01:15Z"),
                )
                os.utime(newer, (now, now))
                os.utime(older, (now - 5_000.0, now - 5_000.0))
                self.assertEqual(reader.latest("spark").used_percent, 5.0)
                self.assertEqual(reader.latest("primary").used_percent, 2.0)

    def test_turn_context_sequence_switches_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions" / "2026" / "07" / "12"
            sessions.mkdir(parents=True)
            rollout = sessions / "rollout.jsonl"

            with self.subTest("spark_to_terra"):
                write_jsonl_events(
                    rollout,
                    build_turn_context_event(
                        "gpt-5.3-codex-spark", timestamp="2026-07-12T06:00:00Z"
                    ),
                    build_token_count_event(55.0, 2_000.0, timestamp="2026-07-12T06:00:05Z"),
                    build_turn_context_event("gpt-5.6-terra", timestamp="2026-07-12T06:00:10Z"),
                    build_token_count_event(80.0, 2_000.0, timestamp="2026-07-12T06:00:15Z"),
                )
                reader = self._reader(Path(temp) / "sessions")
                self.assertEqual(reader.latest("spark").used_percent, 55.0)
                self.assertEqual(reader.latest("primary").used_percent, 80.0)

            write_jsonl_events(rollout)
            with self.subTest("terra_to_spark"):
                write_jsonl_events(
                    rollout,
                    build_turn_context_event("gpt-5.6-terra", timestamp="2026-07-12T06:01:00Z"),
                    build_token_count_event(55.0, 2_000.0, timestamp="2026-07-12T06:01:05Z"),
                    build_turn_context_event(
                        "gpt-5.3-codex-spark", timestamp="2026-07-12T06:01:10Z"
                    ),
                    build_token_count_event(35.0, 2_000.0, timestamp="2026-07-12T06:01:15Z"),
                )
                reader = self._reader(Path(temp) / "sessions")
                self.assertEqual(reader.latest("spark").used_percent, 35.0)
                self.assertEqual(reader.latest("primary").used_percent, 55.0)

    def test_turn_context_state_resets_and_ignores_payload_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions" / "2026" / "07" / "13"
            sessions.mkdir(parents=True)
            rollout = sessions / "rollout.jsonl"

            cases = [
                (
                    "missing_model_resets",
                    [
                        build_turn_context_event(
                            "gpt-5.3-codex-spark",
                            timestamp="2026-07-13T06:00:00Z",
                        ),
                        build_turn_context_event(
                            None,
                            timestamp="2026-07-13T06:00:05Z",
                        ),
                        build_token_count_event(
                            45.0,
                            2_000.0,
                            timestamp="2026-07-13T06:00:10Z",
                        ),
                    ],
                    "primary",
                ),
                (
                    "blank_model_resets",
                    [
                        build_turn_context_event(
                            "gpt-5.3-codex-spark",
                            timestamp="2026-07-13T06:01:00Z",
                        ),
                        build_turn_context_event(
                            "",
                            timestamp="2026-07-13T06:01:05Z",
                        ),
                        build_token_count_event(
                            46.0,
                            2_000.0,
                            timestamp="2026-07-13T06:01:10Z",
                        ),
                    ],
                    "primary",
                ),
                (
                    "unknown_model_defaults_primary",
                    [
                        build_turn_context_event(
                            "gpt-5.6-mystery",
                            timestamp="2026-07-13T06:02:00Z",
                        ),
                        build_token_count_event(
                            47.0,
                            2_000.0,
                            timestamp="2026-07-13T06:02:05Z",
                        ),
                    ],
                    "primary",
                ),
                (
                    "unrelated_payload_model_ignored",
                    [
                        build_turn_context_event(
                            "gpt-5.3-codex-spark",
                            timestamp="2026-07-13T06:03:00Z",
                        ),
                        build_token_count_event(
                            48.0,
                            2_000.0,
                            timestamp="2026-07-13T06:03:05Z",
                            payload_model="gpt-5.6-terra",
                        ),
                    ],
                    "spark",
                ),
            ]
            for description, events, expected_domain in cases:
                with self.subTest(description=description):
                    write_jsonl_events(rollout, *events)
                    snapshot = self._reader(Path(temp) / "sessions").latest(expected_domain)
                    self.assertIsNotNone(snapshot)
                    if snapshot is None:
                        self.fail("Missing quota-domain snapshot")
                    self.assertEqual(snapshot.quota_domain, expected_domain)

    def test_tail_without_preceding_context_defaults_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions" / "2026" / "07" / "14"
            sessions.mkdir(parents=True)
            rollout = sessions / "rollout.jsonl"
            write_jsonl_events(
                rollout,
                build_token_count_event(
                    49.0,
                    2_000.0,
                    timestamp="2026-07-14T06:00:00Z",
                ),
            )

            reader = self._reader(Path(temp) / "sessions")
            primary = reader.latest("primary")
            spark = reader.latest("spark")

            self.assertIsNotNone(primary)
            if primary is None:
                self.fail("Missing primary snapshot")
            self.assertEqual(primary.quota_domain, "primary")
            self.assertIsNone(spark)
