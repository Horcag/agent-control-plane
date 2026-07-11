from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane, StartOptions
from agent_control_plane.shared.config import ControlConfig, ControlDefaults, RouteConfig


class WatchJobTest(unittest.TestCase):
    def test_watch_returns_finished_job_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            _create_job(control, root, "job-1")
            control.finish_job("job-1", "completed", "done")

            summary = control.watch_job("job-1", poll_interval_sec=0, timeout_sec=10)

            self.assertTrue(summary["terminal"])
            self.assertFalse(summary["timed_out"])
            self.assertEqual(summary["status"], "completed")

    def test_watch_times_out_for_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            _create_job(control, root, "job-2")
            control.store.update_job("job-2", status="running")

            summary = control.watch_job("job-2", poll_interval_sec=0, timeout_sec=0)

            self.assertFalse(summary["terminal"])
            self.assertTrue(summary["timed_out"])
            self.assertEqual(summary["status"], "running")
            self.assertNotIn("dirty_status", summary)
            self.assertNotIn("log_tail", summary)
            self.assertNotIn("latest_attempt_metrics", summary)

    def test_watch_returns_bounded_log_delta_from_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-delta")
            log_path = job.run_dir / "attempt-001.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("0123456789", encoding="utf-8")
            control.store.update_job(job.job_id, status="running", log_path=log_path)

            summary = control.watch_job(
                job.job_id,
                poll_interval_sec=0,
                timeout_sec=0,
                log_cursor=2,
                log_byte_limit=4,
            )

            self.assertEqual(summary["log_delta"], "2345")
            self.assertEqual(summary["next_log_cursor"], 6)
            self.assertTrue(summary["log_delta_truncated"])
            self.assertNotIn("dirty_status", summary)

    def test_summary_marks_dead_worker_as_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-dead-worker")
            log_path = job.run_dir / "attempt-001.log"
            control.store.start_attempt(job.job_id, 1, log_path)
            control.store.update_job(job.job_id, status="running", worker_pid=123456)

            with patch(
                "agent_control_plane.app.runtime.orchestrator._process_is_alive", return_value=False
            ):
                summary = control.summary_job(job.job_id)

            finished = control.store.get_job(job.job_id)
            events = control.store.recent_events(job.job_id)
            db = sqlite3.connect(control.config.database_path)
            try:
                attempt = db.execute(
                    "select status, finished_at, message from attempts where job_id = ?",
                    (job.job_id,),
                ).fetchone()
            finally:
                db.close()

            self.assertTrue(summary["terminal"])
            self.assertEqual(summary["status"], "worker_error")
            self.assertEqual(finished.status, "worker_error")
            self.assertIn("no longer alive", finished.last_error or "")
            self.assertTrue(any("no longer alive" in event[2] for event in events))
            self.assertIsNotNone(attempt)
            self.assertEqual(attempt[0], "worker_lost")
            self.assertIsNotNone(attempt[1])
            self.assertIn("no longer alive", attempt[2])

    def test_summary_ignores_result_file_older_than_job_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-3")
            job.result_path.parent.mkdir(parents=True)
            job.result_path.write_text("Status: blocked\n", encoding="utf-8")
            os.utime(job.result_path, (1, 1))
            control.store.update_job(
                "job-3",
                status="running",
                started_at=datetime.fromtimestamp(100, UTC).isoformat(timespec="seconds"),
            )

            summary = control.summary_job("job-3")

            self.assertFalse(summary["result_done"])
            self.assertIsNone(summary["result_status"])

    def test_archive_jobs_dry_run_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-archive")
            job.run_dir.mkdir(parents=True)
            job.prompt_path.write_text("prompt", encoding="utf-8")
            control.finish_job(job.job_id, "completed", "done")
            old_finished_at = datetime.fromtimestamp(1, UTC).isoformat(timespec="seconds")
            control.store.update_job(job.job_id, finished_at=old_finished_at)

            dry_run = control.archive_jobs(older_than_days=1, limit=10, apply=False)
            applied = control.archive_jobs(older_than_days=1, limit=10, apply=True)
            archived = control.store.get_job(job.job_id)

            self.assertEqual(dry_run[0]["action"], "would_archive")
            self.assertEqual(applied[0]["action"], "archived")
            expected_archive_dir = root / "runs" / "_archive" / "1970" / "01" / "01" / job.job_id
            self.assertFalse(job.run_dir.exists())
            self.assertTrue(expected_archive_dir.exists())
            self.assertEqual(archived.run_dir, expected_archive_dir)
            self.assertIsNotNone(archived.archived_at)
            self.assertTrue(archived.prompt_path.exists())

    def test_archive_jobs_refuses_run_dir_outside_configured_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-foreign")
            foreign_run_dir = root / "legacy-runs" / job.job_id
            foreign_run_dir.mkdir(parents=True)
            foreign_prompt_path = foreign_run_dir / "prompt.md"
            foreign_prompt_path.write_text("prompt", encoding="utf-8")
            control.store.update_job(
                job.job_id,
                run_dir=foreign_run_dir,
                prompt_path=foreign_prompt_path,
            )
            control.finish_job(job.job_id, "completed", "done")
            old_finished_at = datetime.fromtimestamp(1, UTC).isoformat(timespec="seconds")
            control.store.update_job(job.job_id, finished_at=old_finished_at)

            decisions = control.archive_jobs(older_than_days=1, limit=10, apply=True)
            current = control.store.get_job(job.job_id)

            self.assertEqual(decisions[0]["action"], "blocked")
            self.assertIn("outside configured runs root", decisions[0]["reason"])
            self.assertTrue(foreign_run_dir.exists())
            self.assertEqual(current.run_dir, foreign_run_dir)
            self.assertIsNone(current.archived_at)

    def test_start_job_uses_date_run_dir_without_implicit_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, auto_archive_days=1))
            _brief(control.config.coordination_root, "task-new")
            old_job = _create_job(control, root, "job-old")
            old_job.run_dir.mkdir(parents=True)
            old_job.prompt_path.write_text("prompt", encoding="utf-8")
            control.finish_job(old_job.job_id, "completed", "done")
            old_finished_at = datetime.fromtimestamp(1, UTC).isoformat(timespec="seconds")
            control.store.update_job(old_job.job_id, finished_at=old_finished_at)
            now = datetime(2026, 6, 29, 12, 0, tzinfo=UTC).timestamp()

            with (
                patch("agent_control_plane.app.runtime.orchestrator.time.time", return_value=now),
                patch.object(control, "_launch_worker", return_value=777),
            ):
                job = control.start_job(
                    StartOptions(
                        task_id="task-new",
                        route="main",
                        workspace_path=workspace,
                        expected_branch="main",
                    )
                )

            old_job_after_start = control.store.get_job(old_job.job_id)
            expected_run_dir = root / "runs" / "2026" / "06" / "29" / job.job_id
            events = control.store.recent_events(job.job_id)

            self.assertEqual(job.run_dir, expected_run_dir)
            self.assertTrue(expected_run_dir.exists())
            self.assertEqual(old_job_after_start.run_dir, old_job.run_dir)
            self.assertTrue(old_job.run_dir.exists())
            self.assertIsNone(old_job_after_start.archived_at)
            self.assertFalse(any("Auto-archived" in event[2] for event in events))


def _git_repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], path)
    _run(["git", "checkout", "-b", branch], path)
    return path


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


def _brief(coordination_root: Path, task_id: str) -> None:
    task_dir = coordination_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (coordination_root / "agent-protocol.md").write_text("# Protocol\n", encoding="utf-8")
    (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
    (task_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")


def _create_job(control: AgentControlPlane, root: Path, job_id: str):
    return control.store.create_job(
        job_id=job_id,
        task_id="task-1",
        route="main",
        workspace_path=root / "workspace",
        expected_branch="main",
        config_path=root / "workspaces.toml",
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
    )


def _config(root: Path, *, auto_archive_days: int | None = None) -> ControlConfig:
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=root / "repo",
        slot_root=root / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=ControlDefaults(
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=0,
            yolo=False,
            allow_dirty=False,
            prepare_slots=False,
            guardrail_poll_sec=2.0,
            forbidden_status_globs=("uv.lock", ".venv/**"),
            runs_layout="date",
            auto_archive_days=auto_archive_days,
            auto_archive_limit=200,
        ),
        routes=MappingProxyType(
            {
                "main": RouteConfig(
                    name="main",
                    path=root / "repo",
                    required_branch="main",
                    worktree_root=root / "worktrees",
                    worktree_base=root / "repo",
                    source_roots=(Path("backend"), Path("frontend/src")),
                    test_roots=(Path("backend/tests"),),
                    exclude_dirs=(),
                )
            }
        ),
        slots=MappingProxyType({}),
        slot_prepare=(),
    )


if __name__ == "__main__":
    unittest.main()
