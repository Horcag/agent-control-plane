from __future__ import annotations

import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent_control_plane.app.runtime import mcp_server
from agent_control_plane.app.runtime.mcp_server import (
    ConfigFreshControl,
    ConfigFreshnessError,
    build_server,
)
from agent_control_plane.app.runtime.orchestrator import PolicyError
from agent_control_plane.entities.slot import SlotStore


class _FakeFastMCP:
    def __init__(self, _name: str) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class _SyncingSlots:
    def __init__(self, store: SlotStore, path: Path) -> None:
        self._store = store
        self._path = path
        self._sync_guard = nullcontext

    def set_configured_slots_sync_guard(self, guard) -> None:
        self._sync_guard = guard

    def sync_configured_slots(self) -> None:
        with self._sync_guard():
            self._store.register_slot("acp-1", "acp", self._path)


class _SyncingControl:
    def __init__(self, config_path: Path, store: SlotStore, slot_path: Path) -> None:
        self.config = SimpleNamespace(config_path=config_path)
        self.slots = _SyncingSlots(store, slot_path)

    def list_slots(self) -> list[dict[str, str]]:
        self.slots.sync_configured_slots()
        return [{"path": str(self.slots._path)}]


def test_mcp_plan_execution_spec_validates_controller_contract() -> None:
    execution = mcp_server._plan_execution_spec(
        {
            "route": "acp",
            "brief": "Focused task",
            "expected_result_status": "partial",
            "controller_gate_mode": "focused",
            "expected_base_sha": "A" * 40,
            "effective_scope": [" tests/api.py ", "src/api.py", "src/api.py"],
            "codex_tool_call_budget": 47,
            "retry_override_reason": " approved retry ",
        }
    )

    assert execution is not None
    assert execution.expected_result_status == "partial"
    assert execution.controller_gate_mode == "focused"
    assert execution.expected_base_sha == "a" * 40
    assert execution.effective_scope == ("src/api.py", "tests/api.py")
    assert execution.codex_tool_call_budget == 47
    assert execution.retry_override_reason == "approved retry"

    with pytest.raises(ValueError, match="expected_result_status"):
        mcp_server._plan_execution_spec(
            {"route": "acp", "brief": "Invalid task", "expected_result_status": "unexpected"}
        )


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

    assert [call.args[0] for call in from_config_path.call_args_list] == [
        str(config_path.resolve()),
        str(config_path.resolve()),
    ]
    assert [
        call.kwargs["config_contents"].replace(b"\r\n", b"\n")
        for call in from_config_path.call_args_list
    ] == [b"[control]\nslot_root = 'slots-old'\n", b"[control]\nslot_root = 'slots-new'\n"]
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


def test_config_provider_stable_load_retries_when_config_changes_during_construction(
    tmp_path,
) -> None:
    config_path = tmp_path / "config" / "workspaces.toml"
    config_path.parent.mkdir()
    config_path.write_text("slot = 'old'\n", encoding="utf-8")
    old_control = Mock()
    old_control.config = SimpleNamespace(config_path=config_path.resolve())
    old_control.list_slots.return_value = [{"path": "old"}]
    new_control = Mock()
    new_control.config = SimpleNamespace(config_path=config_path.resolve())
    new_control.list_slots.return_value = [{"path": "new"}]

    def load_control(*_args, **_kwargs):
        if load_control.calls == 0:
            load_control.calls += 1
            config_path.write_text("slot = 'new'\n", encoding="utf-8")
            return old_control
        return new_control

    load_control.calls = 0
    with patch(
        "agent_control_plane.app.runtime.mcp_server.AgentControlPlane.from_config_path",
        side_effect=load_control,
    ):
        provider = ConfigFreshControl(str(config_path))

    assert provider.list_slots() == [{"path": "new"}]
    old_control.list_slots.assert_not_called()


def test_config_provider_rejects_old_control_before_it_overwrites_new_sqlite_slot_path(
    tmp_path,
) -> None:
    config_path = tmp_path / "config" / "workspaces.toml"
    config_path.parent.mkdir()
    database_path = tmp_path / "runs" / "jobs.sqlite3"
    old_slot = tmp_path / "slots-old" / "acp-1"
    new_slot = tmp_path / "slots-new" / "acp-1"
    config_path.write_text("slot = 'old'\n", encoding="utf-8")
    store = SlotStore(database_path)
    old_control = _SyncingControl(config_path.resolve(), store, old_slot)
    new_control = _SyncingControl(config_path.resolve(), store, new_slot)

    with patch(
        "agent_control_plane.app.runtime.mcp_server.AgentControlPlane.from_config_path",
        side_effect=[old_control, new_control],
    ):
        old_process = ConfigFreshControl(str(config_path))
        selected_old_control = old_process._fresh_control()
        config_path.write_text("slot = 'new'\n", encoding="utf-8")

        new_process = ConfigFreshControl(str(config_path))
        assert new_process.list_slots() == [{"path": str(new_slot)}]
        with pytest.raises(ConfigFreshnessError, match="configuration changed"):
            selected_old_control.list_slots()

    with sqlite3.connect(database_path) as database:
        stored_path = database.execute("select path from slots where name = 'acp-1'").fetchone()[0]
    assert stored_path == str(new_slot)


def test_config_lock_times_out_after_repeated_contention(monkeypatch, tmp_path) -> None:
    lock_path = tmp_path / "config.lock"
    lock_file = Mock()
    lock_file.fileno.return_value = 42
    lock_attempt = Mock(side_effect=OSError("lock is busy"))
    monotonic = Mock(side_effect=[0.0, 1.0, 2.0])
    sleep = Mock()

    if sys.platform == "win32":
        monkeypatch.setattr(mcp_server.msvcrt, "locking", lock_attempt)
    else:
        monkeypatch.setattr(mcp_server.fcntl, "flock", lock_attempt)
    monkeypatch.setattr(mcp_server.time, "monotonic", monotonic)
    monkeypatch.setattr(mcp_server.time, "sleep", sleep)

    with pytest.raises(ConfigFreshnessError) as exc_info:
        mcp_server._acquire_config_lock(lock_file, lock_path)

    assert "timed out after 2.0s acquiring configured-slot config lock" in str(exc_info.value)
    assert str(lock_path) in str(exc_info.value)
    assert lock_attempt.call_count == 2
    sleep.assert_called_once_with(mcp_server._CONFIG_LOCK_RETRY_SEC)


def test_config_lock_file_stays_one_byte_after_repeated_acquisition(tmp_path) -> None:
    config_path = tmp_path / "config" / "workspaces.toml"
    lock_path = mcp_server._config_lock_path(config_path)

    with mcp_server._interprocess_config_lock(config_path):
        pass
    with mcp_server._interprocess_config_lock(config_path):
        pass

    assert lock_path.read_bytes() == b"\0"


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
        "agent_model_catalog",
        "agent_model_routing_explain",
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


def test_mcp_model_catalog_refreshes_after_config_change(monkeypatch, tmp_path: Path) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    config_path = tmp_path / "workspaces.toml"
    config_path.write_text("version = 'old'\n", encoding="utf-8")
    initial = Mock()
    initial.config = SimpleNamespace(config_path=config_path.resolve())
    initial.model_catalog_inspection.return_value = {"version": "old"}
    refreshed = Mock()
    refreshed.config = SimpleNamespace(config_path=config_path.resolve())
    refreshed.model_catalog_inspection.return_value = {"version": "new"}

    with patch(
        "agent_control_plane.app.runtime.mcp_server.AgentControlPlane.from_config_path",
        side_effect=[initial, refreshed],
    ) as from_config_path:
        server = build_server(str(config_path))
        assert server.tools["agent_model_catalog"]() == {"version": "old"}
        config_path.write_text("version = 'new'\n", encoding="utf-8")
        assert server.tools["agent_model_catalog"]() == {"version": "new"}

    assert from_config_path.call_count == 2
    initial.model_catalog_inspection.assert_called_once_with()
    refreshed.model_catalog_inspection.assert_called_once_with()


def test_mcp_model_routing_explain_delegates_and_returns_clean_errors(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    payload = {"route": "main", "policy": "adaptive", "selection_source": "history"}
    control = Mock()
    control.model_routing_explain.return_value = payload

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=control,
    ):
        server = build_server()
        assert server.tools["agent_model_routing_explain"]("adaptive", "main") == payload
        control.model_routing_explain.side_effect = PolicyError("Unknown route: missing")
        assert server.tools["agent_model_routing_explain"]("adaptive", "missing") == {
            "ok": False,
            "error": "Unknown route: missing",
        }

    control.model_routing_explain.assert_called_with("adaptive", "missing")


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
        codex_premium_override_reason=None,
        workspace_access="native",
        worker_pid=123,
        runner_pid=None,
        read_only=False,
        slot_name="app-1",
        expected_result_status="completed",
        controller_gate_mode="full",
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
    assert (options.expected_result_status, options.controller_gate_mode) == ("completed", "full")


def test_mcp_start_forwards_premium_override_reason(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    control = Mock()
    control.start_job.return_value = SimpleNamespace(
        job_id="job-premium",
        status="queued",
        run_dir=Path("runs/job-premium"),
        result_path=Path("tasks/premium/result.md"),
        backend="codex",
        agy_model=None,
        codex_model="gpt-5.6-sol",
        codex_reasoning_effort="medium",
        codex_quality_tier=None,
        codex_premium_override_reason="approved benchmark",
        workspace_access="native",
        worker_pid=123,
        runner_pid=None,
        read_only=False,
        slot_name="app-1",
        expected_result_status="partial",
        controller_gate_mode="focused",
    )

    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl",
        return_value=control,
    ):
        server = build_server()

    response = server.tools["agent_start_job"](
        task_id="premium",
        route="app",
        backend="codex",
        codex_model="gpt-5.6-sol",
        codex_premium_override_reason="approved benchmark",
        workspace_access="native",
        expected_result_status="partial",
        controller_gate_mode="focused",
    )

    options = control.start_job.call_args.args[0]
    assert options.codex_premium_override_reason == "approved benchmark"
    assert response["codex_premium_override_reason"] == "approved benchmark"
    assert response["expected_result_status"] == "partial"


def test_mcp_start_rejects_invalid_controller_contract(monkeypatch) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    control = Mock()
    with patch(
        "agent_control_plane.app.runtime.mcp_server.ConfigFreshControl", return_value=control
    ):
        server = build_server()

    response = server.tools["agent_start_job"](
        task_id="invalid", route="app", expected_result_status="unexpected"
    )

    assert response["ok"] is False
    control.start_job.assert_not_called()
