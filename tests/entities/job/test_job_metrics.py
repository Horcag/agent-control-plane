from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.entities.job import AttemptMetrics, JobStore
from agent_control_plane.entities.job.model.attempt_metrics import (
    create_attempt_metrics_table,
    load_attempt_metrics,
    save_attempt_metrics,
)


class JobMetricsStoreTest(unittest.TestCase):
    def test_attempt_metrics_are_durable_and_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _create_job(store, root)
            log_path = root / "runs" / "job-1" / "attempt-001.log"
            store.start_attempt("job-1", 1, log_path)
            store.finish_attempt("job-1", 1, "completed", result_status="completed", exit_code=0)

            store.record_attempt_metrics(
                "job-1",
                1,
                backend="codex",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                metrics=_metrics(log_path),
            )
            failed_log_path = root / "runs" / "job-1" / "attempt-002.log"
            store.start_attempt("job-1", 2, failed_log_path)
            store.finish_attempt("job-1", 2, "completed", result_status="partial", exit_code=0)
            store.record_attempt_metrics(
                "job-1",
                2,
                backend="codex",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                metrics=_metrics(failed_log_path),
            )

            reopened = JobStore(root / "jobs.sqlite3")
            attempts = reopened.attempt_metrics("job-1")
            attempt = next(row for row in attempts if row["attempt_no"] == 1)
            report = reopened.metrics_report(limit=10, valid_only=True)

            self.assertEqual(attempt["status"], "completed")
            self.assertEqual(attempt["result_status"], "completed")
            self.assertEqual(attempt["thread_id"], "thread-1")
            self.assertEqual(attempt["model"], "gpt-5.6-terra")
            self.assertEqual(attempt["reasoning_effort"], "medium")
            self.assertEqual(
                attempt["tool_counts"],
                {"mcp:agentbridge_idea_8644/read_file": 2},
            )
            self.assertEqual(attempt["cache_creation_input_tokens"], 150)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(report["totals"]["attempt_count"], 2)
            self.assertEqual(report["totals"]["completed_attempt_count"], 2)
            self.assertEqual(report["totals"]["result_completed_attempt_count"], 1)
            self.assertEqual(report["totals"]["partial_attempt_count"], 1)
            self.assertAlmostEqual(report["totals"]["success_rate"], 0.5)
            self.assertAlmostEqual(report["totals"]["cache_hit_ratio"], 0.6)
            self.assertAlmostEqual(report["totals"]["p50_duration_sec"], 20.0)
            self.assertEqual(report["totals"]["cache_creation_input_tokens"], 300)
            self.assertEqual(report["totals"]["uncached_input_tokens"], 800)
            self.assertEqual(report["groups"][0]["model"], "gpt-5.6-terra")
            self.assertEqual(report["groups"][0]["reasoning_effort"], "medium")

    def test_legacy_table_without_cache_creation_column_is_migrated_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _create_job(store, root)
            log_path = root / "runs" / "job-1" / "attempt-001.log"
            store.start_attempt("job-1", 1, log_path)
            store.finish_attempt("job-1", 1, "completed", result_status="completed", exit_code=0)
            store.initialize()

            db = sqlite3.connect(store.database_path)
            try:
                db.execute("alter table attempt_metrics rename to attempt_metrics_new")
                db.execute(
                    """
                    create table attempt_metrics (
                        job_id text not null references jobs(job_id),
                        attempt_no integer not null,
                        backend text not null,
                        model text,
                        reasoning_effort text,
                        duration_sec real not null,
                        thread_id text,
                        event_count integer not null,
                        turn_completed integer not null,
                        usage_available integer not null,
                        input_tokens integer not null,
                        cached_input_tokens integer not null,
                        output_tokens integer not null,
                        reasoning_output_tokens integer not null,
                        tool_calls integer not null,
                        failed_tool_calls integer not null,
                        error_events integer not null,
                        tool_counts_json text not null,
                        estimated_credits real,
                        estimated_api_usd real,
                        rate_card_version text not null,
                        event_log_path text,
                        created_at text not null,
                        primary key(job_id, attempt_no)
                    )
                    """
                )
                db.execute(
                    """
                    insert into attempt_metrics (
                        job_id, attempt_no, backend, model, reasoning_effort,
                        duration_sec, thread_id, event_count, turn_completed, usage_available,
                        input_tokens, cached_input_tokens, output_tokens,
                        reasoning_output_tokens, tool_calls, failed_tool_calls, error_events,
                        tool_counts_json, estimated_credits, estimated_api_usd,
                        rate_card_version, event_log_path, created_at
                    ) values (
                        'job-1', 1, 'codex', 'gpt-5.6-terra', 'medium',
                        20.0, 'thread-1', 7, 1, 1,
                        1000, 600, 200,
                        50, 2, 1, 0,
                        '{}', 0.10375, 0.00415,
                        '2026-07-09', null, '2026-07-20T10:00:00Z'
                    )
                    """
                )
                db.execute("drop table attempt_metrics_new")
                db.execute(
                    "delete from schema_migrations where component = 'job_store' and version = 1"
                )
                columns = {row[1] for row in db.execute("pragma table_info(attempt_metrics)")}
                self.assertNotIn("cache_creation_input_tokens", columns)
                db.commit()
            finally:
                db.close()

            reopened = JobStore(root / "jobs.sqlite3")
            reopened.initialize()
            attempts = reopened.attempt_metrics("job-1")

            db = sqlite3.connect(store.database_path)
            try:
                columns = {row[1] for row in db.execute("pragma table_info(attempt_metrics)")}
            finally:
                db.close()
            self.assertIn("cache_creation_input_tokens", columns)
            self.assertEqual(attempts[0]["cache_creation_input_tokens"], 0)

    def test_pre_existing_database_gets_cache_creation_column_without_rerunning_v1(self) -> None:
        """Reproduces the production KeyError: v1 stays recorded as applied (unlike the
        legacy-table test above, which deletes that record and lets _migrate_schema
        re-run through its own call site). The fix is the unconditional
        create_attempt_metrics_table(db) call in initialize(); removing it must fail
        this test.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            _create_job(store, root)
            log_path = root / "runs" / "job-1" / "attempt-001.log"
            store.start_attempt("job-1", 1, log_path)
            store.finish_attempt("job-1", 1, "completed", result_status="completed", exit_code=0)
            store.initialize()

            db = sqlite3.connect(store.database_path)
            try:
                db.execute("drop table attempt_metrics")
                db.execute(
                    """
                    create table attempt_metrics (
                        job_id text not null references jobs(job_id),
                        attempt_no integer not null,
                        backend text not null,
                        model text,
                        reasoning_effort text,
                        duration_sec real not null,
                        thread_id text,
                        event_count integer not null,
                        turn_completed integer not null,
                        usage_available integer not null,
                        input_tokens integer not null,
                        cached_input_tokens integer not null,
                        output_tokens integer not null,
                        reasoning_output_tokens integer not null,
                        tool_calls integer not null,
                        failed_tool_calls integer not null,
                        error_events integer not null,
                        tool_counts_json text not null,
                        estimated_credits real,
                        estimated_api_usd real,
                        rate_card_version text not null,
                        event_log_path text,
                        created_at text not null,
                        primary key(job_id, attempt_no)
                    )
                    """
                )
                db.execute(
                    """
                    insert into attempt_metrics (
                        job_id, attempt_no, backend, model, reasoning_effort,
                        duration_sec, thread_id, event_count, turn_completed, usage_available,
                        input_tokens, cached_input_tokens, output_tokens,
                        reasoning_output_tokens, tool_calls, failed_tool_calls, error_events,
                        tool_counts_json, estimated_credits, estimated_api_usd,
                        rate_card_version, event_log_path, created_at
                    ) values (
                        'job-1', 1, 'codex', 'gpt-5.6-terra', 'medium',
                        20.0, 'thread-1', 7, 1, 1,
                        1000, 600, 200,
                        50, 2, 1, 0,
                        '{}', 0.10375, 0.00415,
                        '2026-07-09', null, '2026-07-20T10:00:00Z'
                    )
                    """
                )
                columns = {row[1] for row in db.execute("pragma table_info(attempt_metrics)")}
                self.assertNotIn("cache_creation_input_tokens", columns)
                migrated = {
                    row[0]
                    for row in db.execute(
                        "select version from schema_migrations where component = 'job_store'"
                    )
                }
                self.assertIn(1, migrated)
                db.commit()
            finally:
                db.close()

            reopened = JobStore(root / "jobs.sqlite3")
            reopened.initialize()

            db = sqlite3.connect(store.database_path)
            try:
                columns = {row[1] for row in db.execute("pragma table_info(attempt_metrics)")}
            finally:
                db.close()
            self.assertIn("cache_creation_input_tokens", columns)

            attempts = reopened.attempt_metrics("job-1")
            self.assertEqual(attempts[0]["cache_creation_input_tokens"], 0)
            report = reopened.metrics_report(limit=10)
            self.assertEqual(report["totals"]["cache_creation_input_tokens"], 0)

    def test_backend_filter_restricts_loaded_rows(self) -> None:
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute("create table jobs (job_id text primary key, task_id text, route text)")
            db.execute(
                """
                create table attempts (
                    job_id text, attempt_no integer, status text,
                    result_status text, exit_code integer, finished_at text
                )
                """
            )
            create_attempt_metrics_table(db)
            for job_id, backend in (("job-codex", "codex"), ("job-claude", "claude")):
                db.execute(
                    "insert into jobs (job_id, task_id, route) values (?, ?, ?)",
                    (job_id, "task", "main"),
                )
                db.execute(
                    "insert into attempts (job_id, attempt_no, status, result_status, "
                    "exit_code, finished_at) values (?, 1, 'completed', 'completed', 0, 'x')",
                    (job_id,),
                )
                save_attempt_metrics(
                    db,
                    job_id=job_id,
                    attempt_no=1,
                    backend=backend,
                    model="m",
                    reasoning_effort=None,
                    metrics=_metrics(Path(f"{job_id}.log")),
                )

            rows = load_attempt_metrics(db, backend="claude")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["job_id"], "job-claude")
            self.assertEqual(rows[0]["backend"], "claude")

            rows = load_attempt_metrics(db)
            self.assertEqual(len(rows), 2)


def _metrics(log_path: Path, *, cache_creation_input_tokens: int = 150) -> AttemptMetrics:
    return AttemptMetrics(
        duration_sec=20.0,
        thread_id="thread-1",
        event_count=7,
        turn_completed=True,
        usage_available=True,
        input_tokens=1000,
        cached_input_tokens=600,
        output_tokens=200,
        reasoning_output_tokens=50,
        tool_calls=2,
        failed_tool_calls=1,
        error_events=0,
        tool_counts=(("mcp:agentbridge_idea_8644/read_file", 2),),
        estimated_credits=0.10375,
        estimated_api_usd=0.00415,
        rate_card_version="2026-07-09",
        event_log_path=log_path.with_suffix(".events.jsonl"),
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def _create_job(store: JobStore, root: Path) -> None:
    store.create_job(
        job_id="job-1",
        task_id="task-1",
        route="main",
        workspace_path=root / "workspace",
        expected_branch="main",
        config_path=root / "config.toml",
        run_dir=root / "runs" / "job-1",
        prompt_path=root / "runs" / "job-1" / "prompt.md",
        result_path=root / "tasks" / "task-1" / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=True,
        backend="codex",
        codex_model="gpt-5.6-terra",
        codex_reasoning_effort="medium",
    )


if __name__ == "__main__":
    unittest.main()
