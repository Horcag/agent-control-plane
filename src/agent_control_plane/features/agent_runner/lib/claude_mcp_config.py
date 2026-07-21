"""Resolve and materialize the isolated IDE MCP config for claude ide_mcp jobs.

The claude backend reaches an IntelliJ/AgentBridge MCP server the same way Codex does:
the server lives in the operator's own CLI config. For Codex that is
``~/.codex/config.toml``; for Claude it is ``~/.claude.json`` (``mcpServers``). ACP does
not own those endpoints, it only selects one per route and hands the worker exactly that
server via ``--mcp-config`` while ``claude_bare``'s ``--strict-mcp-config`` keeps every
other operator server out of the worker process.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_control_plane.shared.config import ControlConfig


def select_ide_mcp_server(config: ControlConfig, route: str) -> str:
    """Return the IDEA MCP server a route drives.

    This mirrors the prompt builder exactly so the ``--mcp-config`` server and the
    ``mcp__<server>__*`` tool namespace named in the prompt can never diverge.
    """

    disabled = set(config.defaults.codex_disabled_mcp_servers)
    route_config = config.routes.get(route)
    configured_server = route_config.ide_mcp_server if route_config is not None else None
    if configured_server is not None:
        if configured_server in disabled:
            raise ValueError(
                f"Route {route!r} selects disabled IDEA MCP server {configured_server!r}"
            )
        return configured_server
    if "agentbridge_idea_64343" in disabled and "agentbridge_idea_8644" not in disabled:
        return "agentbridge_idea_8644"
    return "agentbridge_idea_64343"


def resolve_claude_mcp_server_definition(config: ControlConfig, server_name: str) -> dict[str, Any]:
    """Resolve the MCP server definition for ``server_name``.

    An explicit ``[control.claude_mcp_servers.<name>]`` override wins; otherwise the
    endpoint is sourced from the operator's Claude config. Fails closed when neither
    defines the server, so an ide_mcp claude job is blocked at launch rather than
    spawned against a worker that has no IDE tools.
    """

    override = config.claude_mcp_servers.get(server_name)
    if override:
        return dict(override)
    operator_servers = _load_operator_mcp_servers(_claude_config_path(config))
    definition = operator_servers.get(server_name)
    if definition is None:
        raise ValueError(
            f"Claude MCP server {server_name!r} is not defined. Add "
            f"[control.claude_mcp_servers.{server_name}] to the ACP config, or register the "
            f"server in the operator Claude config, so claude ide_mcp jobs can reach the IDE MCP."
        )
    return dict(definition)


def write_claude_mcp_config(
    run_dir: Path,
    server_name: str,
    definition: Mapping[str, Any],
) -> Path:
    """Write a durable single-server ``--mcp-config`` file under the job run dir."""

    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "claude-mcp-config.json"
    payload = {"mcpServers": {server_name: dict(definition)}}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _claude_config_path(config: ControlConfig) -> Path:
    if config.claude_config_path is not None:
        return config.claude_config_path
    return Path.home() / ".claude.json"


def _load_operator_mcp_servers(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {
        name: definition for name, definition in servers.items() if isinstance(definition, dict)
    }
