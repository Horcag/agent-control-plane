from __future__ import annotations

import io
import os
import queue
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib import pty_runner
from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunSpec, PtyAgyRunner


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

    def test_windows_uses_pywinpty(self) -> None:
        winpty = SimpleNamespace(PtyProcess="conpty-backend")

        self.assertEqual(self._load_with("nt", {"winpty": winpty}), "conpty-backend")

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
            "agy --model Gemini 3.5 Flash (High) --print <prompt> --print-timeout 10s",
        )
        self.assertNotIn("secret prompt", display)

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

    def test_idle_timeout_mentions_terminal_and_durable_progress(self) -> None:
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
        self.assertIn("terminal output or durable progress", result.message if result else "")
        self.assertTrue(proc.terminated)


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
                    return "\x1b[3"
                if self.reads == 2:
                    if not release_reader.wait(2):
                        raise RuntimeError("test reader was not released")
                    return "2m" + FINAL_RESPONSE[:30]
                if self.reads == 3:
                    return FINAL_RESPONSE[30:] + "\x1b[0m"
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
                    f"sys.stdout.write({prefix + FINAL_RESPONSE!r})\n"
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
                self.assertEqual(result.status, expected)
                self.assertEqual(spec.result_path.exists(), expected == "completed")
                if expected == "completed":
                    self.assertEqual(spec.result_path.read_text(), FINAL_RESPONSE)

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
