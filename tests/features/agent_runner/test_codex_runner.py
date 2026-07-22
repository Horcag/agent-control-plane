from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.codex_process_monitor import (
    CodexProcessMonitor,
)
from agent_control_plane.features.agent_runner.lib.codex_runner import (
    CodexExecRunner,
    _workspace_environment,
)
from agent_control_plane.features.agent_runner.lib.codex_watchdog import (
    CODEX_INEFFICIENT_TOOL_USAGE_LIMIT,
    CODEX_TOOL_TIMEOUT_LIMIT,
    dirty_file_markers_from_porcelain,
    is_known_temporary_patch_artifact,
    porcelain_changed_path,
    productive_log_activity_if_needed,
    scan_tool_timeouts,
    updated_calls_without_durable_progress,
)
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec


class CodexRunnerCommandTest(unittest.TestCase):
    def test_workspace_environment_uses_slot_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "slot"
            local_virtual_env = workspace / ".venv"
            local_scripts = local_virtual_env / ("Scripts" if os.name == "nt" else "bin")
            local_scripts.mkdir(parents=True)
            inherited_virtual_env = root / "controller-venv"
            inherited_scripts = inherited_virtual_env / ("Scripts" if os.name == "nt" else "bin")
            base_path = str(root / "bin")
            inherited_path = os.pathsep.join((str(inherited_scripts), base_path))

            with patch.dict(
                os.environ,
                {
                    "PATH": inherited_path,
                    "VIRTUAL_ENV": str(inherited_virtual_env),
                    "UV_PYTHON": "3.12",
                    "UV_PROJECT_ENVIRONMENT": str(inherited_virtual_env),
                    "PYTHONHOME": str(root / "python-home"),
                    "CONDA_PREFIX": str(root / "conda"),
                },
                clear=True,
            ):
                environment = _workspace_environment(workspace)

            self.assertEqual(environment["VIRTUAL_ENV"], str(local_virtual_env))
            self.assertEqual(environment["UV_PROJECT_ENVIRONMENT"], str(local_virtual_env))
            self.assertNotIn("UV_PYTHON", environment)
            self.assertNotIn("PYTHONHOME", environment)
            self.assertNotIn("CONDA_PREFIX", environment)
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(local_scripts))
            self.assertNotIn(str(inherited_scripts), environment["PATH"].split(os.pathsep))

    def test_workspace_environment_clears_inherited_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace-without-venv"
            workspace.mkdir()
            with patch.dict(
                os.environ,
                {
                    "PATH": "base-bin",
                    "VIRTUAL_ENV": "controller-venv",
                    "UV_PYTHON": "3.12",
                    "UV_PROJECT_ENVIRONMENT": "controller-venv",
                },
                clear=True,
            ):
                environment = _workspace_environment(workspace)

            self.assertNotIn("VIRTUAL_ENV", environment)
            self.assertNotIn("UV_PYTHON", environment)
            self.assertNotIn("UV_PROJECT_ENVIRONMENT", environment)
            self.assertEqual(environment["PATH"], "base-bin")

    def test_build_command_uses_exec_with_configured_writable_sandbox(self) -> None:
        spec = _spec(read_only=False, yolo=False, codex_sandbox_mode="workspace-write")

        command = CodexExecRunner._build_command(spec)

        self.assertEqual(command[0:4], ["codex", "exec", "--model", "gpt-5"])
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("--json", command)
        self.assertIn("--disable", command)
        self.assertIn("image_generation", command)
        self.assertIn("--cd", command)
        self.assertIn(str(spec.workspace_path), command)
        self.assertIn("--output-last-message", command)
        self.assertIn(str(spec.log_path.with_suffix(".last-message.md")), command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn(spec.prompt, command)

    def test_build_command_resumes_same_thread_for_escalation(self) -> None:
        spec = _spec(
            read_only=False,
            yolo=False,
            codex_resume_thread_id="019ef56b-74b4-70e2-9b0d-0e2c0ddfbc9c",
        )

        command = CodexExecRunner._build_command(spec)

        self.assertEqual(command[0:3], ["codex", "exec", "resume"])
        self.assertIn("--model", command)
        self.assertIn("gpt-5", command)
        self.assertIn(spec.codex_resume_thread_id, command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--cd", command)
        self.assertNotIn("--sandbox", command)

    def test_build_command_allows_danger_full_access_sandbox_without_yolo(self) -> None:
        command = CodexExecRunner._build_command(
            _spec(read_only=False, yolo=False, codex_sandbox_mode="danger-full-access")
        )

        self.assertIn("--sandbox", command)
        self.assertIn("danger-full-access", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_build_command_can_disable_configured_mcp_servers(self) -> None:
        command = CodexExecRunner._build_command(
            _spec(
                read_only=False,
                yolo=False,
                codex_disabled_mcp_servers=(
                    "agentbridge_idea_8644",
                    "agentbridge_dataspell_8643",
                ),
            )
        )

        self.assertIn("mcp_servers.agentbridge_idea_8644.enabled=false", command)
        self.assertIn("mcp_servers.agentbridge_dataspell_8643.enabled=false", command)

    def test_build_command_uses_read_only_sandbox_when_requested(self) -> None:
        command = CodexExecRunner._build_command(_spec(read_only=True, yolo=False))

        self.assertIn("read-only", command)
        self.assertNotIn("workspace-write", command)

    def test_native_initial_command_adds_coordination_root_for_both_sandboxes(self) -> None:
        for read_only, expected_sandbox in ((False, "workspace-write"), (True, "read-only")):
            with self.subTest(read_only=read_only):
                spec = _spec(
                    read_only=read_only,
                    yolo=False,
                    workspace_access="native",
                )

                command = CodexExecRunner._build_command(spec)

                add_dir_index = command.index("--add-dir")
                self.assertEqual(command[add_dir_index + 1], str(spec.result_path.parent))
                self.assertIn(expected_sandbox, command)

    def test_native_resume_keeps_original_workspace_roots(self) -> None:
        spec = _spec(
            read_only=False,
            yolo=False,
            workspace_access="native",
            codex_resume_thread_id="019ef56b-74b4-70e2-9b0d-0e2c0ddfbc9c",
        )

        command = CodexExecRunner._build_command(spec)

        self.assertNotIn("--cd", command)
        self.assertNotIn("--add-dir", command)
        self.assertNotIn("--sandbox", command)

    def test_yolo_bypasses_sandbox_flags(self) -> None:
        command = CodexExecRunner._build_command(_spec(read_only=False, yolo=True))

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--sandbox", command)

    def test_completed_wait_observes_usage_event_before_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.with_suffix(".events.jsonl").write_text(
                '{"type":"turn.completed","usage":{}}\n',
                encoding="utf-8",
            )
            proc = _FakeProc()
            spec = _spec(read_only=True, yolo=False, log_path=log_path)

            CodexProcessMonitor()._await_completed_process(proc, spec, cancel_requested=None)

            self.assertEqual(proc.wait_timeout, 1.0)
            self.assertFalse(proc.terminated)

    def test_writable_handoff_budget_boundary_requires_valid_pair(self) -> None:
        for verified, expected_status in ((True, "completed"), (False, "tool_call_budget")):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                log_path = root / "attempt-001.log"
                _write_budget_events(log_path, count=2)
                result_path = root / "result.md"
                result_path.write_text("Status: partial\n", encoding="utf-8")
                if verified:
                    _write_verification(root, status="partial")
                proc = _FakeProc()
                result = CodexProcessMonitor().monitor(
                    proc,
                    _spec(
                        read_only=False,
                        yolo=False,
                        log_path=log_path,
                        result_path=result_path,
                        tool_call_budget=1,
                    ),
                    started_wall=0.0,
                    deadline_mono=time.monotonic() + 10,
                    last_output_mono=time.monotonic(),
                    last_log_size=0,
                    log=io.StringIO(),
                    cancel_requested=lambda: False,
                )
                self.assertEqual(result.status, expected_status)
                self.assertEqual(proc.terminated, not verified)

    def test_budget_breach_grace_window_completes_normally_when_handoff_lands_in_time(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            _write_budget_events(log_path, count=2)
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            _write_verification(root, status="partial")
            proc = _FakeProc()

            result = CodexProcessMonitor().monitor(
                proc,
                _spec(
                    read_only=False,
                    yolo=False,
                    log_path=log_path,
                    result_path=result_path,
                    tool_call_budget=1,
                    tool_call_budget_grace_sec=120,
                ),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 10,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertEqual(result.status, "completed")
            self.assertFalse(proc.terminated)
            self.assertTrue(any(event.kind == "budget_breach" for event in result.lifecycle_events))

    def test_budget_breach_grace_window_expires_without_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            _write_budget_events(log_path, count=2)
            result_path = root / "result.md"
            proc = _FakeProc()

            result = CodexProcessMonitor().monitor(
                proc,
                _spec(
                    read_only=False,
                    yolo=False,
                    log_path=log_path,
                    result_path=result_path,
                    tool_call_budget=1,
                    tool_call_budget_grace_sec=1,
                ),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 10,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertEqual(result.status, "tool_call_budget")
            self.assertTrue(proc.terminated)
            self.assertIn("grace of", result.message)
            self.assertTrue(any(event.kind == "budget_breach" for event in result.lifecycle_events))

    def test_budget_breach_runaway_cap_terminates_immediately_during_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            events_path = log_path.with_suffix(".events.jsonl")
            # budget=1 -> runaway cap = 1 + max(4, 1 // 10) = 5; the first 2 calls cross the
            # budget (breach). Four more, appended one at a time slower than the monitor's
            # poll cadence so each is counted on its own scan, push the count to 6 (> 5),
            # firing the runaway cap instead of waiting out the grace window.
            _write_budget_events(log_path, count=2)
            result_path = root / "result.md"
            proc = _FakeProc()

            def _append_more_calls() -> None:
                for _ in range(4):
                    time.sleep(0.3)
                    with events_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {"type": "item.started", "item": {"type": "command_execution"}}
                            )
                            + "\n"
                        )

            appender = threading.Thread(target=_append_more_calls, daemon=True)
            appender.start()
            try:
                result = CodexProcessMonitor().monitor(
                    proc,
                    _spec(
                        read_only=False,
                        yolo=False,
                        log_path=log_path,
                        result_path=result_path,
                        tool_call_budget=1,
                        tool_call_budget_grace_sec=120,
                    ),
                    started_wall=0.0,
                    deadline_mono=time.monotonic() + 15,
                    last_output_mono=time.monotonic(),
                    last_log_size=0,
                    log=io.StringIO(),
                    cancel_requested=lambda: False,
                )
            finally:
                appender.join(timeout=5)

            self.assertEqual(result.status, "tool_call_budget")
            self.assertTrue(proc.terminated)
            self.assertIn("runaway cap", result.message)

    def test_writable_runner_stops_after_the_inefficient_tool_usage_limit_without_durable_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            _write_budget_events(log_path, count=CODEX_INEFFICIENT_TOOL_USAGE_LIMIT)
            proc = _FakeProc()

            result = CodexProcessMonitor().monitor(
                proc,
                _spec(read_only=False, yolo=False, log_path=log_path),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 10,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertEqual(result.status, "inefficient_tool_usage")
            self.assertTrue(proc.terminated)
            self.assertIn(str(CODEX_INEFFICIENT_TOOL_USAGE_LIMIT), result.message)
            self.assertIn(
                f"started {CODEX_INEFFICIENT_TOOL_USAGE_LIMIT} tool calls", result.message
            )
            self.assertNotIn("started 16 tool calls", result.message)

    def test_periodic_durable_progress_resets_tool_call_counter_before_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            source_path = root / "source.py"
            _write_budget_events(log_path, count=15)

            def publish_durable_progress(_delay: float) -> None:
                source_path.write_text("changed\n", encoding="utf-8")
                with log_path.with_suffix(".events.jsonl").open("a", encoding="utf-8") as events:
                    events.write(
                        json.dumps({"type": "item.started", "item": {"type": "command_execution"}})
                        + "\n"
                    )

            completed = AgentRunResult("completed", True, 0, "partial", "done")
            with (
                patch(
                    "agent_control_plane.features.agent_runner.lib.codex_process_monitor.progress_signature",
                    side_effect=[
                        (("before",), False),
                        (("before",), False),
                        (("source.py",), True),
                        (("source.py",), True),
                    ],
                ),
                patch(
                    "agent_control_plane.features.agent_runner.lib.codex_process_monitor.time.monotonic",
                    side_effect=[0.0, 1.0, 3.0],
                ),
                patch(
                    "agent_control_plane.features.agent_runner.lib.codex_process_monitor.time.sleep",
                    side_effect=publish_durable_progress,
                ),
                patch.object(
                    CodexProcessMonitor,
                    "_terminal_result_if_ready",
                    side_effect=[(None, 0, 0, 0), (completed, 0, 0, 0)],
                ),
            ):
                result = CodexProcessMonitor().monitor(
                    _FakeProc(),
                    _spec(read_only=False, yolo=False, log_path=log_path),
                    started_wall=0.0,
                    deadline_mono=10.0,
                    last_output_mono=0.0,
                    last_log_size=0,
                    log=io.StringIO(),
                    cancel_requested=lambda: False,
                )

            self.assertEqual(result.status, "completed")

    def test_read_only_runner_is_exempt_from_inefficient_tool_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            _write_budget_events(log_path, count=16)
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")

            result = CodexProcessMonitor().monitor(
                _FakeProc(),
                _spec(read_only=True, yolo=False, log_path=log_path, result_path=result_path),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 10,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertEqual(result.status, "completed")

    def test_rg_no_matches_exit_code_does_not_change_watchdog_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            _write_budget_events(log_path, count=CODEX_INEFFICIENT_TOOL_USAGE_LIMIT)
            log_path.write_text("rg completed with Exit code: 1\n", encoding="utf-8")

            result = CodexProcessMonitor().monitor(
                _FakeProc(),
                _spec(read_only=False, yolo=False, log_path=log_path),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 10,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertEqual(result.status, "inefficient_tool_usage")

    def test_durable_progress_resets_consecutive_tool_calls(self) -> None:
        cases = (
            (("before",), ("before",), 15, 1, 16),
            (("before",), ("after",), 15, 1, 0),
            (("progress",), ("result",), 15, 1, 0),
            (("result",), ("verification",), 15, 1, 0),
            (("source",), ("test",), 15, 1, 0),
        )

        for previous, current, consecutive, new_calls, expected in cases:
            with self.subTest(current=current):
                self.assertEqual(
                    updated_calls_without_durable_progress(
                        previous, current, consecutive, new_calls
                    ),
                    expected,
                )

    def test_monitor_distinguishes_writable_and_read_only_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            for read_only, expected in ((False, None), (True, "completed")):
                with self.subTest(read_only=read_only):
                    state = CodexProcessMonitor()._completed_result_if_ready(
                        _FakeProc(),
                        _spec(read_only=read_only, yolo=False, result_path=result_path),
                        0.0,
                        terminate=False,
                    )
                    self.assertEqual(state.status if state else None, expected)

    def test_writable_process_exit_with_invalid_verification_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            proc = _FakeProc()
            proc.terminate()

            state = CodexProcessMonitor()._exited_result_if_dead(
                proc, _spec(read_only=False, yolo=False, result_path=result_path), 0.0
            )

            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status, "exited_without_result")

    def test_writable_process_exit_with_invalid_verification_file_reports_invalid_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            _write_invalid_verification(root)
            proc = _FakeProc()
            proc.terminate()

            state = CodexProcessMonitor()._exited_result_if_dead(
                proc,
                _spec(
                    read_only=False,
                    yolo=False,
                    result_path=result_path,
                    invalid_verification_grace_sec=120,
                ),
                0.0,
            )

            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status, "invalid_verification")
            self.assertIn("unknown keys", state.message)

    def test_invalid_verification_grace_window_expires_and_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            _write_invalid_verification(root)
            proc = _FakeProc()

            result = CodexProcessMonitor().monitor(
                proc,
                _spec(
                    read_only=False,
                    yolo=False,
                    log_path=log_path,
                    result_path=result_path,
                    invalid_verification_grace_sec=1,
                ),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 10,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertEqual(result.status, "invalid_verification")
            self.assertTrue(proc.terminated)
            self.assertIn("unknown keys", result.message)

    def test_invalid_verification_grace_window_recovers_on_valid_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            _write_invalid_verification(root)
            log_path.with_suffix(".events.jsonl").write_text(
                json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
                encoding="utf-8",
            )
            proc = _FakeProc()

            def _fix_verification() -> None:
                time.sleep(0.3)
                _write_verification(root, status="partial")

            fixer = threading.Thread(target=_fix_verification, daemon=True)
            fixer.start()
            try:
                result = CodexProcessMonitor().monitor(
                    proc,
                    _spec(
                        read_only=False,
                        yolo=False,
                        log_path=log_path,
                        result_path=result_path,
                        invalid_verification_grace_sec=5,
                    ),
                    started_wall=0.0,
                    deadline_mono=time.monotonic() + 10,
                    last_output_mono=time.monotonic(),
                    last_log_size=0,
                    log=io.StringIO(),
                    cancel_requested=lambda: False,
                )
            finally:
                fixer.join(timeout=5)

            self.assertEqual(result.status, "completed")
            self.assertFalse(proc.terminated)

    def test_invalid_verification_grace_disabled_keeps_legacy_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            _write_invalid_verification(root)
            proc = _FakeProc()

            result = CodexProcessMonitor().monitor(
                proc,
                _spec(
                    read_only=False,
                    yolo=False,
                    log_path=log_path,
                    result_path=result_path,
                    invalid_verification_grace_sec=0,
                ),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 0.3,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )

            self.assertNotEqual(result.status, "invalid_verification")
            self.assertEqual(result.status, "timeout")

    def test_verification_mutation_after_terminal_observation_is_late_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_path = root / "attempt-001.log"
            _write_budget_events(log_path, count=0)
            result_path = root / "result.md"
            result_path.write_text("Status: partial\n", encoding="utf-8")
            _write_verification(root, status="partial")
            proc = _FakeProc()
            spec = _spec(read_only=False, yolo=False, log_path=log_path, result_path=result_path)
            with patch.object(
                proc,
                "wait",
                side_effect=lambda timeout: _write_verification(root, status="blocked"),
            ):
                state = CodexProcessMonitor()._completed_result_if_ready(
                    proc, spec, 0.0, terminate=True, cancel_requested=lambda: False
                )

            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status, "late_edit")

    def test_timeout_result_stops_clean_workspace_without_any_activity(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=4.0,
            last_productive_mono=4.0,
            workspace_dirty=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "no_progress_timeout")
        self.assertTrue(proc.terminated)

    def test_timeout_result_stops_when_output_continues_without_progress(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=9.5,
            last_productive_mono=4.0,
            workspace_dirty=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "no_progress_timeout")
        self.assertIn("workspace is clean", result.message)
        self.assertTrue(proc.terminated)

    def test_timeout_result_stops_dirty_workspace_without_file_progress(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=9.5,
            last_productive_mono=4.0,
            workspace_dirty=True,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "no_progress_timeout")
        self.assertIn("workspace is dirty", result.message)
        self.assertTrue(proc.terminated)

    def test_timeout_result_keeps_recent_productive_progress(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=9.5,
            last_productive_mono=9.5,
            workspace_dirty=True,
        )

        self.assertIsNone(result)
        self.assertFalse(proc.terminated)

    def test_repeated_tool_timeout_stops_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "2026-07-02 ERROR codex_core::tools::router: error=Exit code: 124\n"
                "2026-07-02 ERROR codex_core::tools::router: error=Exit code: 124\n",
                encoding="utf-8",
            )
            proc = _FakeProc()
            spec = _spec(
                read_only=False,
                yolo=False,
                log_path=log_path,
                tool_timeout_limit=2,
            )

            result, scan_size, timeout_count = CodexProcessMonitor()._tool_timeout_result_if_needed(
                proc,
                spec,
                scan_size=0,
                timeout_count=0,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "tool_timeout")
            self.assertIn("Exit code: 124", result.message)
            self.assertIn("limit 2", result.message)
            self.assertEqual(timeout_count, 2)
            self.assertGreater(scan_size, 0)
            self.assertTrue(proc.terminated)

    def test_tool_timeout_uses_default_limit_when_spec_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "2026-07-02 ERROR codex_core::tools::router: error=Exit code: 124\n"
                "2026-07-02 ERROR codex_core::tools::router: error=Exit code: 124\n",
                encoding="utf-8",
            )
            proc = _FakeProc()
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            result, _scan_size, timeout_count = (
                CodexProcessMonitor()._tool_timeout_result_if_needed(
                    proc,
                    spec,
                    scan_size=0,
                    timeout_count=0,
                )
            )

            self.assertIsNone(result)
            self.assertEqual(timeout_count, 2)
            self.assertFalse(proc.terminated)

    def test_scan_tool_timeouts_fires_on_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "Exit code: 124\nExit code: 124\n",
                encoding="utf-8",
            )

            triggered, scan_size, timeout_count = scan_tool_timeouts(log_path, 0, 0, limit=3)
            self.assertFalse(triggered)
            self.assertEqual(timeout_count, 2)

            log_path.write_text(
                "Exit code: 124\nExit code: 124\nExit code: 124\n",
                encoding="utf-8",
            )
            triggered, scan_size, timeout_count = scan_tool_timeouts(log_path, 0, 0, limit=3)
            self.assertTrue(triggered)
            self.assertEqual(timeout_count, 3)
            self.assertGreater(scan_size, 0)

    def test_scan_tool_timeouts_limit_zero_never_fires(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "Exit code: 124\n" * 50,
                encoding="utf-8",
            )

            triggered, _scan_size, timeout_count = scan_tool_timeouts(log_path, 0, 0, limit=0)

            self.assertFalse(triggered)
            self.assertEqual(timeout_count, 50)

    def test_scan_tool_timeouts_default_limit_matches_constant(self) -> None:
        import inspect

        signature = inspect.signature(scan_tool_timeouts)
        self.assertEqual(signature.parameters["limit"].default, CODEX_TOOL_TIMEOUT_LIMIT)

    def test_forbidden_web_search_stops_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text("codex\nweb search: https://example.com/\n", encoding="utf-8")
            proc = _FakeProc()
            spec = _spec(
                read_only=False,
                yolo=False,
                log_path=log_path,
                codex_forbidden_tool_markers=("web_search",),
            )

            result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                proc,
                spec,
                scan_size=0,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "forbidden_tool_usage")
            self.assertIn("web_search", result.message)
            self.assertGreater(scan_size, 0)
            self.assertTrue(proc.terminated)

    def test_forbidden_raw_exec_stops_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text("codex\nexec\npwsh -Command git status\n", encoding="utf-8")
            proc = _FakeProc()
            spec = _spec(
                read_only=False,
                yolo=False,
                log_path=log_path,
                codex_forbidden_tool_markers=("raw_exec",),
            )

            result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                proc,
                spec,
                scan_size=0,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "forbidden_tool_usage")
            self.assertIn("raw_exec", result.message)
            self.assertGreater(scan_size, 0)
            self.assertTrue(proc.terminated)

    def test_forbidden_agentbridge_project_discovery_stops_runner(self) -> None:
        cases = {
            "agentbridge_global_search": "search_text",
            "agentbridge_global_symbols": "search_symbols",
            "agentbridge_global_files": "list_project_files",
            "agentbridge_global_tree": "list_directory_tree",
            "agentbridge_external_attach": "attach_external_dir",
        }
        servers = (
            "agentbridge_idea_8644",
            "agentbridge_idea_64343",
            "agentbridge_dataspell_8643",
        )
        for marker_name, tool_name in cases.items():
            for server in servers:
                with (
                    self.subTest(marker_name=marker_name, server=server),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    log_path = Path(temp) / "attempt-001.log"
                    log_path.write_text(
                        f"mcp: {server}/{tool_name} started\n",
                        encoding="utf-8",
                    )
                    proc = _FakeProc()
                    spec = _spec(
                        read_only=True,
                        yolo=False,
                        log_path=log_path,
                        codex_forbidden_tool_markers=(marker_name,),
                    )

                    result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                        proc,
                        spec,
                        scan_size=0,
                    )

                    self.assertIsNotNone(result)
                    assert result is not None
                    self.assertEqual(result.status, "forbidden_tool_usage")
                    self.assertIn(marker_name, result.message)
                    self.assertGreater(scan_size, 0)
                    self.assertTrue(proc.terminated)

    def test_forbidden_markers_are_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text("codex\nexec\npwsh -Command git status\n", encoding="utf-8")
            proc = _FakeProc()
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                proc,
                spec,
                scan_size=0,
            )

            self.assertIsNone(result)
            self.assertEqual(scan_size, 0)
            self.assertFalse(proc.terminated)

    def test_agentbridge_tool_activity_counts_as_productive_log_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "codex\nmcp: agentbridge_idea_8644/read_file started\n"
                "mcp: agentbridge_idea_8644/read_file (completed)\n",
                encoding="utf-8",
            )
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            productive, scan_size = productive_log_activity_if_needed(
                spec,
                scan_size=0,
            )

            self.assertTrue(productive)
            self.assertGreater(scan_size, 0)

    def test_second_idea_agentbridge_activity_counts_as_productive_log_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "codex\nmcp: agentbridge_idea_64343/read_file started\n",
                encoding="utf-8",
            )
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            productive, scan_size = productive_log_activity_if_needed(spec, scan_size=0)

            self.assertTrue(productive)
            self.assertGreater(scan_size, 0)

    def test_model_manager_noise_is_not_productive_log_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "ERROR codex_models_manager::manager: failed to refresh available models\n",
                encoding="utf-8",
            )
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            productive, scan_size = productive_log_activity_if_needed(
                spec,
                scan_size=0,
            )

            self.assertFalse(productive)
            self.assertGreater(scan_size, 0)


class CodexRunnerProgressSignatureTest(unittest.TestCase):
    def test_only_known_temporary_patch_artifacts_are_excluded(self) -> None:
        excluded = ("changes.rej", "changes.orig", "tmp_fix.patch", "single_fix.patch")

        self.assertTrue(all(is_known_temporary_patch_artifact(path) for path in excluded))
        self.assertFalse(is_known_temporary_patch_artifact("scoped-change.patch"))

    def test_dirty_file_markers_exclude_only_known_temporary_patch_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patch = root / "scoped-change.patch"
            patch.write_text("visible", encoding="utf-8")

            markers = dirty_file_markers_from_porcelain(
                root,
                "?? tmp_fix.patch\n?? scoped-change.patch\n?? changes.rej",
            )

            self.assertEqual(len(markers), 1)
            self.assertTrue(markers[0].startswith("dirty-file:scoped-change.patch:"))

    def test_porcelain_changed_path_uses_target_path_for_renames(self) -> None:
        self.assertEqual(
            porcelain_changed_path("R  old/name.txt -> new/name.txt"),
            "new/name.txt",
        )

    def test_dirty_file_markers_include_changed_file_stat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            changed = root / "frontend" / "preview.ts"
            changed.parent.mkdir(parents=True)
            changed.write_text("first", encoding="utf-8")

            markers = dirty_file_markers_from_porcelain(root, "?? frontend/preview.ts")

            self.assertEqual(len(markers), 1)
            self.assertTrue(markers[0].startswith("dirty-file:frontend/preview.ts:"))
            self.assertTrue(markers[0].endswith(":5"))


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.wait_timeout: float | None = None

    def poll(self) -> int | None:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeout = timeout
        return 0

    def kill(self) -> None:
        self.killed = True


def _spec(
    *,
    read_only: bool,
    yolo: bool,
    codex_sandbox_mode: str = "workspace-write",
    codex_disabled_mcp_servers: tuple[str, ...] = (),
    no_progress_timeout_sec: int = 0,
    tool_timeout_limit: int = 6,
    codex_forbidden_tool_markers: tuple[str, ...] = (),
    codex_resume_thread_id: str | None = None,
    tool_call_budget: int = 0,
    tool_call_budget_grace_sec: int = 0,
    invalid_verification_grace_sec: int = 0,
    log_path: Path | None = None,
    result_path: Path | None = None,
    workspace_access: str = "ide_mcp",
) -> AgentRunSpec:
    return AgentRunSpec(
        backend="codex",
        agy_command="agy",
        codex_command="codex",
        codex_model="gpt-5",
        codex_reasoning_effort="low",
        codex_sandbox_mode=codex_sandbox_mode,
        codex_disabled_mcp_servers=codex_disabled_mcp_servers,
        no_progress_timeout_sec=no_progress_timeout_sec,
        tool_timeout_limit=tool_timeout_limit,
        tool_call_budget=tool_call_budget,
        tool_call_budget_grace_sec=tool_call_budget_grace_sec,
        invalid_verification_grace_sec=invalid_verification_grace_sec,
        codex_forbidden_tool_markers=codex_forbidden_tool_markers,
        codex_resume_thread_id=codex_resume_thread_id,
        prompt="secret task prompt",
        workspace_path=Path("D:/repo/workspace"),
        result_path=result_path or Path("D:/repo/.agent-work/tasks/task-1/result.md"),
        log_path=log_path or Path("D:/repo/runs/job-1/attempt-001.log"),
        print_timeout="10s",
        timeout_sec=30,
        idle_timeout_sec=10,
        yolo=yolo,
        read_only=read_only,
        workspace_access=workspace_access,
    )


def _write_budget_events(log_path: Path, *, count: int) -> None:
    log_path.with_suffix(".events.jsonl").write_text(
        "".join(
            json.dumps({"type": "item.started", "item": {"type": "command_execution"}}) + "\n"
            for _ in range(count)
        )
        + json.dumps({"type": "turn.completed", "usage": {}})
        + "\n",
        encoding="utf-8",
    )


def _write_verification(root: Path, *, status: str) -> None:
    (root / "verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "changed_files": [],
                "checks": [],
                "unverified": [],
            }
        ),
        encoding="utf-8",
    )


def _write_invalid_verification(root: Path) -> None:
    (root / "verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "partial",
                "changed_files": [],
                "checks": [],
                "unverified": [],
                "extra": "unexpected",
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
