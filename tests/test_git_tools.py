from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.shared.git_tools import run_git


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
            )


if __name__ == "__main__":
    unittest.main()
