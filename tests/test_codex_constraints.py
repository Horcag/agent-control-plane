from __future__ import annotations

import json
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.codex_watchdog import (
    scan_codex_tool_constraints,
)


def test_codex_tool_budget_stops_after_limit_is_exceeded(tmp_path: Path) -> None:
    event_log = tmp_path / "attempt.events.jsonl"
    _write_events(
        event_log,
        _tool_event("read_file", {"path": "a.py"}),
        _tool_event("read_file", {"path": "b.py"}),
        _tool_event("get_problems", {"path": "b.py"}),
    )

    violation, scan_size, count = scan_codex_tool_constraints(
        event_log,
        0,
        0,
        tool_call_budget=2,
        terminal_tab_name="task-1",
    )

    assert violation == "Codex exceeded the hard tool-call budget of 2 (observed 3)"
    assert scan_size == event_log.stat().st_size
    assert count == 3


def test_terminal_tools_require_exact_task_tab_name(tmp_path: Path) -> None:
    event_log = tmp_path / "attempt.events.jsonl"
    _write_events(
        event_log,
        _tool_event("run_in_terminal", {"command": "pytest", "tab_name": "task-1 (new)"}),
    )

    violation, _, count = scan_codex_tool_constraints(
        event_log,
        0,
        0,
        tool_call_budget=10,
        terminal_tab_name="task-1",
    )

    assert violation == (
        "Terminal tool run_in_terminal must use tab_name='task-1'; received 'task-1 (new)'"
    )
    assert count == 1


def test_terminal_tools_accept_exact_task_tab_name(tmp_path: Path) -> None:
    event_log = tmp_path / "attempt.events.jsonl"
    _write_events(
        event_log,
        _tool_event("run_in_terminal", {"command": "pytest", "tab_name": "task-1"}),
        _tool_event("read_terminal_output", {"tab_name": "task-1"}),
        _tool_event("close_terminal", {"tab_name": "task-1"}),
    )

    violation, _, count = scan_codex_tool_constraints(
        event_log,
        0,
        0,
        tool_call_budget=10,
        terminal_tab_name="task-1",
    )

    assert violation is None
    assert count == 3


def test_native_events_count_towards_tool_budget(tmp_path: Path) -> None:
    event_log = tmp_path / "attempt.events.jsonl"
    _write_events(
        event_log,
        {
            "type": "item.started",
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "tool": "exec",
                "arguments": {"command": "git status"},
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "item-2",
                "type": "file_change",
                "tool": "write",
                "arguments": {"path": "a.py"},
            },
        },
        _tool_event("read_file", {"path": "c.py"}),
    )

    # 1 command_execution + 1 file_change + 1 mcp_tool_call = 3 events total.
    violation, _scan_size, count = scan_codex_tool_constraints(
        event_log,
        0,
        0,
        tool_call_budget=2,
        terminal_tab_name="task-1",
    )

    assert violation == "Codex exceeded the hard tool-call budget of 2 (observed 3)"
    assert count == 3


def test_native_terminal_events_skip_tab_validation(tmp_path: Path) -> None:
    event_log = tmp_path / "attempt.events.jsonl"
    _write_events(
        event_log,
        {
            "type": "item.started",
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "tool": "run_in_terminal",
                "arguments": {"command": "pytest"},
            },
        },
    )

    violation, _, count = scan_codex_tool_constraints(
        event_log,
        0,
        0,
        tool_call_budget=10,
        terminal_tab_name="task-1",
    )

    assert violation is None
    assert count == 1


def _write_events(path: Path, *events: dict[str, object]) -> None:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _tool_event(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "type": "item.started",
        "item": {
            "id": f"item-{tool}",
            "type": "mcp_tool_call",
            "server": "ide-mcp-server",
            "tool": tool,
            "arguments": arguments,
            "status": "in_progress",
        },
    }
