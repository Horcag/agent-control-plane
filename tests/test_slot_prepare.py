from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.slot_lifecycle.lib.slot_prepare import prepare_workspace_slot
from agent_control_plane.shared.config import SlotPrepareCommand


class SlotPrepareTest(unittest.TestCase):
    def test_prepare_runs_missing_marker_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = _git_repo(Path(temp) / "slot")
            (root / "frontend").mkdir()
            command = SlotPrepareCommand(
                name="frontend_node_modules",
                working_dir=Path("frontend"),
                marker=Path("frontend/node_modules"),
                command=(
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('node_modules').mkdir()",
                ),
                timeout_sec=10,
                routes=(),
            )

            first = prepare_workspace_slot(
                slot_path=root,
                commands=(command,),
                forbidden_status_globs=("uv.lock",),
            )
            second = prepare_workspace_slot(
                slot_path=root,
                commands=(command,),
                forbidden_status_globs=("uv.lock",),
            )

            self.assertEqual(first[0]["status"], "ran")
            self.assertEqual(second[0]["status"], "skipped")
            self.assertTrue((root / "frontend" / "node_modules").is_dir())


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _run(["git", "init"], path)
    _run(["git", "config", "user.email", "test@example.local"], path)
    _run(["git", "config", "user.name", "Test User"], path)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "initial"], path)
    return path


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


if __name__ == "__main__":
    unittest.main()
