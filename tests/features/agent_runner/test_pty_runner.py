from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunSpec, PtyAgyRunner


class PtyRunnerCommandTest(unittest.TestCase):
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
