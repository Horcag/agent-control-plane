from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from agent_control_plane.entities.job import JobStore, ReviewMetricsStore
from agent_control_plane.entities.plan import PlanStore
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.shared.sqlite_runtime import (
    bootstrap_control_database,
    control_database,
)


def test_control_database_enables_wal_foreign_keys_and_busy_timeout(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    bootstrap_control_database(database)

    with control_database(database) as db:
        journal_mode = db.execute("pragma journal_mode").fetchone()[0]
        foreign_keys = db.execute("pragma foreign_keys").fetchone()[0]
        busy_timeout = db.execute("pragma busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout >= 30_000


def test_ordinary_connection_does_not_force_wal_on_a_legacy_database(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        assert db.execute("pragma journal_mode = delete").fetchone()[0] == "delete"

    with control_database(database) as db:
        assert db.execute("pragma journal_mode").fetchone()[0] == "delete"


def test_bootstrap_waits_for_a_legacy_writer_before_enabling_wal(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("pragma journal_mode = delete")
        db.execute("create table legacy_state (value integer not null)")
        db.execute("insert into legacy_state values (1)")

    writer = sqlite3.connect(database)
    writer.execute("begin immediate")
    writer.execute("update legacy_state set value = 2")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                bootstrap_control_database,
                database,
                busy_timeout_ms=3_000,
            )
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.1)
            writer.commit()
            future.result(timeout=5)
    finally:
        writer.close()

    with control_database(database) as db:
        assert db.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert db.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'schema_migrations'"
        ).fetchone()


def test_control_database_rolls_back_failed_unit_of_work(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    with control_database(database) as db:
        db.execute("create table values_table (value text not null)")

    with pytest.raises(RuntimeError, match="rollback"), control_database(database) as db:
        db.execute("insert into values_table values ('not committed')")
        raise RuntimeError("rollback")

    with control_database(database) as db:
        count = db.execute("select count(*) from values_table").fetchone()[0]
    assert count == 0


def test_all_control_stores_initialize_concurrently_on_one_database(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"

    def initialize_all(_: int) -> None:
        JobStore(database).initialize()
        SlotStore(database).initialize()
        ReviewInboxStore(database).initialize()
        PlanStore(database).initialize()
        ReviewMetricsStore(database).initialize()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(initialize_all, range(16)))

    with control_database(database) as db:
        tables = {
            row[0]
            for row in db.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                insert into plan_events (plan_id, event_type, payload_json, created_at)
                values ('missing-plan', 'invalid', '{}', '2026-07-15T00:00:00+00:00')
                """
            )

    assert {"jobs", "slots", "review_inbox_items", "plans", "review_spans"} <= tables


def test_job_migration_quarantines_orphan_events(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    store = JobStore(database)
    store.initialize()
    with sqlite3.connect(database) as db:
        db.execute("pragma foreign_keys = off")
        db.execute(
            "insert into events (job_id, created_at, level, message) values (?, ?, ?, ?)",
            ("missing-job", "2026-07-15T00:00:00+00:00", "warning", "preserve me"),
        )
        db.execute(
            "delete from schema_migrations where component = 'job_store'"
        )

    store.initialize()

    with control_database(database) as db:
        assert db.execute("pragma foreign_key_check").fetchall() == []
        orphan = db.execute(
            "select job_id, message, reason from orphaned_events"
        ).fetchone()
    assert tuple(orphan) == ("missing-job", "preserve me", "missing_parent_job")


def test_schema_migration_is_applied_once_across_two_processes(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from agent_control_plane.shared.sqlite_runtime import apply_schema_migration

        def migrate(db):
            db.execute("create table if not exists migration_probe (id integer primary key)")
            db.execute("insert into migration_probe values (1)")

        apply_schema_migration(
            Path(sys.argv[1]),
            component="process_probe",
            version=1,
            checksum="process-probe-v1",
            migrate=migrate,
        )
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = [
        subprocess.Popen(  # nosec B603
            [sys.executable, "-c", script, str(database)],
            cwd=Path(__file__).parents[1],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    with control_database(database) as db:
        assert db.execute("select count(*) from migration_probe").fetchone()[0] == 1
        assert db.execute(
            """
            select count(*) from schema_migrations
            where component = 'process_probe' and version = 1
            """
        ).fetchone()[0] == 1
