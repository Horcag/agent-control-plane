from __future__ import annotations

import json

import pytest

from agent_control_plane.features.agent_runner.lib.claude_mcp_config import (
    resolve_claude_mcp_server_definition,
    select_ide_mcp_server,
    write_claude_mcp_config,
)
from agent_control_plane.shared.config import load_config

_BASE_TOML = """
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "worktrees"
worktree_base = "repo"
slot_root = "slots"
{extra_control}

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"
{extra_defaults}

[routes.main]
path = "repo"
required_branch = "main"
{extra_route}
{extra_tables}
"""


def _config(*, extra_control="", extra_defaults="", extra_route="", extra_tables=""):
    toml = _BASE_TOML.format(
        extra_control=extra_control,
        extra_defaults=extra_defaults,
        extra_route=extra_route,
        extra_tables=extra_tables,
    )
    return load_config(config_contents=toml.encode("utf-8"))


def test_select_defaults_to_idea_64343_without_route_override() -> None:
    assert select_ide_mcp_server(_config(), "main") == "agentbridge_idea_64343"


def test_select_honors_route_ide_mcp_server() -> None:
    config = _config(extra_route='ide_mcp_server = "agentbridge_idea_8644"')
    assert select_ide_mcp_server(config, "main") == "agentbridge_idea_8644"


def test_select_rejects_a_disabled_route_server() -> None:
    config = _config(
        extra_defaults='codex_disabled_mcp_servers = ["agentbridge_idea_8644"]',
        extra_route='ide_mcp_server = "agentbridge_idea_8644"',
    )
    with pytest.raises(ValueError, match="disabled"):
        select_ide_mcp_server(config, "main")


def test_resolve_prefers_the_explicit_acp_override() -> None:
    config = _config(
        extra_control='claude_config_path = "/no/such/claude.json"',
        extra_tables=(
            "[control.claude_mcp_servers.agentbridge_idea_64343]\n"
            'type = "http"\n'
            'url = "http://127.0.0.1:64343/mcp"\n'
        ),
    )
    definition = resolve_claude_mcp_server_definition(config, "agentbridge_idea_64343")
    assert definition == {"type": "http", "url": "http://127.0.0.1:64343/mcp"}


def test_resolve_sources_from_the_operator_claude_config(tmp_path) -> None:
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {"mcpServers": {"agentbridge_idea_64343": {"type": "http", "url": "http://x/mcp"}}}
        ),
        encoding="utf-8",
    )
    config = _config(extra_control=f'claude_config_path = "{claude_config.as_posix()}"')
    definition = resolve_claude_mcp_server_definition(config, "agentbridge_idea_64343")
    assert definition == {"type": "http", "url": "http://x/mcp"}


def test_resolve_fails_closed_when_the_server_is_undefined(tmp_path) -> None:
    config = _config(
        extra_control=f'claude_config_path = "{(tmp_path / "missing.json").as_posix()}"'
    )
    with pytest.raises(ValueError, match="is not defined"):
        resolve_claude_mcp_server_definition(config, "agentbridge_idea_64343")


def test_write_claude_mcp_config_emits_a_single_server(tmp_path) -> None:
    path = write_claude_mcp_config(
        tmp_path / "runs" / "job-1",
        "agentbridge_idea_64343",
        {"type": "http", "url": "http://127.0.0.1:64343/mcp"},
    )
    assert path == tmp_path / "runs" / "job-1" / "claude-mcp-config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "mcpServers": {
            "agentbridge_idea_64343": {"type": "http", "url": "http://127.0.0.1:64343/mcp"}
        }
    }
