from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.entities.job import AttemptMetrics, JobStore


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
            self.assertEqual(len(attempts), 2)
            self.assertEqual(report["totals"]["attempt_count"], 2)
            self.assertEqual(report["totals"]["completed_attempt_count"], 2)
            self.assertEqual(report["totals"]["result_completed_attempt_count"], 1)
            self.assertEqual(report["totals"]["partial_attempt_count"], 1)
            self.assertAlmostEqual(report["totals"]["success_rate"], 0.5)
            self.assertAlmostEqual(report["totals"]["cache_hit_ratio"], 0.6)
            self.assertAlmostEqual(report["totals"]["p50_duration_sec"], 20.0)
            self.assertEqual(report["groups"][0]["model"], "gpt-5.6-terra")
            self.assertEqual(report["groups"][0]["reasoning_effort"], "medium")


def _metrics(log_path: Path) -> AttemptMetrics:
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
