from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.shared.config import CodexModelMetadataConfig, load_config


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

[[control.claude_model_catalog.models]]
model = "claude-opus-4-8"
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
            metadata = {model.model: model for model in config.claude_model_catalog.models}
            self.assertTrue(metadata["claude-opus-4-8"].premium)
            self.assertEqual(metadata["claude-opus-4-8"].api_usd_rate.input, 5.0)
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
            self.assertEqual(config.claude_model_catalog.models, ())
            self.assertEqual(config.claude_model_catalog.inventory, ())

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


if __name__ == "__main__":
    unittest.main()
