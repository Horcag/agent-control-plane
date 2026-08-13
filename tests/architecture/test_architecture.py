import argparse
from pathlib import Path
from unittest.mock import Mock, patch

import pytest_fsd
from pytest_fsd import validate_fsd_architecture
from pytest_fsd.config import load_config

from agent_control_plane.app.runtime.cli import _build_parser


def test_project_architecture(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = load_config(str(project_root))

    # pytest-fsd uses base_path both as an import package and as a filesystem root.
    # In a src-layout project, the package root is one level below the repo root.
    monkeypatch.setattr(pytest_fsd, "load_config", lambda _: config)

    validate_fsd_architecture(str(project_root / "src"))


# Every MCP tool's operation must be reachable through the CLI under a command path an
# operator can guess from the MCP name. This mapping is the durable source of truth for
# that correspondence: the test below fails if a new MCP tool is added without extending
# it, if a mapped CLI command path stops parsing, or if the mapping goes stale because a
# tool was renamed or removed. It does not check the reverse direction (CLI commands with
# no MCP mirror, e.g. `demo`, `review`, `manager`, `mcp`, `list`, `run-job`)
# because many CLI-only commands are intentionally local (offline demo, MCP server
# lifecycle, root-review cost accounting) and a mechanical reverse mapping would just be
# restating the CLI subcommand list.
MCP_TOOL_TO_CLI_COMMAND_PATH = {
    "agent_accept_handoff": ("accept-handoff",),
    "agent_analytics": ("analytics",),
    "agent_archive_jobs": ("archive",),
    "agent_cancel_job": ("cancel",),
    "agent_model_catalog": ("model-catalog",),
    "agent_model_routing_explain": ("model-routing-explain",),
    "agent_plan_accept_task": ("plan", "accept"),
    "agent_plan_add_task": ("plan", "add-task"),
    "agent_plan_archive": ("plan", "archive"),
    "agent_plan_bind_job": ("plan", "bind"),
    "agent_plan_cancel": ("plan", "cancel"),
    "agent_plan_create": ("plan", "create"),
    "agent_plan_dispatch": ("plan", "dispatch"),
    "agent_plan_edit_task": ("plan", "edit-task"),
    "agent_plan_list": ("plan", "list"),
    "agent_plan_reject_task": ("plan", "reject"),
    "agent_plan_retry_task": ("plan", "retry"),
    "agent_plan_run_until_review": ("plan", "run"),
    "agent_plan_snapshot": ("plan", "snapshot"),
    "agent_plan_watch": ("plan", "watch"),
    "agent_reconcile": ("reconcile",),
    "agent_result_job": ("result",),
    "agent_retention_gc": ("gc",),
    "agent_review_inbox_get": ("inbox", "get"),
    "agent_review_inbox_list": ("inbox", "list"),
    "agent_review_inbox_requalify": ("inbox", "requalify"),
    "agent_review_inbox_resolve": ("inbox", "resolve"),
    "agent_slots_bootstrap": ("slots", "bootstrap"),
    "agent_slots_checkout": ("slots", "checkout"),
    "agent_slots_checkpoint": ("slots", "checkpoint"),
    "agent_slots_cleanup": ("slots", "cleanup"),
    "agent_slots_create": ("slots", "create"),
    "agent_slots_delete": ("slots", "delete"),
    "agent_slots_ensure_module": ("slots", "ensure-module"),
    "agent_slots_ensure_root_module": ("slots", "ensure-root-module"),
    "agent_slots_list": ("slots", "list"),
    "agent_slots_prepare": ("slots", "prepare"),
    "agent_slots_remove_module": ("slots", "remove-module"),
    "agent_slots_sync": ("slots", "sync"),
    "agent_slots_unload_module": ("slots", "unload-module"),
    "agent_slots_unload_root_module": ("slots", "unload-root-module"),
    "agent_smoke": ("smoke",),
    "agent_start_job": ("start",),
    "agent_status_job": ("status",),
    "agent_summary_job": ("summary",),
    "agent_terminal_statuses": ("statuses",),
    "agent_sync_subagent_results": ("inbox", "sync-subagents"),
    "agent_tail_job": ("tail",),
    "agent_watch_events": ("watch",),
    "agent_watch_job": ("watch",),
}


def _cli_leaf_command_paths(parser: argparse.ArgumentParser) -> set[tuple[str, ...]]:
    """Every command path (including aliases) the CLI parser will actually accept."""
    paths: set[tuple[str, ...]] = set()

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        sub_actions = [
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        ]
        if not sub_actions:
            paths.add(prefix)
            return
        for action in sub_actions:
            for name, subparser in action.choices.items():
                walk(subparser, (*prefix, name))

    walk(parser, ())
    return paths


def test_cli_mcp_command_parity() -> None:
    """Every MCP tool must have a working CLI command path under the pinned mapping.

    Keeps the two surfaces from drifting apart: a new MCP tool with no mapping entry,
    a mapping entry naming a CLI command path that no longer parses, or a mapping entry
    for an MCP tool that was renamed or removed all fail this test.
    """
    from agent_control_plane.app.runtime.mcp_server import build_server

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=Mock(),
    ):
        server = build_server()
    mcp_tool_names = {tool.name for tool in server._tool_manager.list_tools()}

    unmapped_tools = mcp_tool_names - set(MCP_TOOL_TO_CLI_COMMAND_PATH)
    assert not unmapped_tools, (
        f"New MCP tool(s) with no CLI parity mapping in test_architecture.py: "
        f"{sorted(unmapped_tools)}"
    )

    stale_entries = set(MCP_TOOL_TO_CLI_COMMAND_PATH) - mcp_tool_names
    assert not stale_entries, (
        f"MCP_TOOL_TO_CLI_COMMAND_PATH references removed/renamed MCP tool(s): "
        f"{sorted(stale_entries)}"
    )

    cli_paths = _cli_leaf_command_paths(_build_parser())
    for mcp_name, cli_path in MCP_TOOL_TO_CLI_COMMAND_PATH.items():
        assert cli_path in cli_paths, (
            f"{mcp_name} is mapped to CLI command path {cli_path!r}, "
            "which the CLI parser does not accept"
        )
