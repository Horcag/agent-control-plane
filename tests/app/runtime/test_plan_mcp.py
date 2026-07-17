from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent_control_plane.app.runtime.mcp_server import ConfigFreshControl, build_server


class _FakeFastMCP:
    def __init__(self, _name: str) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


def test_config_provider_reloads_before_a_tool_call_and_reports_fingerprints(tmp_path) -> None:
    config_path = tmp_path / "config" / "workspaces.toml"
    config_path.parent.mkdir()
    config_path.write_text("[control]\nslot_root = 'slots-old'\n", encoding="utf-8")
    initial = Mock()
    initial.config = SimpleNamespace(config_path=config_path.resolve())
    initial.list_slots.return_value = [{"path": "slots-old/acp-1"}]
    initial.smoke.return_value = {"ok": True}
    refreshed = Mock()
    refreshed.config = SimpleNamespace(config_path=config_path.resolve())
    refreshed.list_slots.return_value = [{"path": "slots-new/acp-1"}]
    refreshed.smoke.return_value = {"ok": True}

    with patch(
        "agent_control_plane.app.runtime.mcp_server.AgentControlPlane.from_config_path",
        side_effect=[initial, refreshed],
    ) as from_config_path:
        provider = ConfigFreshControl(str(config_path))
        assert provider.list_slots() == [{"path": "slots-old/acp-1"}]
        config_path.write_text("[control]\nslot_root = 'slots-new'\n", encoding="utf-8")

        assert provider.list_slots() == [{"path": "slots-new/acp-1"}]
        smoke = provider.smoke()

    assert from_config_path.call_args_list == [
        ((str(config_path),),),
        ((str(config_path.resolve()),),),
    ]
    assert smoke["config_fingerprint_loaded"] == smoke["config_fingerprint_current"]
    assert smoke["reload_required"] is False
    assert smoke["config_reloaded"] is True


def test_config_provider_does_not_call_stale_control_after_invalid_reload(tmp_path) -> None:
    config_path = tmp_path / "config" / "workspaces.toml"
    config_path.parent.mkdir()
    config_path.write_text("valid = true\n", encoding="utf-8")
    initial = Mock()
    initial.config = SimpleNamespace(config_path=config_path.resolve())

    with patch(
        "agent_control_plane.app.runtime.mcp_server.AgentControlPlane.from_config_path",
        side_effect=[initial, ValueError("invalid replacement config")],
    ):
        provider = ConfigFreshControl(str(config_path))
        config_path.write_text("invalid = [\n", encoding="utf-8")

        with pytest.raises(ValueError, match="invalid replacement config"):
            provider.list_slots()

    initial.list_slots.assert_not_called()


def test_mcp_registers_compact_plan_supervisor_surface(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=object(),
    ):
        server = build_server()

    assert {
        "agent_plan_create",
        "agent_plan_add_task",
        "agent_plan_bind_job",
        "agent_plan_snapshot",
        "agent_plan_watch",
        "agent_plan_accept_task",
        "agent_plan_reject_task",
        "agent_plan_dispatch",
        "agent_plan_run_until_review",
        "agent_plan_retry_task",
        "agent_plan_cancel",
        "agent_plan_archive",
        "agent_plan_list",
        "agent_retention_gc",
    }.issubset(server.tools)


def test_mcp_registers_durable_handoff_and_checkpoint_surface(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=object(),
    ):
        server = build_server()

    assert {
        "agent_review_inbox_list",
        "agent_review_inbox_get",
        "agent_review_inbox_resolve",
        "agent_accept_handoff",
        "agent_sync_subagent_results",
        "agent_slots_checkpoint",
        "agent_reconcile",
    }.issubset(server.tools)


def test_mcp_reconcile_requires_explicit_verified_runner_termination(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    control = Mock()
    control.reconcile_jobs.return_value = {"terminated_orphan_runners": ["job-1"]}

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=control,
    ):
        server = build_server()

    response = server.tools["agent_reconcile"](
        job_id="job-1",
        terminate_verified_runners=True,
    )

    assert response == {"terminated_orphan_runners": ["job-1"]}
    control.reconcile_jobs.assert_called_once_with(
        "job-1",
        terminate_verified_runners=True,
    )


def test_mcp_start_plumbs_workspace_access(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    control = Mock()
    control.start_job.return_value = SimpleNamespace(
        job_id="job-native",
        status="queued",
        run_dir=Path("runs/job-native"),
        result_path=Path("tasks/native/result.md"),
        backend="codex",
        agy_model=None,
        codex_model="gpt-5",
        codex_reasoning_effort="low",
        codex_quality_tier="mechanical",
        workspace_access="native",
        worker_pid=123,
        runner_pid=None,
        read_only=False,
        slot_name="app-1",
    )

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=control,
    ):
        server = build_server()

    response = server.tools["agent_start_job"](
        task_id="native",
        route="app",
        backend="codex",
        workspace_access="native",
    )
    options = control.start_job.call_args.args[0]

    assert options.workspace_access == "native"
    assert response["workspace_access"] == "native"
