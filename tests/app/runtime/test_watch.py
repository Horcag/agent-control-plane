from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock, patch

from agent_control_plane.app.runtime.cli import _watch_job_live
from agent_control_plane.app.runtime.orchestrator import AgentControlPlane, StartOptions
from agent_control_plane.entities.job import TERMINAL_STATUSES
from agent_control_plane.entities.plan import PlanTaskDefinition
from agent_control_plane.features.job_watch import (
    FINALIZATION_SLACK_SEC,
    EmptySelectionError,
)
from agent_control_plane.shared.config import (
    CodexModelCatalogConfig,
    CodexQuotaDomainConfig,
    ControlConfig,
    ControlDefaults,
    RouteConfig,
)


class WatchJobTest(unittest.TestCase):
    def test_live_watch_waits_for_finalization_after_terminal_status(self) -> None:
        control = Mock()
        control.summary_job.side_effect = [
            {
                "status": "completed",
                "terminal": True,
                "settled": False,
                "finalization_status": "pending",
                "log_tail": "",
            },
            {
                "status": "completed",
                "terminal": True,
                "settled": True,
                "on_contract": True,
                "finalization_status": "completed",
                "log_tail": "",
            },
        ]

        with patch("agent_control_plane.app.runtime.cli.time.sleep"):
            summary = _watch_job_live(
                control,
                "job-finalizing",
                poll_interval_sec=0.01,
                timeout_sec=1,
                log_lines=20,
            )

        self.assertTrue(summary["settled"])
        self.assertEqual(control.summary_job.call_count, 2)

    def test_watch_plan_returns_only_new_job_state_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            control.create_plan(
                plan_id="transfer",
                title="Transfer",
                tasks=(PlanTaskDefinition("schema", "Schema"),),
            )
            job = _create_job(control, root, "job-plan")
            control.bind_plan_job("transfer", "schema", job.job_id)
            cursor = control.plan_snapshot("transfer")["cursor"]
            control.finish_job(job.job_id, "completed", "done")

            snapshot = control.watch_plan(
                "transfer",
                since=cursor,
                poll_interval_sec=0,
                timeout_sec=1,
            )

            self.assertFalse(snapshot["timed_out"])
            self.assertEqual(snapshot["awaiting_review"][0]["task_id"], "schema")
            self.assertEqual(snapshot["changes"][-1]["state"], "awaiting_review")

    def test_watch_plan_times_out_when_cursor_has_no_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            control.create_plan(plan_id="transfer", title="Transfer")
            cursor = control.plan_snapshot("transfer")["cursor"]

            snapshot = control.watch_plan(
                "transfer",
                since=cursor,
                poll_interval_sec=0,
                timeout_sec=0,
            )

            self.assertTrue(snapshot["timed_out"])
            self.assertEqual(snapshot["changes"], [])

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

    def test_watch_does_not_return_before_terminal_finalization_settles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-finalizing")
            control.store.mark_finished(job.job_id, "completed")

            with patch.object(control, "reconcile_jobs", return_value={}):
                summary = control.watch_job(
                    job.job_id,
                    poll_interval_sec=0,
                    timeout_sec=0,
                    include_details=True,
                )

            self.assertTrue(summary["terminal"])
            self.assertFalse(summary["settled"])
            self.assertIsNone(summary["on_contract"])
            self.assertTrue(summary["timed_out"])
            self.assertEqual(summary["finalization_status"], "pending")

    def test_watch_reports_no_verdict_for_unsettled_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            _create_job(control, root, "job-unsettled")
            control.store.update_job("job-unsettled", status="running")

            summary = control.watch_job("job-unsettled", poll_interval_sec=0, timeout_sec=0)

            self.assertFalse(summary["settled"])
            self.assertIsNone(summary["on_contract"])

    def test_watch_reports_true_verdict_for_settled_on_contract_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-on-contract")
            self.assertEqual(job.expected_result_status, "completed")
            control.finish_job(job.job_id, "completed", None)

            summary = control.watch_job(job.job_id, poll_interval_sec=0, timeout_sec=10)

            self.assertTrue(summary["settled"])
            self.assertTrue(summary["on_contract"])

    def test_watch_reports_false_verdict_for_settled_off_contract_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-off-contract")
            self.assertEqual(job.expected_result_status, "completed")
            control.finish_job(job.job_id, "failed", "boom")

            summary = control.watch_job(job.job_id, poll_interval_sec=0, timeout_sec=10)

            self.assertTrue(summary["settled"])
            self.assertFalse(summary["on_contract"])

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

    def test_compact_watch_reports_persisted_workspace_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-native")
            control.store.update_job(
                job.job_id,
                status="running",
                workspace_access="native",
            )

            summary = control.watch_job(
                job.job_id,
                poll_interval_sec=0,
                timeout_sec=0,
            )

            self.assertEqual(summary["workspace_access"], "native")

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
                "agent_control_plane.app.runtime.orchestrator.process_is_alive",
                return_value=False,
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


def _create_job(control: AgentControlPlane, root: Path, job_id: str, task_id: str = "task-1"):
    return control.store.create_job(
        job_id=job_id,
        task_id=task_id,
        route="main",
        workspace_path=root / "workspace",
        expected_branch="main",
        config_path=root / "workspaces.toml",
        run_dir=root / "runs" / job_id,
        prompt_path=root / "runs" / job_id / "prompt.md",
        result_path=root / "tasks" / task_id / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
    )


def _config(root: Path, *, auto_archive_days: int | None = None) -> ControlConfig:
    model_catalog = _model_catalog(root)
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
            codex_model="test-codex",
            codex_mechanical_model="test-codex",
            codex_balanced_model="test-codex",
            codex_deep_model="test-codex",
        ),
        model_catalog=model_catalog,
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


def _model_catalog(root: Path) -> CodexModelCatalogConfig:
    cache_path = root / "models_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "test-codex",
                        "supported_reasoning_levels": ["low", "medium"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return CodexModelCatalogConfig(
        cache_path=cache_path,
        max_cache_age_sec=60.0,
        models=(),
        quota_domains=(CodexQuotaDomainConfig("primary", 2, 8, 75.0),),
    )


class WatchEventsTest(unittest.TestCase):
    """`watch_events` is the non-blocking pass an MCP client polls with a cursor."""

    def _settle(self, control: AgentControlPlane, job) -> None:
        job.result_path.parent.mkdir(parents=True, exist_ok=True)
        job.result_path.write_text("Status: completed\n", encoding="utf-8")
        control.store.mark_finished(job.job_id, "completed")
        control.store.mark_finalization_completed(job.job_id)

    def test_settled_job_reports_done_with_the_terminal_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-events-1")
            self._settle(control, job)

            payload = control.watch_events(job_ids=[job.job_id])

            self.assertTrue(payload["done"])
            self.assertEqual(payload["pending"], [])
            self.assertEqual([event["kind"] for event in payload["events"]], ["terminal"])
            self.assertEqual(payload["events"][0]["status"], "completed")
            # The response carries the vocabulary so a caller never retypes it.
            self.assertEqual(payload["terminal_statuses"], sorted(TERMINAL_STATUSES))

    def test_cursor_suppresses_replay_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-events-2")

            first = control.watch_events(job_ids=[job.job_id])
            self.assertEqual([event["kind"] for event in first["events"]], ["start"])
            self.assertFalse(first["done"])

            second = control.watch_events(job_ids=[job.job_id], cursor=first["cursor"])
            self.assertEqual(second["events"], [])

            self._settle(control, job)
            third = control.watch_events(job_ids=[job.job_id], cursor=second["cursor"])
            self.assertEqual([event["kind"] for event in third["events"]], ["terminal"])
            self.assertTrue(third["done"])

    def test_status_that_never_goes_terminal_is_surfaced_not_waited_on(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-events-3")
            # cancel_requested is not terminal and can stay that way indefinitely.
            # A watcher must be able to see that rather than poll until its timeout.
            control.store.update_job(job.job_id, status="cancel_requested")

            payload = control.watch_events(job_ids=[job.job_id])

            self.assertFalse(payload["done"])
            self.assertEqual(
                payload["pending"],
                [{"job_id": job.job_id, "status": "cancel_requested"}],
            )
            self.assertNotIn("cancel_requested", payload["terminal_statuses"])

    def test_task_glob_selection_follows_every_matching_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            first = _create_job(control, root, "job-events-4a", task_id="task-4a")
            second = _create_job(control, root, "job-events-4b", task_id="task-4b")

            payload = control.watch_events(task_id_glob="task-4*")

            self.assertEqual(sorted(payload["watched"]), sorted([first.job_id, second.job_id]))

    def test_selection_matching_nothing_raises_instead_of_reporting_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))

            with self.assertRaises(EmptySelectionError):
                control.watch_events(task_id_glob="no-such-task-*")


class SupervisionHandoffTest(unittest.TestCase):
    """Launching supervises nothing, so every launch has to say how to supervise."""

    def test_watch_command_covers_the_launched_job_and_outlives_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))
            job = _create_job(control, root, "job-supervise-1")

            supervision = control.supervision_for([job.job_id])

            self.assertFalse(supervision["supervised"])
            command = supervision["watch_command"]
            self.assertIn("watch --events", command)
            self.assertIn(job.job_id, command)
            self.assertIn(str(root / "workspaces.toml"), command)
            # The job's own timeout plus finalization slack — not an invented number.
            expected = int(job.timeout_sec + FINALIZATION_SLACK_SEC)
            self.assertIn(f"--timeout-sec {expected}", command)

    def test_unknown_job_ids_do_not_break_the_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = AgentControlPlane(_config(root))

            supervision = control.supervision_for(["never-existed"])

            self.assertIn("never-existed", supervision["watch_command"])
            self.assertIn("--timeout-sec", supervision["watch_command"])


class WatchEventsSnapshotNudgeTest(unittest.TestCase):
    """A pass that is not done must not read as a subscription."""

    def _call(self, payload: dict) -> dict:
        from agent_control_plane.app.runtime.mcp_server import build_server

        control = Mock()
        control.watch_events.return_value = payload
        control.supervision_for.return_value = {
            "supervised": False,
            "watch_command": "agent-control watch --events job-1",
            "note": "n/a",
        }
        with patch(
            "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
            return_value=control,
        ):
            server = build_server()
        result = asyncio.run(
            server._tool_manager.call_tool("agent_watch_events", {"job_ids": ["job-1"]})
        )
        assert result.structuredContent is not None
        return result.structuredContent

    def test_unfinished_pass_says_nothing_will_notify_you(self) -> None:
        result = self._call(
            {
                "watched": ["job-1"],
                "events": [],
                "cursor": {"v": 1, "jobs": {}},
                "done": False,
                "pending": [{"job_id": "job-1", "status": "running"}],
                "terminal_statuses": [],
            }
        )

        self.assertIn("snapshot_only", result)
        self.assertIn("not a subscription", result["snapshot_only"])
        self.assertEqual(result["watch_command"], "agent-control watch --events job-1")

    def test_finished_pass_carries_no_nudge(self) -> None:
        result = self._call(
            {
                "watched": ["job-1"],
                "events": [],
                "cursor": {"v": 1, "jobs": {}},
                "done": True,
                "pending": [],
                "terminal_statuses": [],
            }
        )

        self.assertNotIn("snapshot_only", result)
        self.assertNotIn("watch_command", result)


if __name__ == "__main__":
    unittest.main()
