from __future__ import annotations

import functools
import inspect
import time
from typing import Any
from unittest.mock import Mock, patch

import anyio
import pytest

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
except ImportError:
    pytest.skip("mcp is required for MCP offload tests", allow_module_level=True)

from agent_control_plane.app.runtime.mcp_server import build_server


def test_every_registered_tool_is_coroutine_function() -> None:
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=Mock(),
    ):
        server = build_server()

    tools = server._tool_manager.list_tools()
    assert len(tools) >= 48
    for tool in tools:
        assert inspect.iscoroutinefunction(tool.fn), (
            f"Tool {tool.name} function is not a coroutine function"
        )


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
