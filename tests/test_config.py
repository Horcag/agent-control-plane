from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.shared.config import load_config


class ConfigTest(unittest.TestCase):
    def test_loads_slot_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config" / "workspaces.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"
agy_command = "agy"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"
max_restarts = 0
yolo = false
allow_dirty = false
guardrail_poll_sec = 2
forbidden_status_globs = ["uv.lock"]
prepare_slots = true
runs_layout = "date"
auto_archive_days = 7
auto_archive_limit = 200

[slot_prepare.frontend_node_modules]
routes = ["main", "dev"]
working_dir = "frontend"
marker = "frontend/node_modules"
command = ["bun", "install", "--frozen-lockfile"]
timeout_sec = 1200

[routes.main]
path = "repo"
required_branch = "main"
codex_forbidden_tool_markers = ["raw_exec", "web_search"]
monitor_route_root = false

[routes.reports]
path = "reports"
required_branch = "main"
worktree_base = "reports"
backend = "codex-spark"
codex_reasoning_effort = "medium"
source_roots = [".", "backend/src", "frontend", "frontend/src", "scripts"]
ide_sdk_name = "Python 3.12 (.venv)"
test_roots = ["backend/tests", "frontend/tests"]
exclude_dirs = ["dist", "frontend/build"]

[slots."main-1"]
route = "main"
path = "slots/main-1"

[slots."reports-1"]
route = "reports"
path = "slots/reports-1"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.slot_root, (root / "slots").resolve(strict=False))
            self.assertEqual(config.worktree_base, (root / "repo").resolve(strict=False))
            self.assertEqual(config.slots["main-1"].route, "main")
            self.assertEqual(
                config.slots["main-1"].path,
                (root / "slots" / "main-1").resolve(strict=False),
            )
            self.assertTrue(config.defaults.prepare_slots)
            self.assertEqual(config.codex_command, "codex")
            self.assertEqual(config.defaults.backend, "codex")
            self.assertEqual(config.defaults.codex_model, "gpt-5")
            self.assertEqual(config.defaults.codex_reasoning_effort, "low")
            self.assertEqual(config.defaults.codex_sandbox_mode, "workspace-write")
            self.assertEqual(config.defaults.codex_disabled_mcp_servers, ())
            self.assertEqual(config.defaults.codex_forbidden_tool_markers, ())
            self.assertEqual(config.defaults.codex_no_progress_timeout_sec, 240)
            self.assertEqual(config.defaults.runs_layout, "date")
            self.assertEqual(config.defaults.auto_archive_days, 7)
            self.assertEqual(config.defaults.auto_archive_limit, 200)
            self.assertFalse(config.defaults.auto_switch_agy_on_quota)
            self.assertEqual(config.defaults.auto_switch_agy_strategy, "best")
            self.assertEqual(
                config.defaults.auto_switch_agy_electron_command,
                ("cmd", "/c", "npx", "--no-install", "electron"),
            )
            self.assertEqual(len(config.slot_prepare), 1)
            self.assertEqual(config.slot_prepare[0].working_dir.as_posix(), "frontend")
            marker = config.slot_prepare[0].marker
            self.assertIsInstance(marker, Path)
            marker_text = marker.as_posix() if isinstance(marker, Path) else ""
            self.assertEqual(marker_text, "frontend/node_modules")
            self.assertEqual(config.slot_prepare[0].command[0], "bun")
            self.assertEqual(config.slot_prepare[0].routes, ("main", "dev"))
            self.assertEqual(
                config.routes["main"].worktree_base,
                (root / "repo").resolve(strict=False),
            )
            self.assertEqual(
                config.routes["main"].codex_forbidden_tool_markers,
                ("raw_exec", "web_search"),
            )
            self.assertFalse(config.routes["main"].monitor_route_root)
            self.assertIsNone(config.routes["reports"].codex_forbidden_tool_markers)
            self.assertTrue(config.routes["reports"].monitor_route_root)
            self.assertEqual(
                config.routes["reports"].worktree_base,
                (root / "reports").resolve(strict=False),
            )
            self.assertEqual(
                tuple(path.as_posix() for path in config.routes["reports"].source_roots),
                (".", "backend/src", "frontend", "frontend/src", "scripts"),
            )
            self.assertEqual(config.routes["reports"].backend, "codex")
            self.assertEqual(config.routes["reports"].codex_reasoning_effort, "medium")
            self.assertEqual(config.routes["reports"].ide_sdk_name, "Python 3.12 (.venv)")
            self.assertEqual(
                tuple(path.as_posix() for path in config.routes["reports"].test_roots),
                ("backend/tests", "frontend/tests"),
            )
            self.assertEqual(
                tuple(path.as_posix() for path in config.routes["reports"].exclude_dirs),
                ("dist", "frontend/build"),
            )


if __name__ == "__main__":
    unittest.main()
