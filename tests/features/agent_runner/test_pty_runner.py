from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from agent_control_plane.features.agent_runner.lib import pty_runner, windows_pipe
from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunSpec, PtyAgyRunner
from agent_control_plane.features.agent_runner.lib.windows_pipe import WindowsPipeProcess
from agent_control_plane.shared.agy_activity import read_agy_activity


class PtyBackendSelectionTest(unittest.TestCase):
    """The PTY backend is chosen by platform, so both branches need covering.

    pywinpty and ptyprocess each refuse to install on the other platform, so a
    given CI runner can only ever exercise one branch for real. Both are driven
    through a stubbed importlib instead.
    """

    def _load_with(self, os_name: str, modules: dict[str, object]) -> object:
        def fake_import(name: str) -> object:
            if name in modules:
                return modules[name]
            raise ImportError(f"No module named {name!r}")

        with (
            patch.object(pty_runner.os, "name", os_name),
            patch.object(pty_runner.importlib, "import_module", side_effect=fake_import),
        ):
            return pty_runner._load_pty_process()

    def test_windows_checks_pywinpty_and_uses_structured_pipes(self) -> None:
        winpty = SimpleNamespace(PtyProcess="conpty-backend")

        self.assertIs(self._load_with("nt", {"winpty": winpty}), WindowsPipeProcess)

    def test_posix_uses_the_unicode_ptyprocess_class(self) -> None:
        # PtyProcessUnicode, not PtyProcess: the reader thread and the trust-prompt
        # matcher both operate on str, and the bytes class would feed them bytes.
        ptyprocess = SimpleNamespace(
            PtyProcessUnicode="posix-backend",
            PtyProcess="bytes-backend",
        )

        self.assertEqual(self._load_with("posix", {"ptyprocess": ptyprocess}), "posix-backend")

    def test_missing_backend_names_the_distribution_to_install(self) -> None:
        for os_name, distribution in (("nt", "pywinpty"), ("posix", "ptyprocess")):
            with self.subTest(os_name=os_name):
                with self.assertRaises(ImportError) as caught:
                    self._load_with(os_name, {})

                self.assertIn(distribution, str(caught.exception))

    def test_run_reports_a_missing_backend_as_blocked(self) -> None:
        with patch.object(
            pty_runner,
            "_load_pty_process",
            side_effect=ImportError("ptyprocess is not installed or cannot be imported: boom"),
        ):
            result = PtyAgyRunner().run(
                _spec(prompt="task", yolo=False),
                cancel_requested=lambda: False,
                pid_observed=lambda _pid: None,
            )

        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.completed)
        self.assertIn("ptyprocess", result.message)


class WindowsPipeProcessTest(unittest.TestCase):
    def test_failed_construction_reaps_only_the_spawned_child_and_closes_its_pipe(self) -> None:
        for missing_pipe in (True, False):
            with self.subTest(missing_pipe=missing_pipe):
                process = Mock(stdout=None if missing_pipe else Mock())
                process.poll.return_value = None
                process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 2), 0]
                with (
                    patch.object(windows_pipe.subprocess, "Popen", return_value=process),
                    patch.object(
                        windows_pipe.codecs,
                        "getincrementaldecoder",
                        side_effect=RuntimeError("decoder unavailable"),
                    ),
                    self.assertRaisesRegex(
                        RuntimeError, "stdout pipe" if missing_pipe else "decoder unavailable"
                    ),
                ):
                    WindowsPipeProcess.spawn(["synthetic-agent"])
                process.terminate.assert_called_once_with()
                process.kill.assert_called_once_with()
                self.assertEqual(process.wait.call_count, 2)
                if process.stdout is not None:
                    process.stdout.close.assert_called_once_with()

    def test_spawn_preserves_argv_and_hides_the_console(self) -> None:
        process = Mock(pid=123)
        command = ["python", "fixture with spaces.py", "--print", "synthetic prompt"]
        with (
            patch.object(windows_pipe.subprocess, "Popen", return_value=process) as popen,
            patch.object(windows_pipe.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            patch.object(windows_pipe.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
        ):
            proc = WindowsPipeProcess.spawn(command, cwd="fixture directory")
        popen.assert_called_once_with(
            command,
            cwd="fixture directory",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=0x08000200,
        )
        self.assertEqual(proc.pid, 123)
        proc.close()
        process.stdout.close.assert_called_once_with()

    def test_partial_utf8_is_decoded_strictly_across_reads(self) -> None:
        text = "Ж🙂\n"
        encoded = text.encode("utf-8")
        process = Mock()
        process.stdout.read1.side_effect = [encoded[:1], encoded[1:3], encoded[3:], b""]
        proc = WindowsPipeProcess(process)
        chunks = []
        try:
            with self.assertRaises(EOFError):
                while True:
                    chunks.append(proc.read(4096))
            self.assertEqual("".join(chunks), text)
            self.assertNotIn("", chunks)
        finally:
            proc.close()

    def test_truncated_utf8_and_pipe_errors_remain_reader_failures(self) -> None:
        for ending in ([b"\xf0", b""], [b"\xff"], [OSError("pipe read failed")]):
            with self.subTest(ending=ending):
                process = Mock()
                process.stdout.read1.side_effect = [_stream(FINAL_RESPONSE).encode(), *ending]
                proc = WindowsPipeProcess(process)
                output: queue.Queue[str] = queue.Queue()
                done = threading.Event()
                errors: queue.Queue[str] = queue.Queue()
                try:
                    PtyAgyRunner._reader(proc, output, done, errors)
                    self.assertTrue(done.is_set())
                    self.assertEqual(output.get_nowait(), _stream(FINAL_RESPONSE))
                    self.assertIn(
                        "OSError" if isinstance(ending[0], OSError) else "UnicodeDecodeError",
                        errors.get_nowait(),
                    )
                finally:
                    proc.close()

    def test_forced_termination_kills_and_waits_only_for_the_spawned_process(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 2), 0]
        proc = WindowsPipeProcess(process)
        try:
            proc.terminate(force=True)
            process.terminate.assert_called_once_with()
            process.kill.assert_called_once_with()
            self.assertEqual(process.wait.call_count, 2)
            self.assertFalse(process.stdout.close.called)
        finally:
            proc.close()

    def test_partial_event_is_available_before_the_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc = WindowsPipeProcess.spawn(
                [
                    sys.executable,
                    "-B",
                    "-u",
                    "-c",
                    "import sys,time; print('synthetic event'); sys.stdout.flush(); time.sleep(10)",
                ],
                cwd=directory,
            )
            output: queue.Queue[str] = queue.Queue()
            done = threading.Event()
            errors: queue.Queue[str] = queue.Queue()
            stop = threading.Event()
            reader = threading.Thread(
                target=PtyAgyRunner._reader, args=(proc, output, done, errors, stop)
            )
            reader.start()
            try:
                self.assertIn("synthetic event", output.get(timeout=3))
                self.assertTrue(proc.isalive())
                self.assertFalse(done.is_set())
            finally:
                stop.set()
                proc.terminate(force=True)
                reader.join(3)
                if not reader.is_alive():
                    proc.close()
            self.assertFalse(reader.is_alive())
            self.assertFalse(proc.isalive())


class PtyRunnerCommandTest(unittest.TestCase):
    def test_eligibility_refusal_is_blocked_without_leaking_verification_url(self) -> None:
        proc = SimpleNamespace(isalive=lambda: False, exitstatus=1)
        output = (
            "Eligibility check failed: Your current account is not eligible for Antigravity.\n"
            "Please verify your account: https://accounts.google.com/private-token\n"
        )
        with (
            patch.object(PtyAgyRunner, "_drain_output", return_value=output),
            patch.object(
                pty_runner,
                "inspect_result",
                return_value=SimpleNamespace(done=False, reason="missing"),
            ),
        ):
            result = PtyAgyRunner()._exited_result_if_dead(
                proc,
                SimpleNamespace(),
                SimpleNamespace(),
                _spec(prompt="task", yolo=False),
                0,
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "blocked")
        self.assertIn("eligibility", result.message)
        self.assertNotIn("private-token", result.message)

    def test_build_command_uses_non_interactive_print_mode(self) -> None:
        spec = _spec(
            prompt="do the task",
            yolo=True,
            agy_model="Gemini 3.5 Flash (High)",
        )

        command = PtyAgyRunner._build_command(spec)

        self.assertEqual(
            command,
            [
                "agy",
                "--dangerously-skip-permissions",
                "--model",
                "Gemini 3.5 Flash (High)",
                "--print",
                "do the task",
                "--print-timeout",
                "10s",
                "--output-format",
                "stream-json",
                "--log-file",
                str(spec.log_path.with_suffix(".agy-vendor.log")),
            ],
        )
        self.assertNotIn("--prompt-interactive", command)

    def test_display_command_redacts_prompt(self) -> None:
        spec = _spec(
            prompt="secret prompt",
            yolo=False,
            agy_model="Gemini 3.5 Flash (High)",
        )

        display = PtyAgyRunner._display_command(spec)

        self.assertEqual(
            display,
            "agy --model Gemini 3.5 Flash (High) --print <prompt> --print-timeout 10s "
            f"--output-format stream-json --log-file {spec.log_path.with_suffix('.agy-vendor.log')}",
        )
        self.assertNotIn("secret prompt", display)

    def test_vendor_log_budget_stops_only_spawned_child_and_caps_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec = replace(_spec(prompt="task", yolo=False), log_path=Path(temp) / "attempt.log")
            vendor_path = pty_runner.vendor_log_path(spec.log_path)
            vendor_path.write_bytes(b"x" * 100)
            capture = pty_runner.AgyOutput(spec.log_path, "job")
            proc = _FakeProcess()
            with patch.object(pty_runner, "VENDOR_LOG_LIMIT", 64):
                result = PtyAgyRunner()._monitor_process(
                    proc,
                    queue.Queue(),
                    io.StringIO(),
                    spec,
                    0,
                    0,
                    100,
                    lambda: False,
                    threading.Event(),
                    queue.Queue(),
                    capture,
                )
            self.assertEqual(result.status, "blocked")
            self.assertTrue(proc.terminated)
            self.assertEqual(vendor_path.stat().st_size, 64)
            capture.finish(clean=False)

    def test_vendor_log_missing_or_live_writer_fails_closed(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                spec = replace(
                    _spec(prompt="task", yolo=False), log_path=Path(temp) / "attempt.log"
                )
                vendor_path = pty_runner.vendor_log_path(spec.log_path)
                if not missing:
                    vendor_path.write_bytes(b"x" * 100)
                capture = pty_runner.AgyOutput(spec.log_path, "job")
                proc = _FakeProcess()
                proc.terminate = (
                    Mock()
                )  # A failed termination must not allow live-writer truncation.
                with patch.object(pty_runner, "VENDOR_LOG_LIMIT", 64):
                    result = PtyAgyRunner()._monitor_process(
                        proc,
                        queue.Queue(),
                        io.StringIO(),
                        spec,
                        0,
                        0,
                        100,
                        lambda: False,
                        threading.Event(),
                        queue.Queue(),
                        capture,
                    )
                self.assertEqual(result.status, "blocked")
                proc.terminate.assert_called_once_with(force=True)
                if missing:
                    self.assertIn("unavailable", result.message)
                else:
                    self.assertIn("preserved", result.message)
                    self.assertEqual(vendor_path.stat().st_size, 100)
                capture.finish(clean=False)

    def test_detects_workspace_trust_prompt(self) -> None:
        output = """
        Accessing workspace:

        D:\\Projects\\repo

        Do you trust the contents of this project?

        Antigravity CLI requires permission to read, edit, and execute files here.
        """

        message = PtyAgyRunner._trust_prompt_message_if_needed(output)

        self.assertIsNotNone(message)
        self.assertIn("workspace trust prompt", message or "")

    def test_ignores_normal_agent_output(self) -> None:
        message = PtyAgyRunner._trust_prompt_message_if_needed("Status: completed\n")

        self.assertIsNone(message)

    def test_changed_durable_signature_refreshes_activity(self) -> None:
        spec = _spec(prompt="task", yolo=False)

        with patch(
            "agent_control_plane.features.agent_runner.lib.pty_runner.progress_signature",
            return_value=(("agent-progress.md:2:20",), True),
        ):
            activity, signature = PtyAgyRunner._refresh_durable_activity(
                spec,
                previous_signature=("agent-progress.md:1:10",),
                last_activity_mono=5.0,
                now=12.0,
            )

        self.assertEqual(12.0, activity)
        self.assertEqual(("agent-progress.md:2:20",), signature)

    def test_static_dirty_signature_does_not_refresh_activity(self) -> None:
        spec = _spec(prompt="task", yolo=False)
        signature = ("git-status:?? src/new.py", "dirty-file:src/new.py:1:10")

        with patch(
            "agent_control_plane.features.agent_runner.lib.pty_runner.progress_signature",
            return_value=(signature, True),
        ):
            activity, current = PtyAgyRunner._refresh_durable_activity(
                spec,
                previous_signature=signature,
                last_activity_mono=5.0,
                now=12.0,
            )

        self.assertEqual(5.0, activity)
        self.assertEqual(signature, current)

    def test_hard_timeout_wins_despite_recent_activity(self) -> None:
        proc = _FakeProcess()
        result = PtyAgyRunner()._timeout_result_if_needed(
            proc,
            _spec(prompt="task", yolo=False),
            now=30.0,
            deadline_mono=30.0,
            last_activity_mono=29.9,
        )

        self.assertIsNotNone(result)
        self.assertEqual("timeout", result.status if result else None)
        self.assertTrue(proc.terminated)

    def test_idle_timeout_mentions_verified_stream_and_durable_progress(self) -> None:
        proc = _FakeProcess()
        result = PtyAgyRunner()._timeout_result_if_needed(
            proc,
            _spec(prompt="task", yolo=False),
            now=11.0,
            deadline_mono=30.0,
            last_activity_mono=1.0,
        )

        self.assertIsNotNone(result)
        self.assertEqual("idle_timeout", result.status if result else None)
        self.assertIn("verified AGY stream or durable progress", result.message if result else "")
        self.assertTrue(proc.terminated)

    def test_monitor_uses_verified_progress_and_marks_quiet_active_operations(self) -> None:
        scenarios = (
            ("changing_model", "agent_response", "active", True, "cancelled", False),
            ("silent_tool", "tool", "active", False, "cancelled", True),
            ("silent_model", "agent_response", "active", False, "cancelled", True),
            ("terminal_backlog", "tool", "done", False, "idle_timeout", False),
            ("hard_deadline", "tool", "active", False, "timeout", True),
            ("startup_noise", "user_input", "done", False, "idle_timeout", False),
            ("active_without_init", "tool", "active", False, "idle_timeout", False),
            ("missing_init", "user_input", "done", False, "idle_timeout", False),
            ("malformed_init", "user_input", "done", False, "idle_timeout", False),
            ("foreign_init", "user_input", "done", False, "idle_timeout", False),
        )
        for name, step_type, state, changing, expected, quiet in scenarios:
            with self.subTest(name=name):
                self._verify_monitor_progress_case(
                    name, step_type, state, changing, expected, quiet
                )

    def _verify_monitor_progress_case(
        self, name: str, step_type: str, state: str, changing: bool, expected: str, quiet: bool
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = replace(
                _spec(prompt="task", yolo=False),
                log_path=root / "attempt.log",
                result_path=root / "result.md",
                idle_timeout_sec=3,
            )
            pty_runner.vendor_log_path(spec.log_path).write_text("")
            capture = pty_runner.AgyOutput(spec.log_path, "job")
            output: queue.Queue[str] = queue.Queue()
            clock = [0.0]
            if expected != "idle_timeout":
                output.put(json.dumps({"event": "init", "conversation_id": str(uuid4())}) + "\n")
            elif name in ("missing_init", "malformed_init", "foreign_init"):
                init = {"event": "init"}
                if name == "malformed_init":
                    init["conversation_id"] = "not-a-uuid"
                elif name == "foreign_init":
                    init.update(conversation_id=str(uuid4()), job_id="another-job")
                output.put(json.dumps(init) + "\n")

            def enqueue() -> None:
                event = {
                    "event": "step_update",
                    "step_update": {
                        "step_type": step_type,
                        "step_index": 10,
                        "state": state,
                        "text_delta": f"PRIVATE{clock[0] if changing else 0}",
                        "usage": {"output_tokens": int(clock[0]) if changing else 0},
                    },
                }
                output.put(json.dumps(event) + "\n")
                output.put(json.dumps({"event": "heartbeat"}) + "\n")
                output.put(
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_type": "tool",
                                "step_index": 1,
                                "state": "done",
                            },
                        }
                    )
                    + "\n"
                )

            def tick(_delay: float) -> None:
                clock[0] += 1.0
                enqueue()

            enqueue()
            proc = _FakeProcess()
            with (
                patch.object(
                    pty_runner, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)
                ),
                patch.object(pty_runner, "progress_signature", return_value=(None, False)),
            ):
                result = PtyAgyRunner()._monitor_process(
                    proc,
                    output,
                    io.StringIO(),
                    spec,
                    0,
                    0,
                    8 if name == "hard_deadline" else 30,
                    lambda: clock[0] >= 12,
                    threading.Event(),
                    queue.Queue(),
                    capture,
                )
            self.assertEqual(result.status, expected)
            self.assertEqual(capture.quiet, quiet)
            if expected == "idle_timeout":
                self.assertLess(clock[0], 12)
            if changing:
                self.assertGreater(capture.progress_sequence, 10)
            self.assertNotIn("PRIVATE", capture.snapshot_path.read_text())
            capture.finish(clean=False)

    def test_initialized_turn_completes_after_done_gap_longer_than_idle_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = replace(
                _spec(prompt="task", yolo=False),
                log_path=root / "attempt.log",
                result_path=root / "result.md",
                idle_timeout_sec=3,
                read_only=True,
            )
            pty_runner.vendor_log_path(spec.log_path).write_text("")
            capture = pty_runner.AgyOutput(spec.log_path, "job")
            output: queue.Queue[str] = queue.Queue()
            output.put(json.dumps({"event": "init", "conversation_id": str(uuid4())}) + "\n")
            output.put(
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "tool",
                            "step_index": 1,
                            "state": "DONE",
                        },
                    }
                )
                + "\n"
            )
            clock = [0.0]
            proc = _FakeProcess()
            done = threading.Event()
            observed_quiet = []

            def tick(_delay: float) -> None:
                clock[0] += 1
                if clock[0] == 7:
                    observed_quiet.append(capture.quiet)
                    output.put(
                        json.dumps(
                            {
                                "event": "step_update",
                                "step_update": {
                                    "step_type": "agent_response",
                                    "step_index": 2,
                                    "state": "DONE",
                                },
                            }
                        )
                        + "\n"
                        + _stream(FINAL_RESPONSE)
                    )
                    proc.terminated = True
                    proc.exitstatus = 0
                    done.set()

            with (
                patch.object(
                    pty_runner, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)
                ),
                patch.object(pty_runner, "progress_signature", return_value=(None, False)),
            ):
                result = PtyAgyRunner()._monitor_process(
                    proc,
                    output,
                    io.StringIO(),
                    spec,
                    0,
                    0,
                    30,
                    lambda: False,
                    done,
                    queue.Queue(),
                    capture,
                )
            self.assertEqual(observed_quiet, [True])
            self.assertEqual(result.status, "completed")
            self.assertEqual(spec.result_path.read_text(), FINAL_RESPONSE)
            self.assertFalse(capture.has_inflight_turn)


FINAL_RESPONSE = (
    "Status: completed\n"
    "Changed files: none\n"
    "What changed: ACP_GEMINI_SHORT_20261026\n"
    "Verification performed: pwd returned exact slot\n"
    "Not verified / remaining risks: no exhaustive tests requested\n"
)


class PtyReadonlyRecoveryTest(unittest.TestCase):
    def _recover(
        self,
        root: Path,
        output: str,
        *,
        read_only: bool = True,
        exit_code: int = 0,
        drained: bool = True,
        prompt: str = "Inspect the exact workspace; return the final result.",
    ) -> pty_runner.AgyRunResult:
        spec = replace(
            _spec(prompt=prompt, yolo=False),
            result_path=root / "result.md",
            read_only=read_only,
        )
        chunks: queue.Queue[str] = queue.Queue()
        chunks.put(output)
        done = threading.Event()
        if drained:
            done.set()
        proc = SimpleNamespace(isalive=lambda: False, exitstatus=exit_code)
        with patch.object(pty_runner, "READER_DRAIN_TIMEOUT_SEC", 0.01):
            result = PtyAgyRunner()._exited_result_if_dead(
                proc, chunks, io.StringIO(), spec, 0, reader_done=done
            )
        assert result is not None
        return result

    def test_real_crlf_envelopes_recover_all_terminal_statuses(self) -> None:
        for status in ("completed", "partial", "blocked"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "result.md").write_text("Awaiting `agy`\n", encoding="utf-8")
                envelope = FINAL_RESPONSE.replace("completed", status)
                output = "\x1b[32m" + envelope.replace("\n", "\r\n") + "\x1b[0m"
                output += "\r\n[pty reader stopped: EOF]\r\n"

                result = self._recover(root, output)

                self.assertTrue(result.completed)
                self.assertEqual(result.result_status, status)
                self.assertEqual((root / "result.md").read_text(), envelope)
                state = pty_runner.inspect_result(root / "result.md", 0)
                self.assertTrue(state.done)
                self.assertEqual(state.missing_sections, ())

    def test_preserves_existing_fresh_valid_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = FINAL_RESPONSE.replace("completed", "partial")
            (root / "result.md").write_text(original, encoding="utf-8")

            result = self._recover(root, FINAL_RESPONSE)

            self.assertEqual(result.result_status, "partial")
            self.assertEqual((root / "result.md").read_text(), original)

    def test_stale_result_is_replaced_only_by_fresh_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "result.md"
            path.write_text(FINAL_RESPONSE.replace("completed", "blocked"), encoding="utf-8")
            os.utime(path, (1, 1))
            spec = replace(
                _spec(prompt="Inspect only.", yolo=False), result_path=path, read_only=True
            )
            done = threading.Event()
            done.set()
            chunks: queue.Queue[str] = queue.Queue()
            chunks.put(FINAL_RESPONSE)
            result = PtyAgyRunner()._exited_result_if_dead(
                SimpleNamespace(isalive=lambda: False, exitstatus=0),
                chunks,
                io.StringIO(),
                spec,
                2,
                reader_done=done,
            )
            assert result is not None
            self.assertTrue(result.completed)
            self.assertEqual(path.read_text(), FINAL_RESPONSE)

    def test_rejects_ambiguous_incomplete_or_oversized_stdout(self) -> None:
        candidates = {
            "missing": "",
            "no envelope": "Agent finished successfully.",
            "invalid status": FINAL_RESPONSE.replace("completed", "success"),
            "malformed status": FINAL_RESPONSE.replace("completed", "completed later"),
            "missing section": FINAL_RESPONSE.replace("Changed files: none\n", ""),
            "empty section": FINAL_RESPONSE.replace("Changed files: none", "Changed files:"),
            "truncated": FINAL_RESPONSE.split("Not verified")[0],
            "empty last section": FINAL_RESPONSE.split("risks:")[0] + "risks:",
            "oversized": "x" * pty_runner.FINAL_RESPONSE_LIMIT + FINAL_RESPONSE,
            "oversized body": FINAL_RESPONSE + "x" * pty_runner.FINAL_RESPONSE_LIMIT,
            "prompt echo": "Inspect the exact workspace; return the final result.\n"
            + FINAL_RESPONSE,
            "embedded prompt": FINAL_RESPONSE
            + "Inspect the exact workspace; return the final result.",
            "source snippet": "```python\n" + FINAL_RESPONSE + "```\n",
            "embedded source": FINAL_RESPONSE + "```python\nprint('example')\n```\n",
            "multiple": FINAL_RESPONSE + FINAL_RESPONSE,
            "conflicting": FINAL_RESPONSE + "Status: blocked\n",
            "earlier log": FINAL_RESPONSE + "[log] Status: completed\n",
            "preamble": "Here is a source example:\n" + FINAL_RESPONSE,
        }
        for name, output in candidates.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result = self._recover(root, output)
                self.assertEqual(result.status, "exited_without_result")
                self.assertFalse((root / "result.md").exists())

    def test_recovery_cannot_bypass_writable_exit_or_drain_contract(self) -> None:
        for options in ({"read_only": False}, {"exit_code": 1}, {"drained": False}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                placeholder = "Awaiting `agy`\n"
                (root / "result.md").write_text(placeholder, encoding="utf-8")
                result = self._recover(root, FINAL_RESPONSE, **options)
                self.assertFalse(result.completed)
                self.assertEqual(result.status, "exited_without_result")
                self.assertEqual((root / "result.md").read_text(), placeholder)

    def test_exit_drains_a_full_bounded_queue_before_waiting_for_reader_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = replace(
                _spec(prompt="Inspect only.", yolo=False),
                result_path=root / "result.md",
                log_path=root / "attempt.log",
                read_only=True,
            )
            output: queue.Queue[str] = queue.Queue(maxsize=256)
            for _ in range(256):
                output.put(json.dumps({"event": "heartbeat"}) + "\n")
            done = threading.Event()
            errors: queue.Queue[str] = queue.Queue()
            reached = threading.Event()

            def read(size: int) -> str:
                del size
                reached.set()
                return _stream(FINAL_RESPONSE)

            proc = SimpleNamespace(
                isalive=lambda: False,
                exitstatus=0,
                read=Mock(side_effect=[read(4096), EOFError("done")]),
            )
            capture = pty_runner.AgyOutput(spec.log_path, "job")
            reader = threading.Thread(
                target=PtyAgyRunner._reader, args=(proc, output, done, errors)
            )
            reader.start()
            self.assertTrue(reached.wait(1))
            try:
                result = PtyAgyRunner()._exited_result_if_dead(
                    proc,
                    output,
                    io.StringIO(),
                    spec,
                    0,
                    reader_done=done,
                    reader_errors=errors,
                    capture=capture,
                )
                assert result is not None
                self.assertTrue(result.completed)
                self.assertEqual(spec.result_path.read_text(), FINAL_RESPONSE)
                self.assertTrue(done.is_set())
            finally:
                reader.join(1)
            self.assertFalse(reader.is_alive())

    def test_queue_backpressure_reader_can_be_stopped_without_hanging(self) -> None:
        output: queue.Queue[str] = queue.Queue(maxsize=1)
        output.put("already full")
        stop = threading.Event()
        done = threading.Event()
        errors: queue.Queue[str] = queue.Queue()
        proc = SimpleNamespace(read=Mock(return_value="private chunk"))
        reader = threading.Thread(
            target=PtyAgyRunner._reader, args=(proc, output, done, errors, stop)
        )
        reader.start()
        stop.set()
        reader.join(1)
        self.assertFalse(reader.is_alive())
        self.assertTrue(done.is_set())
        self.assertEqual(errors.get_nowait(), "PTY reader stopped before clean EOF")

    def test_quoted_tool_refusals_are_not_cli_diagnostics(self) -> None:
        refusals = (
            "Do you trust the contents of this project? Antigravity CLI requires permission",
            "Eligibility check failed: Your current account is not eligible for Antigravity",
        )
        for refusal in refusals:
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    workspace_path=root,
                    result_path=root / "result.md",
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                tool = {
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "step_index": 1,
                        "state": "done",
                        "tool_name": "read_file",
                        "tool_info": {"output": refusal},
                    },
                }
                proc = SimpleNamespace(
                    pid=123,
                    exitstatus=0,
                    isalive=lambda: False,
                    read=Mock(
                        side_effect=[json.dumps(tool) + "\n" + _stream(FINAL_RESPONSE), EOFError()]
                    ),
                )
                with patch.object(
                    pty_runner,
                    "_load_pty_process",
                    return_value=SimpleNamespace(spawn=Mock(return_value=proc)),
                ):
                    result = PtyAgyRunner().run(
                        spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                    )
                self.assertTrue(result.completed)
                self.assertNotIn(refusal, spec.log_path.read_text())

    def test_streaming_plain_cli_refusals_remain_blocked(self) -> None:
        for refusal in (
            "Do you trust the contents of this project? Antigravity CLI requires permission",
            "Eligibility check failed: Your current account is not eligible for Antigravity",
        ):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    workspace_path=root,
                    result_path=root / "result.md",
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                proc = SimpleNamespace(
                    pid=123,
                    exitstatus=0,
                    isalive=lambda: False,
                    read=Mock(side_effect=[refusal, EOFError()]),
                )
                with patch.object(
                    pty_runner,
                    "_load_pty_process",
                    return_value=SimpleNamespace(spawn=Mock(return_value=proc)),
                ):
                    result = PtyAgyRunner().run(
                        spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                    )
                self.assertEqual(result.status, "blocked")
                self.assertFalse(spec.result_path.exists())

    def test_durable_writable_completion_closes_capture_without_fabricating_stream_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = replace(
                _spec(prompt="Write result.", yolo=False),
                workspace_path=root,
                result_path=root / "result.md",
                log_path=root / "attempt.log",
            )
            proc = _FakeProcess()
            proc.read = Mock(return_value="")
            with (
                patch.object(
                    pty_runner,
                    "_load_pty_process",
                    return_value=SimpleNamespace(spawn=Mock(return_value=proc)),
                ),
                patch.object(
                    pty_runner,
                    "inspect_result",
                    return_value=SimpleNamespace(done=True, status="completed"),
                ),
            ):
                result = PtyAgyRunner().run(
                    spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                )
            self.assertTrue(result.completed)
            activity = read_agy_activity(spec.log_path)
            self.assertEqual(activity["stream_state"], "closed")
            self.assertEqual(
                activity["current_action"], "Controller stopped AGY after durable result"
            )
            self.assertIsNone(activity["conversation_id"])

    def test_exit_waits_for_split_final_reader_chunks(self) -> None:
        release_reader = threading.Event()

        class ExitingProcess:
            pid = 123
            exitstatus = 0

            def __init__(self) -> None:
                self.reads = 0

            def read(self, size: int) -> str:
                self.reads += 1
                if self.reads == 1:
                    return _stream(FINAL_RESPONSE)[:20]
                if self.reads == 2:
                    if not release_reader.wait(2):
                        raise RuntimeError("test reader was not released")
                    return _stream(FINAL_RESPONSE)[20:40]
                if self.reads == 3:
                    return _stream(FINAL_RESPONSE)[40:]
                raise EOFError("EOF")

            def isalive(self) -> bool:
                release_reader.set()
                return False

            def terminate(self, force: bool = False) -> None:
                raise AssertionError("dead read-only process must not be terminated")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = replace(
                _spec(prompt="Inspect only.", yolo=False),
                workspace_path=root,
                result_path=root / "result.md",
                log_path=root / "attempt.log",
                read_only=True,
            )
            with patch.object(
                pty_runner,
                "_load_pty_process",
                return_value=SimpleNamespace(spawn=lambda *args, **kwargs: ExitingProcess()),
            ):
                result = PtyAgyRunner().run(
                    spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                )
            self.assertTrue(result.completed)
            self.assertEqual(spec.result_path.read_text(), FINAL_RESPONSE)

    def test_reader_recovers_only_after_clean_eof(self) -> None:
        for ending in (
            EOFError("backend EOF"),
            "",
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            OSError("backend read failed"),
            RuntimeError("backend crashed"),
        ):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                placeholder = "Awaiting `agy`\n"
                result_path = root / "result.md"
                result_path.write_text(placeholder, encoding="utf-8")
                proc = SimpleNamespace(
                    pid=123,
                    exitstatus=0,
                    read=Mock(side_effect=[_stream(FINAL_RESPONSE), ending]),
                    isalive=lambda: False,
                )
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    workspace_path=root,
                    result_path=result_path,
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                with patch.object(
                    pty_runner,
                    "_load_pty_process",
                    return_value=SimpleNamespace(spawn=Mock(return_value=proc)),
                ):
                    result = PtyAgyRunner().run(
                        spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                    )

                clean_eof = isinstance(ending, EOFError) or ending == ""
                self.assertEqual(result.completed, clean_eof)
                self.assertEqual(
                    result.status, "completed" if clean_eof else "exited_without_result"
                )
                self.assertEqual(
                    result_path.read_text(), FINAL_RESPONSE if clean_eof else placeholder
                )
                if not clean_eof:
                    self.assertIn(type(ending).__name__, result.message)
                    self.assertNotIn("backend read failed", spec.log_path.read_text())

    def test_empty_non_eof_poll_preserves_later_response_and_read_failures(self) -> None:
        for ending in (EOFError("closed"), OSError("unexpected socket failure")):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    workspace_path=root,
                    result_path=root / "result.md",
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                proc = SimpleNamespace(
                    pid=123,
                    exitstatus=0,
                    read=Mock(side_effect=["", _stream(FINAL_RESPONSE), ending]),
                    eof=Mock(return_value=False),
                    isalive=lambda: False,
                )
                with patch.object(
                    pty_runner,
                    "_load_pty_process",
                    return_value=SimpleNamespace(spawn=Mock(return_value=proc)),
                ):
                    result = PtyAgyRunner().run(
                        spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                    )
                self.assertEqual(proc.read.call_count, 3)
                self.assertEqual(result.completed, isinstance(ending, EOFError))
                self.assertEqual(spec.result_path.exists(), isinstance(ending, EOFError))
                if result.completed:
                    self.assertEqual(spec.result_path.read_text(), FINAL_RESPONSE)
                else:
                    self.assertIn("OSError", result.message)

    def test_empty_poll_with_eof_or_noncallable_eof_stops(self) -> None:
        for eof in (Mock(return_value=True), False):
            with self.subTest(eof=eof):
                proc = SimpleNamespace(read=Mock(return_value=""), eof=eof)
                output: queue.Queue[str] = queue.Queue()
                self.assertIsNone(PtyAgyRunner._read_output(proc, output))
                self.assertEqual(proc.read.call_count, 1)
                self.assertTrue(output.empty())

    def test_eof_check_errors_after_valid_prefix_prevent_recovery(self) -> None:
        for error in (
            EOFError("invalid check"),
            OSError("check failed"),
            RuntimeError("bad check"),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    result_path=root / "result.md",
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                proc = SimpleNamespace(
                    isalive=lambda: False,
                    exitstatus=0,
                    read=Mock(side_effect=[_stream(FINAL_RESPONSE), ""]),
                    eof=Mock(side_effect=error),
                )
                output: queue.Queue[str] = queue.Queue()
                done = threading.Event()
                errors: queue.Queue[str] = queue.Queue()
                PtyAgyRunner._reader(proc, output, done, errors)
                capture = pty_runner.AgyOutput(spec.log_path, "job")
                result = PtyAgyRunner()._exited_result_if_dead(
                    proc,
                    output,
                    io.StringIO(),
                    spec,
                    0,
                    reader_done=done,
                    reader_errors=errors,
                    capture=capture,
                )
                assert result is not None
                self.assertEqual(result.status, "exited_without_result")
                self.assertIn(type(error).__name__, result.message)
                self.assertFalse(spec.result_path.exists())

    def test_empty_non_eof_poll_wait_can_be_interrupted(self) -> None:
        entered = threading.Event()
        stop = threading.Event()
        done = threading.Event()
        errors: queue.Queue[str] = queue.Queue()

        def eof() -> bool:
            entered.set()
            return False

        proc = SimpleNamespace(read=Mock(return_value=""), eof=eof)
        with patch.object(pty_runner, "READER_QUEUE_WAIT_SEC", 10):
            reader = threading.Thread(
                target=PtyAgyRunner._reader, args=(proc, queue.Queue(), done, errors, stop)
            )
            reader.start()
            try:
                self.assertTrue(entered.wait(1))
                stop.set()
                reader.join(1)
                self.assertFalse(reader.is_alive())
                self.assertTrue(done.is_set())
                self.assertEqual(errors.get_nowait(), "PTY reader stopped before clean EOF")
                self.assertEqual(proc.read.call_count, 1)
            finally:
                stop.set()
                reader.join(1)

    def test_native_pty_recovers_print_output_and_rejects_nonzero_or_oversized_stream(self) -> None:
        for prefix, exit_code, expected in (
            ("", 0, "completed"),
            ("", 1, "exited_without_result"),
            ("x" * (pty_runner.FINAL_RESPONSE_LIMIT + 10), 0, "exited_without_result"),
        ):
            with (
                self.subTest(exit_code=exit_code, prefix_length=len(prefix)),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                script = root / "fake_agy.py"
                script.write_text(
                    "import sys\n"
                    f"sys.stdout.write({prefix + _stream(FINAL_RESPONSE)!r})\n"
                    "sys.stdout.flush()\n"
                    f"sys.exit({exit_code})\n",
                    encoding="utf-8",
                )
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    agy_command=str(script),
                    workspace_path=root,
                    result_path=root / "result.md",
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                result = PtyAgyRunner().run(
                    spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                )
                activity = read_agy_activity(spec.log_path)
                capture_state = {
                    key: activity.get(key)
                    for key in ("available", "stream_state", "event_count", "current_state")
                }
                self.assertEqual(
                    result.status,
                    expected,
                    f"status={result.status}, message={result.message!r}, "
                    f"exit_code={result.exit_code}, capture={capture_state}",
                )
                self.assertEqual(spec.result_path.exists(), expected == "completed")
                if expected == "completed":
                    self.assertEqual(spec.result_path.read_text(), FINAL_RESPONSE)

    def test_native_success_without_final_format_explains_missing_result_without_accepting_it(
        self,
    ) -> None:
        # Native 2s print-timeout probe emitted SUCCESS with an empty response at exit zero.
        for status, response, expected in (
            ("SUCCESS", "", "SUCCESS with an empty final response"),
            (
                "SUCCESS",
                "PRIVATE_PROVISIONAL_RESPONSE",
                "SUCCESS without the required final result format",
            ),
            ("PRIVATE_STATUS", FINAL_RESPONSE, "a non-success result status"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                placeholder = "Awaiting `agy`\n"
                result_path = root / "result.md"
                result_path.write_text(placeholder)
                script = root / "fake_agy.py"
                stream = (
                    json.dumps(
                        {"event": "result", "result": {"status": status, "response": response}}
                    )
                    + "\n"
                )
                script.write_text(f"print({stream!r}, end='')\n")
                spec = replace(
                    _spec(prompt="Inspect only.", yolo=False),
                    agy_command=str(script),
                    workspace_path=root,
                    result_path=result_path,
                    log_path=root / "attempt.log",
                    read_only=True,
                )
                result = PtyAgyRunner().run(
                    spec, cancel_requested=lambda: False, pid_observed=lambda pid: None
                )
                self.assertEqual(result.status, "exited_without_result")
                self.assertFalse(result.completed)
                self.assertEqual(result.exit_code, 0)
                self.assertIn(expected, result.message)
                self.assertIn("configured --print-timeout 10s", result.message)
                self.assertNotIn("PRIVATE", result.message)
                self.assertNotIn("PRIVATE", spec.log_path.read_text())
                self.assertEqual(result_path.read_text(), placeholder)

    def test_structured_failure_does_not_override_reader_error_or_invalid_stream(self) -> None:
        for initial_response in ("", "PRIVATE_PROVISIONAL_RESPONSE"):
            for suffix, reader_error in (
                ("", "PTY reader failed: OSError"),
                ("malformed\n", None),
                (_stream(FINAL_RESPONSE), None),
                ('{"event":"result"', None),
            ):
                with (
                    self.subTest(suffix=suffix, reader_error=reader_error),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    root = Path(temp)
                    spec = replace(
                        _spec(prompt="Inspect only.", yolo=False),
                        result_path=root / "result.md",
                        log_path=root / "attempt.log",
                        read_only=True,
                    )
                    output: queue.Queue[str] = queue.Queue()
                    output.put(_stream(initial_response) + suffix)
                    done = threading.Event()
                    done.set()
                    errors: queue.Queue[str] = queue.Queue()
                    if reader_error:
                        errors.put(reader_error)
                    result = PtyAgyRunner()._exited_result_if_dead(
                        SimpleNamespace(isalive=lambda: False, exitstatus=0),
                        output,
                        io.StringIO(),
                        spec,
                        0,
                        reader_done=done,
                        reader_errors=errors,
                        capture=pty_runner.AgyOutput(spec.log_path, "job"),
                    )
                    assert result is not None
                    self.assertEqual(result.status, "exited_without_result")
                    self.assertFalse(result.completed)
                    self.assertNotIn("empty final response", result.message)
                    self.assertNotIn("required final result format", result.message)
                    self.assertNotIn("PRIVATE", result.message)
                    if reader_error:
                        self.assertEqual(result.message, reader_error)
                    else:
                        self.assertIn("single complete valid final result", result.message)
                        self.assertIn("configured --print-timeout 10s", result.message)
                    self.assertFalse(spec.result_path.exists())

    def test_late_trust_or_eligibility_failure_wins_over_envelope(self) -> None:
        for refusal in (
            "Do you trust the contents of this project? Antigravity CLI requires permission",
            "Eligibility check failed: Your current account is not eligible for Antigravity",
            "Eligibility check failed: Your current account is \x1b[31mnot eligible for Antigravity",
        ):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result = self._recover(root, FINAL_RESPONSE + refusal)
                self.assertEqual(result.status, "blocked")
                self.assertFalse((root / "result.md").exists())


def _stream(response: str) -> str:
    return (
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": response}})
        + "\n"
    )


class _FakeProcess:
    pid: int | None = 123
    exitstatus: int | None = None

    def __init__(self) -> None:
        self.terminated = False

    def read(self, size: int) -> str:
        del size
        return ""

    def isalive(self) -> bool:
        return not self.terminated

    def terminate(self, force: bool = False) -> None:
        self.terminated = force


def _spec(*, prompt: str, yolo: bool, agy_model: str | None = None) -> AgyRunSpec:
    return AgyRunSpec(
        backend="agy",
        agy_command="agy",
        codex_command="codex",
        codex_model="gpt-5",
        codex_reasoning_effort="low",
        codex_sandbox_mode="workspace-write",
        codex_disabled_mcp_servers=(),
        prompt=prompt,
        workspace_path=Path("D:/repo/workspace"),
        result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
        log_path=Path("D:/repo/runs/job-1/attempt-001.log"),
        print_timeout="10s",
        timeout_sec=30,
        idle_timeout_sec=10,
        yolo=yolo,
        read_only=False,
        agy_model=agy_model,
    )


if __name__ == "__main__":
    unittest.main()
