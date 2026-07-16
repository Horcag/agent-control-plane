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
agy_model = "Gemini 3.5 Flash (Medium)"
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
codex_quality_tier = "deep"
codex_mechanical_model = "gpt-5.6-luna"
codex_mechanical_reasoning_effort = "low"
codex_balanced_model = "gpt-5.6-terra"
codex_balanced_reasoning_effort = "medium"
codex_deep_model = "gpt-5.6-terra"
codex_deep_reasoning_effort = "medium"
codex_global_quota_database = "global/quota.sqlite3"
codex_global_max_concurrent_jobs = 2
codex_five_hour_soft_limit_percent = 75
codex_quota_poll_sec = 30
codex_sessions_root = "sessions"
terminal_slot_policy = "checkpoint"

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

[routes.audit]
path = "other-repo"
required_branch = "main"

[routes.reports]
path = "reports"
required_branch = "main"
worktree_base = "reports"
backend = "codex-spark"
codex_reasoning_effort = "medium"
source_roots = [".", "backend/src", "frontend", "frontend/src", "scripts"]
ide_sdk_name = "Python 3.12 (.venv)"
ide_mcp_server = "reports_agentbridge_idea"
agy_mcp_server = "agentbridge-ide"
agy_model = "Gemini 3.5 Flash (High)"
ide_mcp_project_root = "ide-project"
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
            self.assertEqual(config.defaults.agy_model, "Gemini 3.5 Flash (Medium)")
            self.assertEqual(config.defaults.codex_model, "gpt-5")
            self.assertEqual(config.defaults.codex_reasoning_effort, "low")
            self.assertEqual(config.defaults.codex_sandbox_mode, "workspace-write")
            self.assertEqual(config.defaults.codex_disabled_mcp_servers, ())
            self.assertEqual(config.defaults.codex_forbidden_tool_markers, ())
            self.assertEqual(config.defaults.codex_no_progress_timeout_sec, 240)
            self.assertEqual(config.defaults.codex_quality_tier, "deep")
            self.assertEqual(config.defaults.codex_mechanical_model, "gpt-5.6-luna")
            self.assertEqual(config.defaults.codex_mechanical_reasoning_effort, "low")
            self.assertEqual(config.defaults.codex_balanced_model, "gpt-5.6-terra")
            self.assertEqual(config.defaults.codex_balanced_reasoning_effort, "medium")
            self.assertEqual(config.defaults.codex_deep_model, "gpt-5.6-terra")
            self.assertEqual(config.defaults.codex_deep_reasoning_effort, "medium")
            self.assertEqual(
                config.defaults.codex_global_quota_database,
                (root / "global" / "quota.sqlite3").resolve(strict=False),
            )
            self.assertEqual(config.defaults.codex_global_max_concurrent_jobs, 2)
            self.assertEqual(config.defaults.codex_global_max_burst_jobs, 8)
            self.assertEqual(config.defaults.codex_five_hour_soft_limit_percent, 75.0)
            self.assertEqual(config.defaults.codex_quota_poll_sec, 30.0)
            self.assertEqual(
                config.defaults.codex_sessions_root,
                (root / "sessions").resolve(strict=False),
            )
            self.assertEqual(config.defaults.terminal_slot_policy, "checkpoint")
            self.assertEqual(config.defaults.native_quality_policy, "worker")
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
                config.routes["audit"].worktree_base,
                (root / "other-repo").resolve(strict=False),
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
                config.routes["reports"].ide_mcp_server,
                "reports_agentbridge_idea",
            )
            self.assertEqual(config.routes["reports"].agy_mcp_server, "agentbridge-ide")
            self.assertEqual(config.routes["reports"].agy_model, "Gemini 3.5 Flash (High)")
            self.assertEqual(
                config.routes["reports"].ide_mcp_project_root,
                (root / "ide-project").resolve(strict=False),
            )
            self.assertEqual(
                tuple(path.as_posix() for path in config.routes["reports"].test_roots),
                ("backend/tests", "frontend/tests"),
            )
            self.assertEqual(
                tuple(path.as_posix() for path in config.routes["reports"].exclude_dirs),
                ("dist", "frontend/build"),
            )

    def test_native_quality_contract_config(self) -> None:
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

[control.defaults]
native_quality_policy = "off"

[routes.main]
path = "repo"
required_branch = "main"
native_quality_policy = "controller"

[[routes.main.native_quality_gates]]
name = "affected-tests"
command = ["python", "scripts/run_affected_tests.py", "--worktree"]
working_dir = "."
timeout_sec = 300

[[routes.main.native_quality_gates]]
name = "ruff"
command = ["python", "-m", "ruff", "check", "src", "tests"]
include_globs = ["*.py", "**/*.py", "pyproject.toml"]
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            route = config.routes["main"]
            self.assertEqual(config.defaults.native_quality_policy, "off")
            self.assertEqual(route.native_quality_policy, "controller")
            self.assertEqual(
                [gate.name for gate in route.native_quality_gates],
                ["affected-tests", "ruff"],
            )
            self.assertEqual(route.native_quality_gates[0].command[-1], "--worktree")
            self.assertEqual(route.native_quality_gates[0].working_dir, Path("."))
            self.assertEqual(route.native_quality_gates[0].timeout_sec, 300)
            self.assertEqual(
                route.native_quality_gates[1].include_globs,
                ("*.py", "**/*.py", "pyproject.toml"),
            )

    def test_native_quality_contract_rejects_unsafe_or_ambiguous_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "workspaces.toml"
            base = """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[routes.main]
path = "repo"
required_branch = "main"
native_quality_policy = "controller"
"""
            invalid_cases = (
                (
                    "unknown policy",
                    base.replace(
                        'native_quality_policy = "controller"',
                        'native_quality_policy = "magic"',
                    ),
                    "native_quality_policy",
                ),
                (
                    "missing gates",
                    base,
                    "requires at least one native_quality_gate",
                ),
                (
                    "escaping cwd",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "escape"
command = ["python", "-m", "pytest"]
working_dir = "../other"
""",
                    "working_dir must stay inside",
                ),
                (
                    "duplicate names",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "tests"
command = ["python", "-m", "pytest"]
[[routes.main.native_quality_gates]]
name = "tests"
command = ["python", "-m", "ruff", "check", "."]
""",
                    "duplicate native quality gate",
                ),
                (
                    "dependency install",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "install"
command = ["uv", "sync", "--frozen"]
""",
                    "must be a read-only quality check",
                ),
                (
                    "mutating formatter",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "format"
command = ["python", "-m", "ruff", "format", "src"]
""",
                    "must be a read-only quality check",
                ),
            )
            for label, payload, message in invalid_cases:
                with self.subTest(label=label):
                    config_path.write_text(payload, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(config_path)

    def test_workspace_access_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config" / "workspaces.toml"
            config_path.parent.mkdir(parents=True)

            base_toml = """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"
agy_command = "agy"
"""

            # Case 1: default workspace_access is ide_mcp (compatibility default)
            config_path.write_text(
                base_toml
                + """
[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.defaults.workspace_access, "ide_mcp")
            self.assertIsNone(config.routes["main"].workspace_access)

            # Case 2: valid global "native" and route override
            config_path.write_text(
                base_toml
                + """
[control.defaults]
workspace_access = "native"
[routes.main]
path = "repo"
required_branch = "main"
workspace_access = "ide_mcp"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.defaults.workspace_access, "native")
            self.assertEqual(config.routes["main"].workspace_access, "ide_mcp")

            # Case 3: invalid global value
            config_path.write_text(
                base_toml
                + """
[control.defaults]
workspace_access = "invalid"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "workspace_access must be either 'ide_mcp' or 'native'"
            ):
                load_config(config_path)

            # Case 4: invalid route value
            config_path.write_text(
                base_toml
                + """
[routes.main]
path = "repo"
required_branch = "main"
workspace_access = "invalid"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "workspace_access must be either 'ide_mcp' or 'native'"
            ):
                load_config(config_path)

    def test_terminal_slot_policy_rejects_unknown_value(self) -> None:
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

[control.defaults]
terminal_slot_policy = "delete"

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "terminal_slot_policy must be either 'preserve' or 'checkpoint'",
            ):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
