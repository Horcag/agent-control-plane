from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.entities.job import JobStore


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
                    '{"schema_version":1,"pid":456,'
                    '"started_key":"test:1","executable":"python"}'
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

    def test_workspace_access_default_is_ide_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = _create_job(store, root, "job-1")
            self.assertEqual(job.workspace_access, "ide_mcp")

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


def _create_job(store: JobStore, root: Path, job_id: str):
    return store.create_job(
        job_id=job_id,
        task_id="task-1",
        route="main",
        workspace_path=root / "workspace",
        expected_branch="review/pr",
        config_path=root / "config.toml",
        run_dir=root / "runs" / job_id,
        prompt_path=root / "runs" / job_id / "prompt.md",
        result_path=root / "tasks" / "task-1" / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="codex-spark",
        agy_model="Gemini 3.5 Flash (High)",
        codex_model="gpt-5",
        codex_reasoning_effort="low",
    )


if __name__ == "__main__":
    unittest.main()
