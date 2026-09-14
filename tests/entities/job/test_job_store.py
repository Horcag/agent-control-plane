from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.entities.job import AttemptMetrics, JobStore, ReviewMetricsStore
from agent_control_plane.shared.codex_session_usage import TokenUsage


class JobStoreTest(unittest.TestCase):
    def test_job_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = _create_job(store, root, "job-1")

            self.assertEqual(job.job_id, "job-1")
            self.assertEqual(job.status, "created")
            self.assertEqual(job.backend, "codex")
            self.assertEqual(job.agy_model, "Gemini 3.5 Flash (High)")
            self.assertEqual(job.codex_model, "gpt-5")
            self.assertEqual(job.codex_reasoning_effort, "low")

            updated = store.update_job(
                "job-1",
                status="running",
                worker_pid=123,
                runner_pid=456,
                runner_process_identity=(
                    '{"schema_version":1,"pid":456,"started_key":"test:1","executable":"python"}'
                ),
            )
            self.assertEqual(updated.status, "running")
            self.assertEqual(updated.worker_pid, 123)
            self.assertEqual(updated.runner_pid, 456)
            self.assertIsNotNone(updated.runner_process_identity)

            store.add_event("job-1", "info", "started")
            self.assertEqual(store.recent_events("job-1")[-1][2], "started")

    def test_create_job_rejects_duplicate_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _create_job(store, root, "job-1")

            with self.assertRaisesRegex(ValueError, "Task ID already exists"):
                _create_job(store, root, "job-2")

    def test_cancel_flag_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _create_job(store, root, "job-2")

            store.request_cancel("job-2")

            self.assertTrue(store.cancel_requested("job-2"))

    def test_request_cancel_does_not_resurrect_a_finished_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _create_job(store, root, "job-done")

            # Worker finished and the job finalized before the racing cancel lands.
            store.mark_finished("job-done", "completed")
            store.mark_finalization_completed("job-done")

            cancelled = store.request_cancel("job-done")

            # The cancel must be a no-op: clobbering the terminal status back to
            # cancel_requested would exclude the job from reconciliation and
            # strand its owning plan in "cancelling" forever.
            self.assertEqual(cancelled.status, "completed")
            self.assertFalse(cancelled.cancel_requested)
            self.assertIsNotNone(cancelled.finished_at)
            self.assertNotIn(
                "job-done",
                {job.job_id for job in store.reconciliation_candidates()},
            )

    def test_routing_decision_readback_is_deterministic_after_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            store = JobStore(database)
            _create_job(store, root, "job-routing")
            payload = _routing_payload()

            store.record_routing_decision("job-routing", payload)
            restarted = JobStore(database)

            self.assertEqual(restarted.routing_decision("job-routing"), payload)

    def test_routing_history_returns_one_valid_reviewed_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            store = JobStore(database)
            _record_attempt(store, root, "job-history")
            self.assertIsNone(store.routing_history(limit=1)[0]["root_outcome"])

            reviews = ReviewMetricsStore(database)
            span_id = reviews.start_span(
                span_id="review-1",
                name="Root review",
                session_path=root / "review.jsonl",
                usage=TokenUsage(0, 0, 0, 0),
            )
            reviews.attach_job(
                span_id,
                job_id="job-history",
                outcome="accepted",
                root_verified=True,
                defects_found=1,
            )

            history = store.routing_history(limit=1)
            row = history[0]
            self.assertEqual(
                (
                    row["model"],
                    row["reasoning_effort"],
                    row["metrics_valid"],
                    row["root_outcome"],
                    row["defects_found"],
                ),
                ("gpt-5", "low", True, "accepted", 1),
            )
            self.assertEqual(
                (
                    row["route"],
                    row["policy_name"],
                    row["task_class"],
                    row["selection_source"],
                    row["catalog_version"],
                ),
                ("main", "code-change", "implementation", "configured_fallback", "catalog-v1"),
            )

    def test_malformed_routing_payload_is_non_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _record_attempt(store, root, "job-malformed")
            store.add_event("job-malformed", "routing_decision", "not-json")

            self.assertIsNone(store.routing_decision("job-malformed"))
            history = store.routing_history()
            self.assertIsNone(history[0]["policy_name"])

    def test_routing_history_rejects_invalid_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp) / "jobs.sqlite3")

            with self.assertRaises(ValueError):
                store.routing_history(limit=0)

    def test_workspace_access_default_is_ide_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = _create_job(store, root, "job-1")
            self.assertEqual(job.workspace_access, "ide_mcp")

    def test_create_job_finalization_status_defaults_not_started_on_fresh_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = _create_job(store, root, "job-1")
            self.assertEqual(job.finalization_status, "not_started")

    def test_workspace_access_explicit_native(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = store.create_job(
                job_id="job-native",
                task_id="task-native",
                route="main",
                workspace_path=root / "workspace",
                expected_branch="review/pr",
                config_path=root / "config.toml",
                run_dir=root / "runs" / "job-native",
                prompt_path=root / "runs" / "job-native" / "prompt.md",
                result_path=root / "tasks" / "task-native" / "result.md",
                timeout_sec=10,
                idle_timeout_sec=5,
                print_timeout="10s",
                max_restarts=0,
                yolo=False,
                allow_dirty=False,
                read_only=False,
                workspace_access="native",
            )
            self.assertEqual(job.workspace_access, "native")

            # Fetch from DB and check again
            fetched = store.get_job("job-native")
            self.assertEqual(fetched.workspace_access, "native")

    def test_runner_identity_v2_migrates_a_database_already_at_v1(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            store = JobStore(database)
            store.initialize()
            db = sqlite3.connect(database)
            try:
                db.execute("alter table jobs drop column runner_process_identity")
                db.execute(
                    "delete from schema_migrations where component = 'job_store' and version = 2"
                )
                db.commit()
            finally:
                db.close()

            store.initialize()

            db = sqlite3.connect(database)
            try:
                columns = {row[1] for row in db.execute("pragma table_info(jobs)")}
                migration = db.execute(
                    "select checksum from schema_migrations "
                    "where component = 'job_store' and version = 2"
                ).fetchone()
            finally:
                db.close()
            self.assertIn("runner_process_identity", columns)
            self.assertEqual(
                migration,
                ("job-store-runner-process-identity-v2-20260715",),
            )

    def test_premium_override_reason_v3_migrates_older_jobs_database(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            store = JobStore(database)
            store.initialize()
            db = sqlite3.connect(database)
            try:
                db.execute("alter table jobs drop column codex_premium_override_reason")
                db.execute(
                    "delete from schema_migrations where component = 'job_store' and version = 3"
                )
                db.commit()
            finally:
                db.close()

            store.initialize()

            db = sqlite3.connect(database)
            try:
                columns = {row[1] for row in db.execute("pragma table_info(jobs)")}
                migration = db.execute(
                    "select checksum from schema_migrations "
                    "where component = 'job_store' and version = 3"
                ).fetchone()
            finally:
                db.close()
            self.assertIn("codex_premium_override_reason", columns)
            self.assertEqual(
                migration,
                ("job-store-premium-override-reason-v3-20260718",),
            )

    def test_controller_contract_v4_migrates_v3_rows_idempotently(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            store = JobStore(database)
            _create_job(store, Path(temp), "job-v3")
            db = sqlite3.connect(database)
            try:
                db.execute("alter table jobs drop column expected_result_status")
                db.execute("alter table jobs drop column controller_gate_mode")
                db.execute(
                    "delete from schema_migrations where component = 'job_store' and version = 4"
                )
                db.commit()
            finally:
                db.close()

            store.initialize()
            store.initialize()

            db = sqlite3.connect(database)
            try:
                migrations = dict(
                    db.execute(
                        "select version, checksum from schema_migrations "
                        "where component = 'job_store' and version in (3, 4)"
                    )
                )
            finally:
                db.close()
            self.assertEqual(migrations[3], "job-store-premium-override-reason-v3-20260718")
            self.assertEqual(migrations[4], "job-store-controller-contract-v4-20260719")
            job = store.get_job("job-v3")
            self.assertEqual(
                (job.expected_result_status, job.controller_gate_mode), ("completed", "full")
            )

    def test_controller_contract_is_durable_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            job = JobStore(database).create_job(
                **_job_kwargs(root, "job-contract"),
                expected_result_status="partial",
                controller_gate_mode="focused",
            )

            restarted = JobStore(database).get_job(job.job_id)
            self.assertEqual(
                (restarted.expected_result_status, restarted.controller_gate_mode),
                ("partial", "focused"),
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                JobStore(database).update_job(job.job_id, expected_result_status="completed")

    def test_launch_provenance_v6_is_durable_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            job = JobStore(database).create_job(
                **_job_kwargs(root, "job-provenance"),
                launch_base_sha="a" * 40,
                brief_sha256="b" * 64,
                effective_scope_json='["src/a.py","tests/a.py"]',
                retry_override_reason="approved retry",
            )

            restarted = JobStore(database).get_job(job.job_id)
            self.assertEqual(
                (
                    restarted.launch_base_sha,
                    restarted.brief_sha256,
                    restarted.effective_scope_json,
                    restarted.retry_override_reason,
                ),
                ("a" * 40, "b" * 64, '["src/a.py","tests/a.py"]', "approved retry"),
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                JobStore(database).update_job(job.job_id, launch_base_sha="c" * 40)

    def test_causal_outcomes_v5_migrate_v4_rows_idempotently(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            store = JobStore(database)
            _create_job(store, root, "job-v4")
            fields = [
                "runner_failure",
                "workspace_disposition",
                "checkpoint_disposition",
                "root_acceptance",
                "launch_base_sha",
                "brief_sha256",
                "effective_scope_json",
                "retry_override_reason",
            ]
            db = sqlite3.connect(database)
            ledger = dict(
                db.execute(
                    "select version, checksum from schema_migrations "
                    "where component = 'job_store' and version in (3, 4)"
                )
            )
            for column in fields:
                db.execute(f"alter table jobs drop column {column}")
            db.execute(
                "delete from schema_migrations where component = 'job_store' and version = 5"
            )
            db.execute(
                "delete from schema_migrations where component = 'job_store' and version = 6"
            )
            db.commit()
            db.close()

            store.initialize()
            store.initialize()

            restarted = JobStore(database).get_job("job-v4")
            self.assertEqual(
                tuple(getattr(restarted, field) for field in fields),
                (None, "unknown", "none", "pending", None, None, "[]", None),
            )
            db = sqlite3.connect(database)
            migrations = dict(
                db.execute(
                    "select version, checksum from schema_migrations "
                    "where component = 'job_store' and version in (3, 4, 5, 6)"
                )
            )
            db.close()
            self.assertEqual((migrations[3], migrations[4]), (ledger[3], ledger[4]))
            self.assertEqual(migrations[5], "job-store-causal-outcomes-v5-20260719")
            self.assertEqual(migrations[6], "job-store-launch-provenance-v6-20260719")

    def test_causal_outcomes_are_durable_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            store = JobStore(database)
            job = _create_job(store, root, "job-causal")

            for runner_failure in (
                "tool_call_budget",
                "terminal_scope_violation",
                "tool_timeout",
                "controller_transport_failure",
            ):
                store.set_runner_failure(job.job_id, runner_failure)
                self.assertEqual(store.get_job(job.job_id).runner_failure, runner_failure)
            store.set_workspace_disposition(job.job_id, "dirty_after_failure")
            store.set_checkpoint_disposition(job.job_id, "salvage")
            store.set_root_acceptance(job.job_id, "rejected")

            restarted = JobStore(database).get_job(job.job_id)
            self.assertEqual(
                tuple(
                    getattr(restarted, field)
                    for field in (
                        "runner_failure",
                        "workspace_disposition",
                        "checkpoint_disposition",
                        "root_acceptance",
                    )
                ),
                ("controller_transport_failure", "dirty_after_failure", "salvage", "rejected"),
            )
            for invalid in ("", " ", "tool-timeout", "a" * 65):
                with self.assertRaisesRegex(ValueError, "Invalid runner failure"):
                    store.set_runner_failure(job.job_id, invalid)

    def test_causal_setter_migrates_pre_v5_database_before_writing(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            job = _create_job(JobStore(database), root, "job-pre-v5")
            db = sqlite3.connect(database)
            db.execute("alter table jobs drop column runner_failure")
            db.execute(
                "delete from schema_migrations where component = 'job_store' and version = 5"
            )
            db.commit()
            db.close()

            restarted = JobStore(database).set_runner_failure(job.job_id, "tool_timeout")
            self.assertEqual(restarted.runner_failure, "tool_timeout")

    def test_slot_generation_v8_migrates_populated_pre_change_database(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "jobs.sqlite3"
            store = JobStore(database)
            store.initialize()

            # Create an existing job before migration v8 was introduced
            _create_job(store, root, "job-pre-v8")
            store.update_job("job-pre-v8", slot_name="slot-alpha")

            # Simulate a database populated up to migration v7 without slot_generation column
            db = sqlite3.connect(database)
            try:
                db.execute("alter table jobs drop column slot_generation")
                db.execute(
                    "delete from schema_migrations where component = 'job_store' and version = 8"
                )
                db.commit()
            finally:
                db.close()

            # Prove pre-migration v8 database is populated and migration ledger has 1..7 applied
            db = sqlite3.connect(database)
            try:
                cols_pre = {row[1] for row in db.execute("pragma table_info(jobs)").fetchall()}
                self.assertNotIn("slot_generation", cols_pre)
                versions = {
                    row[0]
                    for row in db.execute(
                        "select version from schema_migrations where component = 'job_store'"
                    ).fetchall()
                }
                self.assertEqual(versions, {1, 2, 3, 4, 5, 6, 7})
            finally:
                db.close()

            # Initialize: triggers v8 migration (v1 is skipped because version 1 is in ledger)
            store.initialize()

            # Prove migration v8 was recorded and column was added
            db = sqlite3.connect(database)
            try:
                migration = db.execute(
                    "select checksum from schema_migrations where component = 'job_store' and version = 8"
                ).fetchone()
                self.assertEqual(migration, ("job-store-slot-generation-v8-20260914",))
                cols_post = {row[1] for row in db.execute("pragma table_info(jobs)").fetchall()}
                self.assertIn("slot_generation", cols_post)
            finally:
                db.close()

            # Prove READ still works for existing populated pre-change record
            read_job = store.get_job("job-pre-v8")
            self.assertEqual(read_job.job_id, "job-pre-v8")
            self.assertEqual(read_job.slot_name, "slot-alpha")
            self.assertIsNone(read_job.slot_generation)

            # Prove UPDATE still works (including setting slot_generation)
            updated_job = store.update_job("job-pre-v8", slot_generation=42)
            self.assertEqual(updated_job.slot_generation, 42)
            self.assertEqual(store.get_job("job-pre-v8").slot_generation, 42)

            # Prove CREATE still works with new schema
            kwargs = _job_kwargs(root, "job-post-v8")
            kwargs["task_id"] = "task-2"
            new_job = store.create_job(
                **kwargs,
                slot_name="slot-beta",
                slot_generation=99,
            )
            self.assertEqual(new_job.job_id, "job-post-v8")
            self.assertEqual(new_job.slot_generation, 99)
            self.assertEqual(store.get_job("job-post-v8").slot_generation, 99)

    def test_old_jobs_table_migration(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "jobs.sqlite3"

            # Manually create table without workspace_access column
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                create table jobs (
                    job_id                 text primary key,
                    task_id                text    not null,
                    route                  text    not null,
                    workspace_path         text    not null,
                    expected_branch        text    not null,
                    status                 text    not null,
                    config_path            text    not null,
                    run_dir                text    not null,
                    prompt_path            text    not null,
                    result_path            text    not null,
                    log_path               text,
                    worker_pid             integer,
                    runner_pid             integer,
                    agy_pid                integer,
                    backend                text    not null default 'agy',
                    agy_model              text,
                    codex_model            text,
                    codex_reasoning_effort text,
                    codex_quality_tier     text,
                    codex_tool_call_budget integer,
                    archived_at            text,
                    created_at             text    not null,
                    updated_at             text    not null,
                    started_at             text,
                    finished_at            text,
                    timeout_sec            integer not null,
                    idle_timeout_sec       integer not null,
                    print_timeout          text    not null,
                    max_restarts           integer not null,
                    yolo                   integer not null,
                    allow_dirty            integer not null,
                    read_only              integer not null default 0,
                    slot_name              text,
                    last_error             text,
                    cancel_requested       integer not null default 0
                )
                """
            )
            conn.execute(
                """
                insert into jobs (job_id, task_id, route, workspace_path, expected_branch, status,
                                  config_path, run_dir, prompt_path, result_path,
                                  created_at, updated_at, timeout_sec, idle_timeout_sec,
                                  print_timeout, max_restarts, yolo, allow_dirty)
                values ('old-job', 'old-task', 'main', 'wp', 'branch', 'created',
                        'cp', 'rd', 'pp', 'rp', 'now', 'now', 10, 5, '10s', 0, 0, 0)
                """
            )
            conn.commit()
            conn.close()

            # Initialize JobStore (should trigger migration/add workspace_access column)
            store = JobStore(db_path)
            job = store.get_job("old-job")
            self.assertEqual(job.workspace_access, "ide_mcp")

    def test_migrated_schema_preserves_completed_finalization_status_for_old_rows(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "jobs.sqlite3"

            # Manually create the table shape from before finalization_status existed
            # and seed a row the way a job that predates the column would look.
            conn = sqlite3.connect(db_path)
            conn.execute(_OLD_JOBS_TABLE_SQL)
            conn.execute(_OLD_JOB_INSERT_SQL, ("old-job", "old-task"))
            conn.commit()
            conn.close()

            # initialize() runs the alter table that backfills finalization_status
            # for rows that predate the column with the historically correct 'completed'.
            store = JobStore(db_path)
            old_job = store.get_job("old-job")
            self.assertEqual(old_job.finalization_status, "completed")

    def test_create_job_finalization_status_defaults_not_started_on_migrated_schema(
        self,
    ) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "jobs.sqlite3"

            conn = sqlite3.connect(db_path)
            conn.execute(_OLD_JOBS_TABLE_SQL)
            conn.execute(_OLD_JOB_INSERT_SQL, ("old-job", "old-task"))
            conn.commit()
            conn.close()

            store = JobStore(db_path)
            store.initialize()

            # A job created after the migration must not inherit the migrated
            # column's backfill default; it must start its own lifecycle.
            new_job = _create_job(store, root, "job-after-migration")
            self.assertEqual(new_job.finalization_status, "not_started")

            # The pre-existing row must be unaffected by the new job's creation.
            self.assertEqual(store.get_job("old-job").finalization_status, "completed")


_OLD_JOBS_TABLE_SQL = """
    create table jobs (
        job_id                 text primary key,
        task_id                text    not null,
        route                  text    not null,
        workspace_path         text    not null,
        expected_branch        text    not null,
        status                 text    not null,
        config_path            text    not null,
        run_dir                text    not null,
        prompt_path            text    not null,
        result_path            text    not null,
        log_path               text,
        worker_pid             integer,
        runner_pid             integer,
        agy_pid                integer,
        backend                text    not null default 'agy',
        agy_model              text,
        codex_model            text,
        codex_reasoning_effort text,
        codex_quality_tier     text,
        codex_tool_call_budget integer,
        archived_at            text,
        created_at             text    not null,
        updated_at             text    not null,
        started_at             text,
        finished_at            text,
        timeout_sec            integer not null,
        idle_timeout_sec       integer not null,
        print_timeout          text    not null,
        max_restarts           integer not null,
        yolo                   integer not null,
        allow_dirty            integer not null,
        read_only              integer not null default 0,
        slot_name              text,
        last_error             text,
        cancel_requested       integer not null default 0
    )
"""

_OLD_JOB_INSERT_SQL = """
    insert into jobs (job_id, task_id, route, workspace_path, expected_branch, status,
                      config_path, run_dir, prompt_path, result_path,
                      created_at, updated_at, timeout_sec, idle_timeout_sec,
                      print_timeout, max_restarts, yolo, allow_dirty)
    values (?, ?, 'main', 'wp', 'branch', 'created',
            'cp', 'rd', 'pp', 'rp', 'now', 'now', 10, 5, '10s', 0, 0, 0)
"""


def _create_job(store: JobStore, root: Path, job_id: str):
    return store.create_job(**_job_kwargs(root, job_id))


def _job_kwargs(root: Path, job_id: str) -> dict[str, object]:
    return {
        "job_id": job_id,
        "task_id": "task-1",
        "route": "main",
        "workspace_path": root / "workspace",
        "expected_branch": "review/pr",
        "config_path": root / "config.toml",
        "run_dir": root / "runs" / job_id,
        "prompt_path": root / "runs" / job_id / "prompt.md",
        "result_path": root / "tasks" / "task-1" / "result.md",
        "timeout_sec": 10,
        "idle_timeout_sec": 5,
        "print_timeout": "10s",
        "max_restarts": 0,
        "yolo": False,
        "allow_dirty": False,
        "read_only": False,
        "backend": "codex-spark",
        "agy_model": "Gemini 3.5 Flash (High)",
        "codex_model": "gpt-5",
        "codex_reasoning_effort": "low",
    }


def _record_attempt(store: JobStore, root: Path, job_id: str) -> None:
    _create_job(store, root, job_id)
    store.record_routing_decision(job_id, _routing_payload())
    store.start_attempt(job_id, 1, root / f"{job_id}.attempt.log")
    store.finish_attempt(job_id, 1, "completed", result_status="completed", exit_code=0)
    store.record_attempt_metrics(
        job_id,
        1,
        backend="codex",
        model="gpt-5",
        reasoning_effort="low",
        metrics=_routing_metrics(),
    )


def _routing_payload() -> dict[str, object]:
    return {
        "event": "routing_decision",
        "requested_policy": "code-change",
        "task_class": "implementation",
        "selection_source": "configured_fallback",
        "route": "main",
        "catalog": {"source": "models_cache.json", "version": "catalog-v1"},
    }


def _routing_metrics() -> AttemptMetrics:
    return AttemptMetrics(
        duration_sec=2.0,
        thread_id=None,
        event_count=1,
        turn_completed=True,
        usage_available=True,
        input_tokens=100,
        cached_input_tokens=10,
        output_tokens=20,
        reasoning_output_tokens=0,
        tool_calls=1,
        failed_tool_calls=0,
        error_events=0,
        tool_counts=(),
        estimated_credits=None,
        estimated_api_usd=None,
        rate_card_version="test-card",
        event_log_path=None,
    )


if __name__ == "__main__":
    unittest.main()
