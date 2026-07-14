from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
from agent_control_plane.entities.job import JobStore
from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result


class FailureReportingTest(unittest.TestCase):
    def test_missing_result_writes_diagnostic_blocked_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = store.create_job(
                job_id="job-1",
                task_id="task-1",
                route="main",
                workspace_path=root / "workspace",
                expected_branch="review/pr",
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
                read_only=False,
            )
            result = SimpleNamespace(
                status="exited_without_result",
                message="result file does not exist",
                exit_code=0,
            )
            log_path = root / "runs" / "job-1" / "attempt-001.log"

            message = AgentControlPlane._missing_result_message(job, result, log_path)
            AgentControlPlane._write_blocked_result_if_missing(job, message)

            text = job.result_path.read_text(encoding="utf-8")
            state = inspect_result(job.result_path, 0.0)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "blocked")
            self.assertIn("exited_without_result", text)
            self.assertIn("result file does not exist", text)
            self.assertIn("attempt-001.log", text)
            self.assertIn("Verification performed", text)
            self.assertIn("Not verified / remaining risks", text)

    def test_missing_result_reports_agy_quota_before_stale_result_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root / "jobs.sqlite3")
            job = store.create_job(
                job_id="job-quota",
                task_id="task-quota",
                route="main",
                workspace_path=root / "workspace",
                expected_branch="review/pr",
                config_path=root / "config.toml",
                run_dir=root / "runs" / "job-quota",
                prompt_path=root / "runs" / "job-quota" / "prompt.md",
                result_path=root / "tasks" / "task-quota" / "result.md",
                timeout_sec=10,
                idle_timeout_sec=5,
                print_timeout="10s",
                max_restarts=0,
                yolo=False,
                allow_dirty=False,
                read_only=False,
            )
            result = SimpleNamespace(
                status="exited_without_result",
                message="result file is older than job start",
                exit_code=1,
            )
            log_path = root / "runs" / "job-quota" / "attempt-001.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "Error: Individual quota reached. Resets in 17m13s.\n",
                encoding="utf-8",
            )

            message = AgentControlPlane._missing_result_message(job, result, log_path)

            self.assertIn("agy quota exhausted", message)
            self.assertIn("result file is older than job start", message)


if __name__ == "__main__":
    unittest.main()
