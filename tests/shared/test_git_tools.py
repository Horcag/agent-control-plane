from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.shared.git_tools import (
    GIT_TIMEOUT_SEC,
    GitError,
    GitWorkspaceState,
    diff_patch,
    run_git,
    workspace_snapshot,
)


class GitToolsTest(unittest.TestCase):
    def test_run_git_decodes_output_as_utf8_with_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            completed = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="файл.txt\n",
                stderr="",
            )

            with patch(
                "agent_control_plane.shared.git_tools.subprocess.run", return_value=completed
            ) as run:
                output = run_git(repo, "status", "--porcelain=v1")

            self.assertEqual(output, "файл.txt")
            run.assert_called_once_with(
                ["git", "-C", str(repo), "status", "--porcelain=v1"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=GIT_TIMEOUT_SEC,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

    def test_run_git_wraps_timeout_with_repository_and_command(self) -> None:
        repo = Path("repo")
        timeout = subprocess.TimeoutExpired(["git", "status"], GIT_TIMEOUT_SEC)

        with (
            patch("agent_control_plane.shared.git_tools.subprocess.run", side_effect=timeout),
            self.assertRaisesRegex(GitError, r"repo.*git.*status") as error,
        ):
            run_git(repo, "status", "--porcelain=v1")

        self.assertIs(error.exception.__cause__, timeout)

    def test_workspace_snapshot_marks_head_race_as_unstable(self) -> None:
        state = GitWorkspaceState(branch="main", porcelain=" M tracked.py")

        with (
            patch(
                "agent_control_plane.shared.git_tools.head_commit",
                side_effect=["head-a", "head-b"],
            ),
            patch(
                "agent_control_plane.shared.git_tools.workspace_state",
                return_value=state,
            ),
        ):
            snapshot = workspace_snapshot(Path("repo"))

        self.assertFalse(snapshot.stable)
        self.assertEqual(snapshot.head, "head-b")
        self.assertEqual(snapshot.porcelain, state.porcelain)

    def test_diff_patch_includes_cached_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            _run(["git", "init"], repo)
            tracked = repo / "tracked.py"
            tracked.write_text("before\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], repo)
            _commit(repo, "seed")

            tracked.write_text("after\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], repo)

            patch_text = diff_patch(repo)

        self.assertIn("-before", patch_text)
        self.assertIn("+after", patch_text)


def _commit(repo: Path, message: str) -> None:
    _run(
        [
            "git",
            "-c",
            "user.name=ACP Test",
            "-c",
            "user.email=acp@example.test",
            "commit",
            "-m",
            message,
        ],
        repo,
    )


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


if __name__ == "__main__":
    unittest.main()
