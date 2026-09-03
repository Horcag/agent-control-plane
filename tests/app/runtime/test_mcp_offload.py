from __future__ import annotations

import functools
import inspect
import json
import time
import warnings
from typing import Any
from unittest.mock import Mock, patch

import anyio
import pytest

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import CallToolResult, TextContent
    from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning
except ImportError:
    pytest.skip("mcp is required for MCP offload tests", allow_module_level=True)

from agent_control_plane.app.runtime.mcp_response_budget import (
    MAX_WIRE_BYTES,
    MCP_TOOL_POLICIES,
    response_for,
    serialized_bytes,
)
from agent_control_plane.app.runtime.mcp_server import _validate_tool_policies, build_server


def test_every_registered_tool_is_coroutine_function() -> None:
    with (
        patch(
            "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
            return_value=Mock(),
        ),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("error", IncompleteFieldDefinitionWarning)
        server = build_server()

    tools = server._tool_manager.list_tools()
    assert {tool.name for tool in tools} == set(MCP_TOOL_POLICIES)
    for tool in tools:
        assert inspect.iscoroutinefunction(tool.fn), (
            f"Tool {tool.name} function is not a coroutine function"
        )


def test_policy_matrix_has_valid_detail_parameters() -> None:
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=Mock(),
    ):
        server = build_server()

    assert len(MCP_TOOL_POLICIES) == 52
    _validate_tool_policies(server)


def test_stale_detail_parameter_fails_policy_validation(monkeypatch) -> None:
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=Mock(),
    ):
        server = build_server()
    monkeypatch.setitem(
        MCP_TOOL_POLICIES,
        "agent_smoke",
        MCP_TOOL_POLICIES["agent_smoke"]._replace(detail_param="missing_full"),
    )

    with pytest.raises(RuntimeError, match=r"agent_smoke\.missing_full"):
        _validate_tool_policies(server)


def test_declared_detail_parameters_are_boolean_in_registered_schemas() -> None:
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=Mock(),
    ):
        server = build_server()

    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    for name, policy in MCP_TOOL_POLICIES.items():
        if policy.detail_param is None:
            continue
        properties = tools[name].parameters["properties"]
        assert properties[policy.detail_param]["type"] == "boolean"


def representative_tool(
    name: str,
    count: int = 10,
    ratio: float = 1.5,
    limit: int | None = None,
    flag: bool = False,
) -> dict[str, Any]:
    """A representative tool with various parameter types and docstring."""
    return {"name": name, "count": count, "ratio": ratio, "limit": limit, "flag": flag}


@pytest.mark.anyio
async def test_offload_wrapper_preserves_schema() -> None:
    m_direct = FastMCP("direct")
    m_direct.tool()(representative_tool)

    m_offloaded = FastMCP("offloaded")

    def _offloaded(mcp_inst: Any) -> Any:
        def decorator(fn: Any) -> Any:
            @functools.wraps(fn)
            async def offload(*args: Any, **kwargs: Any) -> Any:
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

            mcp_inst.tool()(offload)
            return offload

        return decorator

    _offloaded(m_offloaded)(representative_tool)

    async with create_connected_server_and_client_session(m_direct._mcp_server) as s1:
        res1 = await s1.list_tools()
    async with create_connected_server_and_client_session(m_offloaded._mcp_server) as s2:
        res2 = await s2.list_tools()

    t1 = res1.tools[0]
    t2 = res2.tools[0]

    assert t1.name == t2.name
    assert t1.description == t2.description
    assert t1.inputSchema == t2.inputSchema
    assert t1.outputSchema == t2.outputSchema


@pytest.mark.anyio
async def test_concurrency_is_restored() -> None:
    m = FastMCP("concurrency_test")

    def _offloaded(mcp_inst: Any) -> Any:
        def decorator(fn: Any) -> Any:
            @functools.wraps(fn)
            async def offload(*args: Any, **kwargs: Any) -> Any:
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

            mcp_inst.tool()(offload)
            return offload

        return decorator

    @_offloaded(m)
    def slow_tool() -> dict[str, Any]:
        time.sleep(0.3)
        return {"ok": True}

    async with create_connected_server_and_client_session(m._mcp_server) as session:
        n_calls = 4
        sleep_duration = 0.3
        start = time.monotonic()
        async with anyio.create_task_group() as tg:
            for _ in range(n_calls):
                tg.start_soon(session.call_tool, "slow_tool", {})
        elapsed = time.monotonic() - start

        assert elapsed < n_calls * sleep_duration * 0.6


@pytest.mark.anyio
async def test_real_fastmcp_rejects_boolean_page_control_before_controller_call() -> None:
    control = Mock()
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl", return_value=control
    ):
        server = build_server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        response = await session.call_tool(
            "agent_plan_snapshot", {"plan_id": "plan-1", "event_limit": True}
        )

    assert response.isError is True
    control.plan_snapshot.assert_not_called()


@pytest.mark.anyio
async def test_real_fastmcp_rejects_strict_bool_before_reconcile_controller_call() -> None:
    control = Mock()
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl", return_value=control
    ):
        server = build_server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        response = await session.call_tool("agent_reconcile", {"terminate_verified_runners": 1})

    assert response.isError is True
    control.reconcile_jobs.assert_not_called()


def test_prebuilt_structured_payload_is_sanitized_to_one_copy() -> None:
    marker = "prebuilt-structured-marker"
    prebuilt = CallToolResult(
        content=[TextContent(type="text", text=marker + ("x" * 50_000))],
        structuredContent={"ok": True, "job_id": "job-1", "payload": marker + ("x" * 50_000)},
    )

    result = response_for("agent_summary_job", prebuilt, {"full": True})

    envelope = json.dumps(result.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)
    assert result is not prebuilt
    assert result.structuredContent == prebuilt.structuredContent
    assert marker not in result.content[0].text
    assert envelope.count(marker) == 1


def test_prebuilt_oversized_mapping_becomes_guard_with_identity_and_follow_up() -> None:
    marker = "prebuilt-guard-marker"
    prebuilt = CallToolResult(
        content=[TextContent(type="text", text=marker)],
        structuredContent={
            "ok": True,
            "job_id": "job-1",
            "status": "running",
            "events": [marker + ("x" * 50_000)],
        },
    )

    result = response_for("agent_status_job", prebuilt, {})

    assert result.structuredContent == {
        "ok": True,
        "status": "running",
        "tool": "agent_status_job",
        "guarded": True,
        "truncated": True,
        "measured_bytes": serialized_bytes(prebuilt.structuredContent),
        "structured_cap_bytes": 48 * 1024,
        "wire_cap_bytes": MAX_WIRE_BYTES,
        "payload_sha256": result.structuredContent["payload_sha256"],
        "job_id": "job-1",
        "events_count": 1,
        "detail_tool": "agent_summary_job",
    }
    assert marker not in result.content[0].text
    assert marker not in json.dumps(result.model_dump(by_alias=True, exclude_none=True))


def test_content_only_prebuilt_error_exposes_metadata_without_raw_content() -> None:
    marker = "content-only-secret"
    prebuilt = CallToolResult(
        content=[TextContent(type="text", text=marker)],
        isError=True,
    )

    result = response_for("agent_status_job", prebuilt, {})

    assert result.isError is True
    assert result.structuredContent == {
        "ok": False,
        "isError": True,
        "source_call_tool_result": True,
        "content_block_count": 1,
        "content_block_type_counts": {"text": 1},
        "content_sha256": result.structuredContent["content_sha256"],
    }
    assert marker not in json.dumps(result.model_dump(by_alias=True, exclude_none=True))


def test_every_policy_guards_hostile_default_payload_without_leaking_marker() -> None:
    marker = "hostile-wire-marker"
    for tool_name, policy in MCP_TOOL_POLICIES.items():
        payload: dict[str, Any] = {
            "ok": True,
            "status": "ready",
            "collection": [{"marker": marker + ("x" * 50_000)}],
        }
        payload.update({key: f"{key}-1" for key in policy.identity_keys})
        arguments = {policy.detail_param: False} if policy.detail_param else {}

        result = response_for(tool_name, payload, arguments)
        wire = json.dumps(result.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)

        assert result.structuredContent["guarded"] is True
        assert result.structuredContent["truncated"] is True
        assert (
            serialized_bytes(result.model_dump(by_alias=True, exclude_none=True)) < MAX_WIRE_BYTES
        )
        assert marker not in result.content[0].text
        assert marker not in wire
        for key in policy.identity_keys:
            assert result.structuredContent[key] == payload[key]
        assert result.structuredContent["collection_count"] == 1
        if policy.mutation:
            assert result.structuredContent["detail_tool"] == policy.follow_up


def test_explicit_detail_policies_keep_payload_in_exactly_one_copy() -> None:
    marker = "explicit-detail-marker"
    for tool_name, policy in MCP_TOOL_POLICIES.items():
        if policy.detail_param is None:
            continue
        result = response_for(
            tool_name,
            {"ok": True, "payload": marker + ("x" * 10_000)},
            {policy.detail_param: True},
        )
        wire = json.dumps(result.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False)

        assert result.structuredContent["payload"].startswith(marker)
        assert marker not in result.content[0].text
        assert wire.count(marker) == 1
