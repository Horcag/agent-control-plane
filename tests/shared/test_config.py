from __future__ import annotations

import contextlib
import inspect
import json
import os
import tempfile
import unittest
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.app.runtime.cli import _build_parser
from agent_control_plane.shared.config import (
    CodexModelMetadataConfig,
    default_config_path,
    ensure_mcp_server,
    find_enclosing_git_repo,
    load_config,
    port_for,
    probe_mcp_health,
    probe_mcp_health_detailed,
    register_known_config,
    resolve_config_for,
    wire_mcp_servers,
)


class ConfigTest(unittest.TestCase):
    def test_loads_example_config_cheap_first_policy(self) -> None:
        config = load_config(
            Path(__file__).resolve().parents[2] / "config" / "workspaces.example.toml"
        )

        self.assertEqual(config.defaults.codex_quality_tier, "cheap-first")
        policy = next(policy for policy in config.routing_policies if policy.name == "cheap-first")
        self.assertEqual(
            (config.defaults.codex_model, config.defaults.codex_reasoning_effort),
            (policy.candidates[0].model, policy.candidates[0].reasoning_effort),
        )
        self.assertEqual(
            [(candidate.model, candidate.reasoning_effort) for candidate in policy.candidates],
            [
                ("gpt-5.6-luna", "low"),
                ("gpt-5.6-terra", "medium"),
                ("gpt-5.6-sol", "medium"),
            ],
        )
        metadata = {model.model: model for model in config.model_catalog.models}
        self.assertTrue(metadata["gpt-5.6-sol"].premium)
        self.assertFalse(metadata["gpt-5.6-luna"].premium)
        self.assertFalse(metadata["gpt-5.6-terra"].premium)

    def test_direct_model_metadata_defaults_premium_to_false(self) -> None:
        metadata = CodexModelMetadataConfig(
            model="legacy-codex",
            quota_domain=None,
            capacity_units=(),
            credit_rate=None,
            api_usd_rate=None,
            rate_card_version=None,
            rate_card_source=None,
        )

        self.assertFalse(metadata.premium)

    def test_loads_claude_mcp_servers_and_config_path(self) -> None:
        config = load_config(
            config_contents=(
                b"[control]\n"
                b'coordination_root = ".agent-work"\n'
                b'runs_root = "runs"\n'
                b'database = "runs/jobs.sqlite3"\n'
                b'worktree_root = "worktrees"\n'
                b'worktree_base = "repo"\n'
                b'slot_root = "slots"\n'
                b'claude_config_path = "~/custom-claude.json"\n'
                b"[control.claude_mcp_servers.agentbridge_idea_64343]\n"
                b'type = "http"\n'
                b'url = "http://127.0.0.1:64343/mcp"\n'
                b"[control.defaults]\n"
                b"timeout_sec = 10\n"
                b"idle_timeout_sec = 5\n"
                b'print_timeout = "10s"\n'
                b"[routes.main]\n"
                b'path = "repo"\n'
                b'required_branch = "main"\n'
            )
        )

        self.assertEqual(
            dict(config.claude_mcp_servers["agentbridge_idea_64343"]),
            {"type": "http", "url": "http://127.0.0.1:64343/mcp"},
        )
        self.assertEqual(
            config.claude_config_path,
            Path("~/custom-claude.json").expanduser(),
        )

    def test_dirty_diff_ceiling_defaults_and_overrides(self) -> None:
        base = (
            b"[control]\n"
            b'coordination_root = ".agent-work"\n'
            b'runs_root = "runs"\n'
            b'database = "runs/jobs.sqlite3"\n'
            b'worktree_root = "worktrees"\n'
            b'worktree_base = "repo"\n'
            b'slot_root = "slots"\n'
        )
        route = b'[routes.main]\npath = "repo"\nrequired_branch = "main"\n'

        config = load_config(config_contents=base + route)
        self.assertEqual(config.defaults.dirty_diff_max_changed_lines, 8000)
        self.assertIsNone(config.routes["main"].dirty_diff_max_changed_lines)

        config = load_config(
            config_contents=(
                base
                + b"[control.defaults]\n"
                + b"dirty_diff_max_changed_lines = 0\n"
                + route
                + b"dirty_diff_max_changed_lines = 2500\n"
            )
        )
        self.assertEqual(config.defaults.dirty_diff_max_changed_lines, 0)
        self.assertEqual(config.routes["main"].dirty_diff_max_changed_lines, 2500)

        with self.assertRaises(ValueError):
            load_config(config_contents=(base + route + b"dirty_diff_max_changed_lines = -1\n"))

    def test_route_root_ignore_globs_default_empty(self) -> None:
        base = (
            b"[control]\n"
            b'coordination_root = ".agent-work"\n'
            b'runs_root = "runs"\n'
            b'database = "runs/jobs.sqlite3"\n'
            b'worktree_root = "worktrees"\n'
            b'worktree_base = "repo"\n'
            b'slot_root = "slots"\n'
        )
        route = b'[routes.main]\npath = "repo"\nrequired_branch = "main"\n'

        config = load_config(config_contents=base + route)

        self.assertEqual(config.routes["main"].route_root_ignore_globs, ())

    def test_route_root_ignore_globs_parses_configured_list(self) -> None:
        base = (
            b"[control]\n"
            b'coordination_root = ".agent-work"\n'
            b'runs_root = "runs"\n'
            b'database = "runs/jobs.sqlite3"\n'
            b'worktree_root = "worktrees"\n'
            b'worktree_base = "repo"\n'
            b'slot_root = "slots"\n'
            b'[routes.main]\npath = "repo"\nrequired_branch = "main"\n'
            b'route_root_ignore_globs = ["kanban/**"]\n'
        )

        config = load_config(config_contents=base)

        self.assertEqual(config.routes["main"].route_root_ignore_globs, ("kanban/**",))

    def test_route_root_ignore_globs_rejects_whole_tree_pattern(self) -> None:
        base = (
            b"[control]\n"
            b'coordination_root = ".agent-work"\n'
            b'runs_root = "runs"\n'
            b'database = "runs/jobs.sqlite3"\n'
            b'worktree_root = "worktrees"\n'
            b'worktree_base = "repo"\n'
            b'slot_root = "slots"\n'
            b'[routes.main]\npath = "repo"\nrequired_branch = "main"\n'
            b'route_root_ignore_globs = ["**"]\n'
        )

        with self.assertRaises(ValueError):
            load_config(config_contents=base)

    def test_route_root_ignore_globs_rejects_every_blanket_spelling(self) -> None:
        """Pins where the blanket-pattern heuristic starts and stops.

        Rejected: patterns matching every path, or every path below the root --
        those spare at most the handful of files sitting directly in the root, so
        the guard is off in practice however the pattern is spelled.

        Accepted: anything narrower, including deliberately broad patterns like
        `src/**` or `**/**/*` (which needs two separators and so spares the root
        and its immediate children). The check exists to stop an accidental total
        silence, not to judge how wide a deliberate exclusion is -- an operator
        can always write a broad-but-valid pattern, and refusing those would just
        push people to disable `monitor_route_root` wholesale instead.
        """
        base = (
            b"[control]\n"
            b'coordination_root = ".agent-work"\n'
            b'runs_root = "runs"\n'
            b'database = "runs/jobs.sqlite3"\n'
            b'worktree_root = "worktrees"\n'
            b'worktree_base = "repo"\n'
            b'slot_root = "slots"\n'
            b'[routes.main]\npath = "repo"\nrequired_branch = "main"\n'
        )

        for pattern in (b"**", b"**/**", b"**/*"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                load_config(
                    config_contents=base + b"route_root_ignore_globs = [" + b'"' + pattern + b'"]\n'
                )

        for pattern in (b"kanban/**", b"docs/*.md", b"**/*.generated.py", b"*"):
            with self.subTest(pattern=pattern):
                config = load_config(
                    config_contents=base + b"route_root_ignore_globs = [" + b'"' + pattern + b'"]\n'
                )
                self.assertEqual(
                    config.routes["main"].route_root_ignore_globs,
                    (pattern.decode(),),
                )

    def test_route_slot_root_overrides_global_slot_root(self) -> None:
        base = (
            b"[control]\n"
            b'coordination_root = ".agent-work"\n'
            b'runs_root = "runs"\n'
            b'database = "runs/jobs.sqlite3"\n'
            b'worktree_root = "worktrees"\n'
            b'worktree_base = "repo"\n'
            b'slot_root = "slots"\n'
            b'[routes.main]\npath = "repo"\nrequired_branch = "main"\n'
        )

        config = load_config(config_contents=base)
        self.assertIsNone(config.routes["main"].slot_root)
        self.assertEqual(config.slot_root_for("main"), config.slot_root)
        self.assertEqual(config.slot_root_for(None), config.slot_root)
        self.assertEqual(config.slot_root_for("unknown"), config.slot_root)

        config = load_config(config_contents=base + b'slot_root = "main-slots"\n')
        expected = config.project_root / "main-slots"
        self.assertEqual(config.routes["main"].slot_root, expected)
        self.assertEqual(config.slot_root_for("main"), expected)
        self.assertNotEqual(config.slot_root_for("main"), config.slot_root)

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
codex_spark_max_concurrent_jobs = 8
codex_five_hour_soft_limit_percent = 75
codex_spark_soft_limit_percent = 88
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
            self.assertEqual(config.defaults.codex_model, "default")
            self.assertEqual(config.defaults.codex_reasoning_effort, "low")
            self.assertEqual(config.defaults.codex_sandbox_mode, "workspace-write")
            self.assertEqual(config.defaults.codex_disabled_mcp_servers, ())
            self.assertEqual(config.defaults.codex_forbidden_tool_markers, ())
            self.assertEqual(config.defaults.no_progress_timeout_sec, 240)
            self.assertEqual(config.defaults.tool_timeout_limit, 6)
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
            self.assertEqual(config.defaults.codex_spark_max_concurrent_jobs, 8)
            self.assertEqual(config.defaults.codex_spark_models, ())
            self.assertEqual(config.defaults.codex_five_hour_soft_limit_percent, 75.0)
            self.assertEqual(config.defaults.codex_spark_soft_limit_percent, 88.0)
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

    def test_loads_claude_backend_config(self) -> None:
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
claude_command = "claude-nightly"

[control.defaults]
claude_model = "claude-sonnet-5"
claude_reasoning_effort = "xhigh"
claude_permission_mode = "dontAsk"
claude_allowed_tools = ["Read", "Bash"]
claude_sessions_root = "claude-sessions"
claude_max_turns = 40
claude_bare = false

[[control.claude_model_catalog.models]]
model = "claude-opus-5"
premium = true
rate_card_version = "2026-07"
rate_card_source = "operator"

[control.claude_model_catalog.models.api_usd_rate]
input = 5.0
cached_input = 0.5
output = 25.0

[[control.claude_model_catalog.inventory]]
model = "claude-nova-7"
priority = 0
default_reasoning_effort = "high"
supported_reasoning_efforts = ["low", "high"]

[routes.main]
path = "repo"
required_branch = "main"
backend = "claude-code"
claude_model = "claude-haiku-4-5"
claude_reasoning_effort = "low"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.claude_command, "claude-nightly")
            self.assertEqual(config.defaults.claude_model, "claude-sonnet-5")
            self.assertEqual(config.defaults.claude_reasoning_effort, "xhigh")
            self.assertEqual(config.defaults.claude_permission_mode, "dontAsk")
            self.assertEqual(config.defaults.claude_allowed_tools, ("Read", "Bash"))
            self.assertEqual(
                config.defaults.claude_sessions_root,
                (root / "claude-sessions").resolve(strict=False),
            )
            self.assertEqual(config.defaults.claude_max_turns, 40)
            self.assertFalse(config.defaults.claude_bare)
            metadata = {model.model: model for model in config.claude_model_catalog.models}
            rate = metadata["claude-opus-5"].api_usd_rate
            self.assertIsNotNone(rate)
            assert rate is not None
            self.assertEqual(rate.input, 5.0)

            inventory = config.claude_model_catalog.inventory[0]
            self.assertEqual(inventory.model, "claude-nova-7")
            self.assertEqual(inventory.supported_reasoning_efforts, ("low", "high"))
            self.assertEqual(config.routes["main"].backend, "claude")
            self.assertEqual(config.routes["main"].claude_model, "claude-haiku-4-5")
            self.assertEqual(config.routes["main"].claude_reasoning_effort, "low")

    def test_claude_defaults_are_safe_when_unconfigured(self) -> None:
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

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.claude_command, "claude")
            self.assertEqual(config.defaults.claude_model, "default")
            self.assertEqual(config.defaults.claude_reasoning_effort, "medium")
            self.assertEqual(config.defaults.claude_permission_mode, "acceptEdits")
            self.assertEqual(
                config.defaults.claude_allowed_tools,
                ("Read", "Edit", "Write", "Glob", "Grep", "Bash"),
            )
            self.assertIsNone(config.defaults.claude_sessions_root)
            self.assertEqual(config.defaults.claude_max_turns, 0)
            self.assertTrue(config.defaults.claude_bare)
            self.assertEqual(config.claude_model_catalog.models, ())
            self.assertEqual(config.claude_model_catalog.inventory, ())

    def test_tool_timeout_limit_parses_explicit_values(self) -> None:
        for raw_value, expected in ((0, 0), (3, 3)):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config_path = root / "config" / "workspaces.toml"
                config_path.parent.mkdir(parents=True)
                config_path.write_text(
                    f"""
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[control.defaults]
tool_timeout_limit = {raw_value}

[routes.main]
path = "repo"
required_branch = "main"
""",
                    encoding="utf-8",
                )

                config = load_config(config_path)

                self.assertEqual(config.defaults.tool_timeout_limit, expected)

    def test_tool_timeout_limit_rejects_negative_value(self) -> None:
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
tool_timeout_limit = -1

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "tool_timeout_limit"):
                load_config(config_path)

    def test_codex_tool_timeout_limit_alias_still_parses(self) -> None:
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
codex_tool_timeout_limit = 9

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.tool_timeout_limit, 9)

    def test_tool_timeout_limit_new_key_wins_over_alias(self) -> None:
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
tool_timeout_limit = 4
codex_tool_timeout_limit = 9

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.tool_timeout_limit, 4)

    def test_tool_call_budget_grace_sec_default_and_explicit_values(self) -> None:
        for raw_value, expected in ((None, 120), (0, 0), (30, 30)):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config_path = root / "config" / "workspaces.toml"
                config_path.parent.mkdir(parents=True)
                override = (
                    "" if raw_value is None else f"tool_call_budget_grace_sec = {raw_value}\n"
                )
                config_path.write_text(
                    f"""
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[control.defaults]
{override}
[routes.main]
path = "repo"
required_branch = "main"
""",
                    encoding="utf-8",
                )

                config = load_config(config_path)

                self.assertEqual(config.defaults.tool_call_budget_grace_sec, expected)

    def test_tool_call_budget_grace_sec_rejects_negative_value(self) -> None:
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
tool_call_budget_grace_sec = -1

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "tool_call_budget_grace_sec"):
                load_config(config_path)

    def test_codex_tool_call_budget_grace_sec_alias_still_parses(self) -> None:
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
codex_tool_call_budget_grace_sec = 45

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.tool_call_budget_grace_sec, 45)

    def test_tool_call_budget_grace_sec_new_key_wins_over_alias(self) -> None:
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
tool_call_budget_grace_sec = 10
codex_tool_call_budget_grace_sec = 45

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.tool_call_budget_grace_sec, 10)

    def test_invalid_verification_grace_sec_default_and_explicit_values(self) -> None:
        for raw_value, expected in ((None, 120), (0, 0), (30, 30)):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config_path = root / "config" / "workspaces.toml"
                config_path.parent.mkdir(parents=True)
                override = (
                    "" if raw_value is None else f"invalid_verification_grace_sec = {raw_value}\n"
                )
                config_path.write_text(
                    f"""
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[control.defaults]
{override}
[routes.main]
path = "repo"
required_branch = "main"
""",
                    encoding="utf-8",
                )

                config = load_config(config_path)

                self.assertEqual(config.defaults.invalid_verification_grace_sec, expected)

    def test_invalid_verification_grace_sec_rejects_negative_value(self) -> None:
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
invalid_verification_grace_sec = -1

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid_verification_grace_sec"):
                load_config(config_path)

    def test_codex_invalid_verification_grace_sec_alias_still_parses(self) -> None:
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
codex_invalid_verification_grace_sec = 77

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.invalid_verification_grace_sec, 77)

    def test_invalid_verification_grace_sec_new_key_wins_over_alias(self) -> None:
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
invalid_verification_grace_sec = 15
codex_invalid_verification_grace_sec = 77

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.invalid_verification_grace_sec, 15)

    def test_no_progress_timeout_sec_alias_still_parses(self) -> None:
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
codex_no_progress_timeout_sec = 300

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.no_progress_timeout_sec, 300)

    def test_no_progress_timeout_sec_new_key_wins_over_alias(self) -> None:
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
no_progress_timeout_sec = 120
codex_no_progress_timeout_sec = 300

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.no_progress_timeout_sec, 120)

    def test_invalid_claude_permission_mode_is_rejected(self) -> None:
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
claude_permission_mode = "bypassPermissions"

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "claude_permission_mode"):
                load_config(config_path)

    def test_loads_named_routing_policies(self) -> None:
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
codex_quality_tier = "implementation-fast-path"

[control.model_routing]
[[control.model_routing.policies]]
name = "implementation-fast-path"
task_class = "implementation"
tool_call_budget = 77
candidates = [
  { model = "invented-cached-model", reasoning_effort = "ultra" },
  { model = "fallback-model", reasoning_effort = "medium" },
]
adaptive = { minimum_samples_per_candidate = 3, history_window = 20, quality_floor = 0.8, prior_quality = 0.75, prior_weight = 2.0, allow_missing_price = true }

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.codex_quality_tier, "implementation-fast-path")
            self.assertEqual(len(config.routing_policies), 1)
            policy = config.routing_policies[0]
            self.assertEqual(policy.name, "implementation-fast-path")
            self.assertEqual(policy.task_class, "implementation")
            self.assertEqual(policy.tool_call_budget, 77)
            self.assertEqual(
                [(candidate.model, candidate.reasoning_effort) for candidate in policy.candidates],
                [("invented-cached-model", "ultra"), ("fallback-model", "medium")],
            )
            self.assertTrue(policy.adaptive is not None and policy.adaptive.allow_missing_price)

            payload = config_path.read_text(encoding="utf-8")
            invalid_cases = (
                ("nonpositive budget", "tool_call_budget = 77", "tool_call_budget = 0", "positive"),
                (
                    "duplicate candidates",
                    '{ model = "invented-cached-model", reasoning_effort = "ultra" }',
                    '{ model = "fallback-model", reasoning_effort = "medium" }',
                    "duplicate model and effort pairs",
                ),
                (
                    "too few adaptive samples",
                    "minimum_samples_per_candidate = 3",
                    "minimum_samples_per_candidate = 1",
                    "at least two comparable samples are required",
                ),
                (
                    "infeasible adaptive history window",
                    "history_window = 20",
                    "history_window = 5",
                    r"history_window must be at least minimum_samples_per_candidate \* len\(candidates\)",
                ),
            )
            for label, old, new, message in invalid_cases:
                with self.subTest(label=label):
                    config_path.write_text(payload.replace(old, new), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(config_path)

    def test_loads_codex_spark_models_override(self) -> None:
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
codex_spark_models = ["gpt-5.3-codex-spark", "gpt-5.6-spark"]

[routes]

[routes.main]
path = "repo"
required_branch = "main"
backend = "codex"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(
                config.defaults.codex_spark_models,
                ("gpt-5.3-codex-spark", "gpt-5.6-spark"),
            )

    def test_loads_model_catalog_overlay_and_arbitrary_quota_domain(self) -> None:
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

[control.model_catalog]
cache_path = "cache/models_cache.json"
max_cache_age_sec = 600

[[control.model_catalog.quota_domains]]
name = "primary"
max_concurrent_jobs = 1
max_burst_jobs = 2
soft_limit_percent = 75

[[control.model_catalog.quota_domains]]
name = "expedited"
max_concurrent_jobs = 3
max_burst_jobs = 6
soft_limit_percent = 90

[[control.model_catalog.models]]
model = "future-codex"
quota_domain = "expedited"
capacity_units = { low = 4, max = 12, ultra = 18 }
credit_rate = { input = 2.0, cached_input = 0.2, output = 12.0 }
api_usd_rate = { input = 1.0, cached_input = 0.1, output = 6.0 }
rate_card_version = "future-v1"
rate_card_source = "operator-verified"

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(
                config.model_catalog.cache_path,
                (root / "cache" / "models_cache.json").resolve(strict=False),
            )
            self.assertEqual(config.model_catalog.max_cache_age_sec, 600.0)
            self.assertEqual(
                tuple(domain.name for domain in config.model_catalog.quota_domains),
                ("primary", "expedited"),
            )
            model = config.model_catalog.models[0]
            self.assertEqual(model.model, "future-codex")
            self.assertEqual(model.quota_domain, "expedited")
            self.assertEqual(model.capacity_units, (("low", 4), ("max", 12), ("ultra", 18)))
            self.assertEqual(
                model.credit_rate.output if model.credit_rate is not None else None, 12.0
            )
            self.assertEqual(
                model.api_usd_rate.output if model.api_usd_rate is not None else None, 6.0
            )
            self.assertEqual(model.rate_card_version, "future-v1")
            self.assertEqual(model.rate_card_source, "operator-verified")
            self.assertFalse(model.premium)

    def test_loads_model_rules_in_model_catalog_and_claude_model_catalog(self) -> None:
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

[[control.model_catalog.model_rules]]
match = "gpt-5.6-*"
premium = true
quota_domain = "primary"
capacity_units = { low = 5, high = 15 }

[[control.claude_model_catalog.model_rules]]
match = "claude-*"
api_usd_rate = { input = 3.0, cached_input = 0.3, output = 15.0 }
rate_card_version = "2026-07-09"
rate_card_source = "operator-supplied"

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)
            self.assertEqual(len(config.model_catalog.model_rules), 1)
            rule = config.model_catalog.model_rules[0]
            self.assertEqual(rule.match, "gpt-5.6-*")
            self.assertTrue(rule.premium)
            self.assertEqual(rule.quota_domain, "primary")
            self.assertEqual(rule.capacity_units, (("high", 15), ("low", 5)))

            self.assertEqual(len(config.claude_model_catalog.model_rules), 1)
            claude_rule = config.claude_model_catalog.model_rules[0]
            self.assertEqual(claude_rule.match, "claude-*")
            self.assertIsNotNone(claude_rule.api_usd_rate)
            self.assertEqual(claude_rule.rate_card_version, "2026-07-09")

    def test_rejects_model_rule_without_version_and_source(self) -> None:
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

[[control.model_catalog.model_rules]]
match = "gpt-5.6-*"
credit_rate = { input = 25.0, cached_input = 2.5, output = 150.0 }

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as cm:
                load_config(config_path)
            self.assertIn("needs version and source", str(cm.exception))

    def test_rejects_model_rule_with_empty_match_or_unknown_field_or_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config" / "workspaces.toml"
            config_path.parent.mkdir(parents=True)

            # 1. Empty match
            config_path.write_text(
                """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[[control.model_catalog.model_rules]]
match = "  "

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as cm1:
                load_config(config_path)
            self.assertIn("match pattern must not be empty", str(cm1.exception))

            # 2. Unknown field
            config_path.write_text(
                """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[[control.model_catalog.model_rules]]
match = "gpt-*"
invalid_field = 123

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as cm2:
                load_config(config_path)
            self.assertIn("unknown field(s)", str(cm2.exception))

            # 3. Duplicate match patterns
            config_path.write_text(
                """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"

[[control.model_catalog.model_rules]]
match = "gpt-5.6-*"

[[control.model_catalog.model_rules]]
match = "gpt-5.6-*"

[routes.main]
path = "repo"
required_branch = "main"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as cm3:
                load_config(config_path)
            self.assertIn("duplicate match patterns", str(cm3.exception))

    def test_loads_codex_spark_max_concurrent_jobs_override(self) -> None:
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
codex_spark_max_concurrent_jobs = 9

[routes.main]
path = "repo"
required_branch = "main"
backend = "codex"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.defaults.codex_spark_max_concurrent_jobs, 9)

    def test_rejects_non_positive_spark_max_concurrent_jobs(self) -> None:
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
codex_spark_max_concurrent_jobs = 0

[routes.main]
path = "repo"
required_branch = "main"
backend = "codex"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "codex_spark_max_concurrent_jobs must be positive",
            ):
                load_config(config_path)

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
native_quality_max_parallel = 2

[[routes.main.native_quality_gates]]
name = "affected-tests"
command = ["python", "scripts/run_affected_tests.py", "--worktree"]
working_dir = "."
timeout_sec = 300
run_on = "controller"

[[routes.main.native_quality_gates]]
name = "ruff"
command = ["python", "-m", "ruff", "check", "{changed_python_files}"]
include_globs = ["*.py", "**/*.py"]
run_on = "both"
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            route = config.routes["main"]
            self.assertEqual(config.defaults.native_quality_policy, "off")
            self.assertEqual(route.native_quality_policy, "controller")
            self.assertEqual(route.native_quality_max_parallel, 2)
            self.assertEqual(
                [gate.name for gate in route.native_quality_gates],
                ["affected-tests", "ruff"],
            )
            self.assertEqual(route.native_quality_gates[0].run_on, "controller")
            self.assertEqual(route.native_quality_gates[1].run_on, "both")
            self.assertEqual(route.native_quality_gates[0].command[-1], "--worktree")
            self.assertEqual(route.native_quality_gates[0].working_dir, Path("."))
            self.assertEqual(route.native_quality_gates[0].timeout_sec, 300)
            self.assertEqual(
                route.native_quality_gates[1].include_globs,
                ("*.py", "**/*.py"),
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
                (
                    "unknown run stage",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "tests"
command = ["python", "-m", "pytest"]
run_on = "sometimes"
""",
                    "run_on must be worker, controller, or both",
                ),
                (
                    "controller policy without controller gate",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "worker-only"
command = ["python", "-m", "ruff", "check", "src"]
run_on = "worker"
""",
                    "requires at least one controller quality gate",
                ),
                (
                    "excessive parallelism",
                    base.replace(
                        'native_quality_policy = "controller"',
                        'native_quality_policy = "controller"\nnative_quality_max_parallel = 5',
                    )
                    + """
[[routes.main.native_quality_gates]]
name = "tests"
command = ["python", "-m", "pytest"]
""",
                    "native_quality_max_parallel must be between 1 and 4",
                ),
                (
                    "unknown command placeholder",
                    base
                    + """
[[routes.main.native_quality_gates]]
name = "ruff"
command = ["python", "-m", "ruff", "check", "{changed_files}"]
""",
                    "unsupported command placeholder",
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


class ConfigDiscoveryTest(unittest.TestCase):
    def test_resolve_config_for_nearest_upwards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            sub_dir = root / "work" / "src" / "pkg"
            sub_dir.mkdir(parents=True)
            config_dir = root / "work" / ".agent-work"
            config_dir.mkdir(parents=True)
            config_file = config_dir / "workspaces.toml"
            config_file.write_text("[control]\n[routes.main]\npath='.'\nrequired_branch='main'\n")

            found = resolve_config_for(sub_dir)
            self.assertEqual(found, config_file)

    def test_known_config_index_roundtrips_and_self_registers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg_path = root / "custom_workspaces.toml"
            cfg_path.write_text(
                "[control]\ncoordination_root='.agent-work'\nruns_root='runs'\n"
                "database='jobs.db'\nworktree_root='w'\nworktree_base='b'\nslot_root='s'\n"
                "[routes.main]\npath='repo'\nrequired_branch='main'\n"
            )

            idx_file = root / "known-configs.json"
            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                registered = register_known_config(cfg_path)
                self.assertEqual(registered, cfg_path)
                self.assertTrue(idx_file.exists())
                import json

                data = json.loads(idx_file.read_text(encoding="utf-8"))
                self.assertIn(str(cfg_path), data)

                cfg_path2 = root / "custom_workspaces2.toml"
                cfg_path2.write_text(
                    "[control]\ncoordination_root='.agent-work'\nruns_root='runs'\n"
                    "database='jobs.db'\nworktree_root='w'\nworktree_base='b'\nslot_root='s'\n"
                    "[routes.main]\npath='repo'\nrequired_branch='main'\n"
                )
                load_config(cfg_path2)
                data2 = json.loads(idx_file.read_text(encoding="utf-8"))
                self.assertIn(str(cfg_path2), data2)

    def test_known_config_index_concurrent_write(self) -> None:
        import concurrent.futures
        import json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            idx_file = root / "known-configs.json"

            def worker(i: int) -> None:
                p = root / f"cfg_{i}.toml"
                p.write_text("dummy")
                register_known_config(p)

            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    futures = [executor.submit(worker, i) for i in range(10)]
                    concurrent.futures.wait(futures)

                data = json.loads(idx_file.read_text(encoding="utf-8"))
                self.assertEqual(len(data), 10)

    def test_resolve_config_for_known_index_route_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()

            cfg1 = root / "cfg1" / "workspaces.toml"
            cfg1.parent.mkdir(parents=True)
            cfg1.write_text(
                f'[control]\ncoordination_root="{(root / "cfg1" / ".agent-work").as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.main]\npath="{(root / "projectA").as_posix()}"\nrequired_branch="main"\n'
            )

            cfg2 = root / "cfg2" / "workspaces.toml"
            cfg2.parent.mkdir(parents=True)
            cfg2.write_text(
                f'[control]\ncoordination_root="{(root / "cfg2" / ".agent-work").as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.main]\npath="{(root / "projectA" / "deep" / "sub").as_posix()}"\nrequired_branch="main"\n'
            )

            idx_file = root / "known-configs.json"
            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                register_known_config(cfg1)
                register_known_config(cfg2)

                cwd_inside = root / "projectA" / "deep" / "sub" / "src"
                cwd_inside.mkdir(parents=True)
                res1 = resolve_config_for(cwd_inside)
                self.assertEqual(res1, cfg2)

                cwd_outside = root
                res2 = resolve_config_for(cwd_outside)
                self.assertEqual(res2, cfg2)

    def test_resolve_config_for_discovery_precedence_by_specificity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            idx_file = root / "known-configs.json"

            # 1. Outer repo (work) with its own .agent-work/workspaces.toml pointing route 'hhru' at work_dir
            work_dir = root / "work"
            work_dir.mkdir(parents=True)
            work_cfg = work_dir / ".agent-work" / "workspaces.toml"
            work_cfg.parent.mkdir(parents=True)
            work_cfg.write_text(
                f'[control]\ncoordination_root="{(work_cfg.parent).as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.hhru]\npath="{work_dir.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            # 2. Second config elsewhere (gvsu) whose route 'natively' points at a subdirectory of work_dir
            natively_dir = work_dir / "sources" / "natively"
            natively_dir.mkdir(parents=True)
            gvsu_cfg_dir = root / "gvsu"
            gvsu_cfg_dir.mkdir(parents=True)
            gvsu_cfg = gvsu_cfg_dir / "workspaces.toml"

            main_tiger_dir = root / "main-tiger"
            main_tiger_dir.mkdir(parents=True)

            gvsu_cfg.write_text(
                f'[control]\ncoordination_root="{(gvsu_cfg_dir / ".agent-work").as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.natively]\npath="{natively_dir.as_posix()}"\nrequired_branch="main"\n'
                f'[routes.main]\npath="{main_tiger_dir.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            # 3. Codar repo with its own .agent-work/workspaces.toml
            codar_dir = root / "codar"
            codar_dir.mkdir(parents=True)
            codar_cfg = codar_dir / ".agent-work" / "workspaces.toml"
            codar_cfg.parent.mkdir(parents=True)
            codar_cfg.write_text(
                f'[control]\ncoordination_root="{(codar_cfg.parent).as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.radar]\npath="{codar_dir.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            # 4. Unmentioned directory with no .agent-work above it
            unmentioned_dir = root / "unmentioned"
            unmentioned_dir.mkdir(parents=True)

            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                register_known_config(work_cfg)
                register_known_config(gvsu_cfg)
                register_known_config(codar_cfg)

                # Outcome 1: D:/Documents/work/sources/natively -> GVSU config (route natively matches exactly)
                self.assertEqual(resolve_config_for(natively_dir), gvsu_cfg)

                # Outcome 2: D:/Documents/work -> work config (route hhru matches exactly; GVSU is weaker Rule 3)
                self.assertEqual(resolve_config_for(work_dir), work_cfg)

                # Outcome 3: D:/Projects/VSCode/codar -> codar config
                self.assertEqual(resolve_config_for(codar_dir), codar_cfg)

                # Outcome 4: D:/Projects/VSCode/GVSU/main-tiger -> GVSU config
                self.assertEqual(resolve_config_for(main_tiger_dir), gvsu_cfg)

                # Outcome 5: Unmentioned dir -> default_config_path()
                self.assertEqual(resolve_config_for(unmentioned_dir), default_config_path())

    def test_resolve_config_for_tie_breaking_is_deterministic(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            idx_file = root / "known-configs.json"

            target_dir = root / "shared_project"
            target_dir.mkdir(parents=True)

            cfg_a = root / "aaa" / "workspaces.toml"
            cfg_a.parent.mkdir(parents=True)
            cfg_a.write_text(
                f'[control]\ncoordination_root="{(cfg_a.parent / ".agent-work").as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.main]\npath="{target_dir.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            cfg_b = root / "bbb" / "workspaces.toml"
            cfg_b.parent.mkdir(parents=True)
            cfg_b.write_text(
                f'[control]\ncoordination_root="{(cfg_b.parent / ".agent-work").as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.main]\npath="{target_dir.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            # Test registration order A then B
            idx_file.write_text(json.dumps([str(cfg_a), str(cfg_b)]), encoding="utf-8")
            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                res1 = resolve_config_for(target_dir)

            # Test registration order B then A
            idx_file.write_text(json.dumps([str(cfg_b), str(cfg_a)]), encoding="utf-8")
            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                res2 = resolve_config_for(target_dir)

            self.assertEqual(res1, res2)
            # Deterministic winner is cfg_a because str(cfg_a) < str(cfg_b)
            self.assertEqual(res1, cfg_a)

    def test_index_path_follows_the_environment_override(self) -> None:
        from agent_control_plane.shared.config import KNOWN_CONFIGS_ENV_VAR, known_configs_path

        with tempfile.TemporaryDirectory() as temp:
            redirected = Path(temp) / "elsewhere" / "known-configs.json"
            with patch.dict(os.environ, {KNOWN_CONFIGS_ENV_VAR: str(redirected)}):
                self.assertEqual(known_configs_path(), redirected)

    def test_registering_drops_entries_whose_config_is_gone(self) -> None:
        import json

        from agent_control_plane.shared.config import register_known_config

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            idx_file = root / "known-configs.json"
            live = root / "live" / "workspaces.toml"
            live.parent.mkdir(parents=True)
            live.write_text("", encoding="utf-8")
            dead = root / "gone" / "workspaces.toml"
            idx_file.write_text(json.dumps([str(dead), str(live)]), encoding="utf-8")

            newcomer = root / "newcomer" / "workspaces.toml"
            newcomer.parent.mkdir(parents=True)
            newcomer.write_text("", encoding="utf-8")

            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                register_known_config(newcomer)

            entries = json.loads(idx_file.read_text(encoding="utf-8"))
            # A dead entry is not free: every resolution stats and parses each survivor.
            self.assertEqual(entries, [str(live), str(newcomer)])

    def test_tie_breaking_prefers_the_config_nearest_the_working_directory(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            idx_file = root / "known-configs.json"
            target_dir = root / "project"
            target_dir.mkdir(parents=True)

            def _write(cfg: Path) -> None:
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text(
                    f'[control]{chr(10)}coordination_root="{(cfg.parent / ".agent-work").as_posix()}"{chr(10)}'
                    f'runs_root="runs"{chr(10)}database="db"{chr(10)}worktree_root="w"{chr(10)}'
                    f'worktree_base="b"{chr(10)}slot_root="s"{chr(10)}'
                    f'[routes.main]{chr(10)}path="{target_dir.as_posix()}"{chr(10)}'
                    f'required_branch="main"{chr(10)}',
                    encoding="utf-8",
                )

            # Both match the working directory equally well, so only the tie-break decides.
            # The stray copy sorts first alphabetically; the project's own config does not.
            near = target_dir / "config" / "workspaces.toml"
            stray = root / "aaa-stray" / "workspaces.toml"
            _write(near)
            _write(stray)
            self.assertLess(str(stray), str(near))

            idx_file.write_text(json.dumps([str(stray), str(near)]), encoding="utf-8")
            with patch(
                "agent_control_plane.shared.config.known_configs_path",
                return_value=idx_file,
            ):
                self.assertEqual(resolve_config_for(target_dir), near)

    def test_port_for_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg1 = root / "config1" / "workspaces.toml"
            cfg2 = root / "config2" / "workspaces.toml"
            cfg1.parent.mkdir()
            cfg2.parent.mkdir()
            cfg1.write_text("c1")
            cfg2.write_text("c2")

            port1 = port_for(cfg1)
            port1_repeat = port_for(cfg1)
            port2 = port_for(cfg2)

            self.assertTrue(9230 <= port1 <= 9329)
            self.assertTrue(9230 <= port2 <= 9329)
            self.assertEqual(port1, port1_repeat)

            # Only a case-insensitive filesystem may fold the two spellings into one
            # config; on Linux `CONFIG1` and `config1` are genuinely different paths and
            # must keep their own ports.
            if os.path.normcase("A") == os.path.normcase("a"):
                cfg1_spelling = root / "CONFIG1" / "workspaces.toml"
                self.assertEqual(port_for(cfg1_spelling), port1)

    def test_port_for_keeps_a_remembered_port_that_is_still_booting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text("c")
            assignments = root / "port-assignments.json"
            canonical = os.path.normcase(str(cfg))
            assignments.write_text(json.dumps({canonical: 9260}), encoding="utf-8")

            # A server that has just bound the port answers only on the third probe.
            probes = [False, False, True]

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch("agent_control_plane.shared.config._is_port_open", return_value=True),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    side_effect=lambda *_a, **_k: probes.pop(0) if probes else True,
                ),
                patch("time.sleep"),
            ):
                self.assertEqual(port_for(cfg), 9260)

    def test_port_for_gives_up_a_remembered_port_held_by_something_else(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text("c")
            assignments = root / "port-assignments.json"
            canonical = os.path.normcase(str(cfg))
            assignments.write_text(json.dumps({canonical: 9260}), encoding="utf-8")

            def open_ports(port: int) -> bool:
                return port == 9260

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch(
                    "agent_control_plane.shared.config._is_port_open",
                    side_effect=open_ports,
                ),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=False,
                ),
                patch("time.sleep"),
            ):
                moved = port_for(cfg)

            self.assertNotEqual(moved, 9260)
            self.assertTrue(9230 <= moved <= 9329)
            self.assertEqual(json.loads(assignments.read_text(encoding="utf-8"))[canonical], moved)

    def test_port_for_skips_candidate_recorded_for_another_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg_a = root / "config_a" / "workspaces.toml"
            cfg_b = root / "config_b" / "workspaces.toml"
            cfg_a.parent.mkdir()
            cfg_b.parent.mkdir()
            cfg_a.write_text("ca")
            cfg_b.write_text("cb")

            canonical_a = os.path.normcase(str(cfg_a))
            canonical_b = os.path.normcase(str(cfg_b))

            import hashlib

            base_hash_a = int(hashlib.sha256(canonical_a.encode("utf-8")).hexdigest(), 16)
            base_port_a = 9230 + (base_hash_a % 100)

            assignments = root / "port-assignments.json"
            assignments.write_text(json.dumps({canonical_b: base_port_a}), encoding="utf-8")

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch("agent_control_plane.shared.config._is_port_open", return_value=False),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=False,
                ),
            ):
                assigned_a = port_for(cfg_a)

            self.assertNotEqual(assigned_a, base_port_a)
            self.assertTrue(9230 <= assigned_a <= 9329)
            data = json.loads(assignments.read_text(encoding="utf-8"))
            self.assertEqual(data[canonical_b], base_port_a)
            self.assertEqual(data[canonical_a], assigned_a)

    def test_port_for_skips_candidate_occupied_by_healthy_foreign_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg_a = root / "config_a" / "workspaces.toml"
            cfg_a.parent.mkdir()
            cfg_a.write_text("ca")

            canonical_a = os.path.normcase(str(cfg_a))
            import hashlib

            base_hash_a = int(hashlib.sha256(canonical_a.encode("utf-8")).hexdigest(), 16)
            base_port_a = 9230 + (base_hash_a % 100)

            assignments = root / "port-assignments.json"

            def is_open(port: int) -> bool:
                return port == base_port_a

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch(
                    "agent_control_plane.shared.config._is_port_open",
                    side_effect=is_open,
                ),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=True,
                ),
            ):
                assigned_a = port_for(cfg_a)

            self.assertNotEqual(assigned_a, base_port_a)
            self.assertTrue(9230 <= assigned_a <= 9329)
            data = json.loads(assignments.read_text(encoding="utf-8"))
            self.assertEqual(data[canonical_a], assigned_a)

    def test_port_for_takes_and_persists_free_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text("c")

            assignments = root / "port-assignments.json"
            canonical = os.path.normcase(str(cfg))

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch("agent_control_plane.shared.config._is_port_open", return_value=False),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=False,
                ),
            ):
                assigned = port_for(cfg)

            self.assertTrue(9230 <= assigned <= 9329)
            self.assertTrue(assignments.is_file())
            data = json.loads(assignments.read_text(encoding="utf-8"))
            self.assertEqual(data[canonical], assigned)

    def test_port_for_corrupt_file_is_preserved_and_raises_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg_new = root / "config_new" / "workspaces.toml"
            cfg_new.parent.mkdir()
            cfg_new.write_text("c_new")

            assignments = root / "port-assignments.json"
            corrupt_content = '{"/path/to/other/config": 9250, "corrupt": '
            assignments.write_text(corrupt_content, encoding="utf-8")

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch("agent_control_plane.shared.config._is_port_open", return_value=False),
                self.assertRaises(RuntimeError) as ctx,
            ):
                port_for(cfg_new)

            self.assertIn("Failed to parse port assignments file", str(ctx.exception))
            if assignments.exists():
                data = assignments.read_text(encoding="utf-8")
                self.assertNotIn(os.path.normcase(str(cfg_new)), data)

            corrupt_files = list(root.glob("port-assignments.json.corrupt-*"))
            self.assertEqual(len(corrupt_files), 1)
            self.assertEqual(corrupt_files[0].read_text(encoding="utf-8"), corrupt_content)

    def test_port_for_empty_file_cold_starts_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text("c")

            assignments = root / "port-assignments.json"
            assignments.write_text("   \n", encoding="utf-8")
            canonical = os.path.normcase(str(cfg))

            with (
                patch(
                    "agent_control_plane.shared.config.port_assignments_path",
                    return_value=assignments,
                ),
                patch("agent_control_plane.shared.config._is_port_open", return_value=False),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=False,
                ),
            ):
                assigned = port_for(cfg)

            self.assertTrue(9230 <= assigned <= 9329)
            self.assertTrue(assignments.is_file())
            data = json.loads(assignments.read_text(encoding="utf-8"))
            self.assertEqual(data[canonical], assigned)

    def test_mcp_ensure_resolves_the_port_under_the_config_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text(
                '[control]\ncoordination_root=".agent-work"\nruns_root="runs"\n'
                'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                '[routes.main]\npath="repo"\nrequired_branch="main"\n'
            )
            events: list[str] = []

            @contextlib.contextmanager
            def recording_lock(config_path: Path) -> Iterator[None]:
                events.append("lock")
                try:
                    yield
                finally:
                    events.append("unlock")

            def recording_port_for(config_path: Path | str) -> int:
                events.append("port_for")
                return 9260

            with (
                patch(
                    "agent_control_plane.shared.config.interprocess_config_lock",
                    recording_lock,
                ),
                patch("agent_control_plane.shared.config.port_for", recording_port_for),
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=True,
                ),
            ):
                result = ensure_mcp_server(config_path=cfg)

            self.assertEqual(result["port"], 9260)
            self.assertEqual(events, ["lock", "port_for", "unlock"])

    def test_mcp_ensure_no_start_and_print_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text(
                '[control]\ncoordination_root=".agent-work"\nruns_root="runs"\n'
                'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                '[routes.main]\npath="repo"\nrequired_branch="main"\n'
            )

            with (
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=False,
                ),
                patch("subprocess.Popen") as mock_popen,
            ):
                res = ensure_mcp_server(config_path=cfg, no_start=True)
                self.assertTrue(res["ok"])
                self.assertFalse(res["running"])
                self.assertFalse(res["started"])
                self.assertEqual(mock_popen.call_count, 0)
                self.assertTrue(res["url"].startswith("http://127.0.0.1:"))

    def test_mcp_ensure_default_wait_is_90s_and_overridable(self) -> None:
        sig = inspect.signature(ensure_mcp_server)
        self.assertEqual(sig.parameters["timeout_sec"].default, 90.0)

        parser = _build_parser()
        args = parser.parse_args(["mcp", "ensure"])
        self.assertEqual(args.timeout, 90.0)

        args_override = parser.parse_args(["mcp", "ensure", "--timeout", "15.0"])
        self.assertEqual(args_override.timeout, 15.0)

    def test_mcp_ensure_already_healthy_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text(
                '[control]\ncoordination_root=".agent-work"\nruns_root="runs"\n'
                'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                '[routes.main]\npath="repo"\nrequired_branch="main"\n'
            )
            with (
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=True,
                ),
                patch("subprocess.Popen") as mock_popen,
            ):
                res = ensure_mcp_server(config_path=cfg)
                self.assertTrue(res["ok"])
                self.assertTrue(res["running"])
                self.assertFalse(res["started"])
                self.assertEqual(mock_popen.call_count, 0)

    def test_mcp_ensure_final_reprobe_succeeds_after_loop_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text(
                '[control]\ncoordination_root=".agent-work"\nruns_root="runs"\n'
                'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                '[routes.main]\npath="repo"\nrequired_branch="main"\n'
            )
            probe_responses = [False, False, True]

            def fake_probe(port: int, timeout_sec: float = 2.0) -> bool:
                if probe_responses:
                    return probe_responses.pop(0)
                return True

            with (
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    side_effect=fake_probe,
                ),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_popen.return_value = None
                res = ensure_mcp_server(config_path=cfg, timeout_sec=0.01)
                self.assertTrue(res["ok"])
                self.assertTrue(res["running"])
                self.assertTrue(res["started"])
                self.assertEqual(mock_popen.call_count, 1)

    def test_mcp_ensure_timeout_reports_details_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = root / "workspaces.toml"
            cfg.write_text(
                '[control]\ncoordination_root=".agent-work"\nruns_root="runs"\n'
                'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                '[routes.main]\npath="repo"\nrequired_branch="main"\n'
            )
            with (
                patch(
                    "agent_control_plane.shared.config.probe_mcp_health",
                    return_value=False,
                ),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_popen.return_value = None
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_mcp_server(config_path=cfg, timeout_sec=0.05)
                err_msg = str(ctx.exception)
                self.assertIn("Timed out after 0.05s", err_msg)
                self.assertIn("port", err_msg)
                self.assertIn(str(cfg), err_msg)
                self.assertIn("mcp-server-", err_msg)


class ProbeMcpHealthTest(unittest.TestCase):
    def test_probe_sends_accept_header(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {}
            mock_resp.read.return_value = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            ).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None
            mock_urlopen.return_value = mock_resp

            healthy, detail = probe_mcp_health_detailed(9256)
            self.assertTrue(healthy)
            self.assertEqual(detail, "healthy")
            self.assertTrue(mock_urlopen.called)

            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.headers.get("Accept"), "application/json, text/event-stream")
            self.assertEqual(req.headers.get("Content-type"), "application/json")

    def test_probe_406_response_reported_as_unhealthy_with_distinguishable_reason(self) -> None:
        err = urllib.error.HTTPError(
            url="http://127.0.0.1:9256/mcp",
            code=406,
            msg="Not Acceptable",
            hdrs=Message(),
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=err):
            healthy, detail = probe_mcp_health_detailed(9256)
            self.assertFalse(healthy)
            self.assertIn("something answered but not as an MCP server", detail)
            self.assertIn("406", detail)
            self.assertFalse(probe_mcp_health(9256))

    def test_probe_200_valid_response_is_healthy(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {}
            mock_resp.read.return_value = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            ).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None
            mock_urlopen.return_value = mock_resp

            healthy, detail = probe_mcp_health_detailed(9256)
            self.assertTrue(healthy)
            self.assertEqual(detail, "healthy")
            self.assertTrue(probe_mcp_health(9256))

    def test_probe_sse_framed_response_is_healthy(self) -> None:
        result = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"Content-Type": "text/event-stream"}
            mock_resp.read.return_value = f"event: message\ndata: {result}\n\n".encode()
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None
            mock_urlopen.return_value = mock_resp

            healthy, detail = probe_mcp_health_detailed(9256)
            self.assertTrue(healthy)
            self.assertEqual(detail, "healthy")

    def test_probe_unparsable_body_reported_as_not_an_mcp_server(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {}
            mock_resp.read.return_value = b"<html>hello</html>"
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None
            mock_urlopen.return_value = mock_resp

            healthy, detail = probe_mcp_health_detailed(9256)
            self.assertFalse(healthy)
            self.assertIn("neither JSON nor an SSE data frame", detail)

    def test_probe_connection_error_reported_as_nothing_listening(self) -> None:
        err = urllib.error.URLError(reason="Connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            healthy, detail = probe_mcp_health_detailed(9256)
            self.assertFalse(healthy)
            self.assertIn("nothing is listening", detail)
            self.assertFalse(probe_mcp_health(9256))

    def test_probe_session_cleanup_sends_delete_request(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"Mcp-Session-Id": "sess-xyz789"}
            mock_resp.read.return_value = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            ).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None
            mock_urlopen.return_value = mock_resp

            healthy, _ = probe_mcp_health_detailed(9256)
            self.assertTrue(healthy)

            self.assertEqual(mock_urlopen.call_count, 2)
            delete_req = mock_urlopen.call_args_list[1][0][0]
            self.assertEqual(delete_req.get_method(), "DELETE")
            self.assertEqual(delete_req.headers.get("Mcp-session-id"), "sess-xyz789")


class WireMcpTest(unittest.TestCase):
    def test_find_enclosing_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo = root / "my_repo"
            sub_dir = repo / "src" / "pkg"
            sub_dir.mkdir(parents=True)
            (repo / ".git").mkdir()

            found = find_enclosing_git_repo(sub_dir)
            self.assertEqual(found, repo)

            outside = root / "other"
            outside.mkdir()
            self.assertEqual(find_enclosing_git_repo(outside), outside)

    def test_wire_computes_client_repos_and_dry_runs_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()

            # Client repo 1 (owns coordination_root and route r1)
            repo1 = root / "repo1"
            (repo1 / ".git").mkdir(parents=True)
            coord_root = repo1 / ".agent-work"
            r1_path = repo1 / "sub_r1"
            coord_root.mkdir(parents=True)
            r1_path.mkdir(parents=True)

            # Client repo 2 (owns route r2)
            repo2 = root / "repo2"
            (repo2 / ".git").mkdir(parents=True)

            cfg_file = coord_root / "workspaces.toml"
            cfg_file.write_text(
                f'[control]\ncoordination_root="{coord_root.as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.r1]\npath="{r1_path.as_posix()}"\nrequired_branch="main"\n'
                f'[routes.r2]\npath="{repo2.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            res = wire_mcp_servers(config_path=cfg_file, apply=False)

            self.assertTrue(res["ok"])
            self.assertFalse(res["apply"])
            self.assertEqual(len(res["targets"]), 2)

            target_repos = {t["repo_path"] for t in res["targets"]}
            self.assertIn(str(repo1), target_repos)
            self.assertIn(str(repo2), target_repos)

            for t in res["targets"]:
                self.assertEqual(t["action"], "would_create")
                mcp_json_p = Path(t["mcp_json_path"])
                self.assertFalse(
                    mcp_json_p.exists(), f"File {mcp_json_p} should not exist in dry run"
                )

    def test_wire_writes_only_with_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()

            repo1 = root / "repo1"
            (repo1 / ".git").mkdir(parents=True)
            coord_root = repo1 / ".agent-work"
            coord_root.mkdir(parents=True)

            cfg_file = coord_root / "workspaces.toml"
            cfg_file.write_text(
                f'[control]\ncoordination_root="{coord_root.as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.r1]\npath="{repo1.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            res = wire_mcp_servers(config_path=cfg_file, apply=True)

            self.assertTrue(res["ok"])
            self.assertTrue(res["apply"])
            self.assertEqual(len(res["targets"]), 1)

            t = res["targets"][0]
            self.assertEqual(t["action"], "created")
            mcp_json_p = Path(t["mcp_json_path"])
            self.assertTrue(mcp_json_p.is_file())

            data = json.loads(mcp_json_p.read_text(encoding="utf-8"))
            self.assertIn("mcpServers", data)
            self.assertIn("agent_control_plane", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["agent_control_plane"]["type"], "http")
            self.assertEqual(data["mcpServers"]["agent_control_plane"]["url"], res["url"])

    def test_wire_merges_into_existing_mcp_json_without_disturbing_other_servers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()

            repo1 = root / "repo1"
            (repo1 / ".git").mkdir(parents=True)
            coord_root = repo1 / ".agent-work"
            coord_root.mkdir(parents=True)

            mcp_json_p = repo1 / ".mcp.json"
            initial_content = {
                "custom_key": "hello",
                "mcpServers": {
                    "other_server": {
                        "command": "node",
                        "args": ["server.js"],
                    }
                },
            }
            mcp_json_p.write_text(json.dumps(initial_content, indent=2), encoding="utf-8")

            cfg_file = coord_root / "workspaces.toml"
            cfg_file.write_text(
                f'[control]\ncoordination_root="{coord_root.as_posix()}"\nruns_root="runs"\n'
                f'database="db"\nworktree_root="w"\nworktree_base="b"\nslot_root="s"\n'
                f'[routes.r1]\npath="{repo1.as_posix()}"\nrequired_branch="main"\n',
                encoding="utf-8",
            )

            res = wire_mcp_servers(config_path=cfg_file, apply=True)

            self.assertTrue(res["ok"])
            t = res["targets"][0]
            self.assertEqual(t["action"], "updated")

            data = json.loads(mcp_json_p.read_text(encoding="utf-8"))
            self.assertEqual(data.get("custom_key"), "hello")
            self.assertIn("other_server", data.get("mcpServers", {}))
            self.assertEqual(data["mcpServers"]["other_server"]["command"], "node")
            self.assertIn("agent_control_plane", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["agent_control_plane"]["url"], res["url"])

    def test_wire_fails_cleanly_when_no_config_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            non_existent_cfg = root / "non_existent.toml"
            with self.assertRaises(FileNotFoundError):
                wire_mcp_servers(config_path=non_existent_cfg)


if __name__ == "__main__":
    unittest.main()
