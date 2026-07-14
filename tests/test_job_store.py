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
            )
            self.assertEqual(updated.status, "running")
            self.assertEqual(updated.worker_pid, 123)
            self.assertEqual(updated.runner_pid, 456)

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
