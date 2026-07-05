from __future__ import annotations

import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunSpec, PtyAgyRunner


class PtyRunnerCommandTest(unittest.TestCase):
    def test_build_command_uses_non_interactive_print_mode(self) -> None:
        spec = _spec(prompt="do the task", yolo=True)

        command = PtyAgyRunner._build_command(spec)

        self.assertEqual(
            command,
            [
                "agy",
                "--dangerously-skip-permissions",
                "--print",
                "do the task",
                "--print-timeout",
                "10s",
            ],
        )
        self.assertNotIn("--prompt-interactive", command)

    def test_display_command_redacts_prompt(self) -> None:
        spec = _spec(prompt="secret prompt", yolo=False)

        display = PtyAgyRunner._display_command(spec)

        self.assertEqual(display, "agy --print <prompt> --print-timeout 10s")
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


def _spec(*, prompt: str, yolo: bool) -> AgyRunSpec:
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
    )


if __name__ == "__main__":
    unittest.main()
