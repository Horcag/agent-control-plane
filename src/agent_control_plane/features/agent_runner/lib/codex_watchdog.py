from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.features.agent_runner.lib.runner import AgentRunSpec, BudgetLifecycleEvent
from agent_control_plane.shared.git_tools import GitError, workspace_state
from agent_control_plane.shared.path_rules import is_known_temporary_patch_artifact

CODEX_TOOL_TIMEOUT_LIMIT = 2
# Consecutive tool calls without any durable-progress signature change before the worker is
# stopped. Resets to 0 on any progress (repo tree dirtied, agent-progress.md/result.md/
# verification.json touched). A reference-heavy task on an unfamiliar codebase (build a full
# new slice: read the ingest/HTTP/reused-feature APIs + a reference slice, then implement)
# routinely reads 30-50 files before its first write; 16 and even 50 killed legitimate workers
# mid-exploration. 200 leaves ample room while still catching a genuinely stuck/looping worker
# (200 CONSECUTIVE no-progress calls — real work interleaves writes and resets the counter).
CODEX_INEFFICIENT_TOOL_USAGE_LIMIT = 200
CODEX_TOOL_TIMEOUT_MARKER = "Exit code: 124"
CODEX_FORBIDDEN_TOOL_MARKERS_BY_NAME: dict[str, str] = {
    "web_search": "\nweb search:",
    "raw_exec": "\nexec\n",
    "codex_list_mcp_resources": "mcp: codex/list_mcp_resources",
    "codex_list_mcp_resource_templates": "mcp: codex/list_mcp_resource_templates",
}
CODEX_FORBIDDEN_AGENTBRIDGE_TOOLS_BY_NAME: dict[str, str] = {
    "agentbridge_global_search": "search_text",
    "agentbridge_global_symbols": "search_symbols",
    "agentbridge_global_files": "list_project_files",
    "agentbridge_global_tree": "list_directory_tree",
    "agentbridge_external_attach": "attach_external_dir",
}
CODEX_PRODUCTIVE_LOG_MARKERS = ("mcp: agentbridge_",)
CODEX_TERMINAL_TOOLS = frozenset(
    {
        "run_in_terminal",
        "read_terminal_output",
        "write_terminal_input",
        "close_terminal",
    }
)
CODEX_DISCOVERY_TOOLS = frozenset(
    {
        "read_file",
        "search_text",
        "search_symbols",
        "list_project_files",
        "list_directory_tree",
        "get_problems",
    }
)


@dataclass(frozen=True)
class ToolBudgetPolicy:
    hard_limit: int
    reserved_calls: int
    discretionary_calls: int
    warning_threshold: int
    handoff_threshold: int


@dataclass(frozen=True)
class BudgetLifecycleScan:
    scan_size: int
    tool_call_count: int
    events: tuple[BudgetLifecycleEvent, ...]
    violation: str | None


def tool_budget_policy(tool_call_budget: int) -> ToolBudgetPolicy | None:
    if tool_call_budget <= 0:
        return None
    reserved = min(16, max(0, tool_call_budget - 1))
    discretionary = max(1, tool_call_budget - reserved)
    warning = _ceil_fraction(discretionary, 80)
    handoff = max(warning, _ceil_fraction(discretionary, 90))
    return ToolBudgetPolicy(tool_call_budget, reserved, discretionary, warning, handoff)


def refresh_log_activity(
    spec: AgentRunSpec,
    last_output_mono: float,
    last_log_size: int,
) -> tuple[float, int]:
    try:
        current_size = spec.log_path.stat().st_size
    except OSError:
        return last_output_mono, last_log_size
    if current_size != last_log_size:
        return time.monotonic(), current_size
    return last_output_mono, last_log_size


def productive_log_activity_if_needed(
    spec: AgentRunSpec,
    scan_size: int,
) -> tuple[bool, int]:
    text, next_scan_size = _read_new_log_text(spec.log_path, scan_size)
    if any(marker in text for marker in CODEX_PRODUCTIVE_LOG_MARKERS):
        return True, next_scan_size
    if spec.workspace_access == "native" and "\nexec\n" in text:
        return True, next_scan_size
    return False, next_scan_size


def scan_tool_timeouts(
    log_path: Path,
    scan_size: int,
    timeout_count: int,
) -> tuple[bool, int, int]:
    text, next_scan_size = _read_new_log_text(log_path, scan_size)
    timeout_count += text.count(CODEX_TOOL_TIMEOUT_MARKER)
    return timeout_count >= CODEX_TOOL_TIMEOUT_LIMIT, next_scan_size, timeout_count


def scan_forbidden_tool(
    log_path: Path,
    scan_size: int,
    marker_names: tuple[str, ...],
) -> tuple[tuple[str, str] | None, int]:
    if not marker_names:
        return None, scan_size

    text, next_scan_size = _read_new_log_text(log_path, scan_size)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for name in marker_names:
        agentbridge_tool = CODEX_FORBIDDEN_AGENTBRIDGE_TOOLS_BY_NAME.get(name)
        if agentbridge_tool is not None:
            marker = f"mcp: agentbridge_*/{agentbridge_tool}"
            if any(
                "mcp: agentbridge_" in line and f"/{agentbridge_tool}" in line
                for line in normalized.splitlines()
            ):
                return (name, marker), next_scan_size
            continue

        marker = CODEX_FORBIDDEN_TOOL_MARKERS_BY_NAME.get(name, name)
        if marker in normalized:
            return (name, marker), next_scan_size
    return None, next_scan_size


def scan_codex_tool_constraints(
    event_log_path: Path,
    scan_size: int,
    tool_call_count: int,
    *,
    tool_call_budget: int,
    terminal_tab_name: str | None,
) -> tuple[str | None, int, int]:
    """Count started tools and reject terminal calls that can leak across jobs."""
    text, next_scan_size = _read_new_log_text(event_log_path, scan_size)
    for line in text.splitlines():
        event = _json_object(line)
        if event is None or event.get("type") != "item.started":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {"mcp_tool_call", "command_execution", "file_change"}:
            continue
        tool_call_count += 1
        tool = str(item.get("tool") or "")
        arguments = item.get("arguments")
        if (
            item_type == "mcp_tool_call"
            and tool in CODEX_TERMINAL_TOOLS
            and terminal_tab_name is not None
        ):
            tab_name = arguments.get("tab_name") if isinstance(arguments, dict) else None
            if tab_name != terminal_tab_name:
                return (
                    f"Terminal tool {tool} must use tab_name={terminal_tab_name!r}; "
                    f"received {tab_name!r}",
                    next_scan_size,
                    tool_call_count,
                )
        if tool_call_budget > 0 and tool_call_count > tool_call_budget:
            return (
                f"Codex exceeded the hard tool-call budget of {tool_call_budget} "
                f"(observed {tool_call_count})",
                next_scan_size,
                tool_call_count,
            )
    return None, next_scan_size, tool_call_count


def scan_budget_lifecycle(
    event_log_path: Path,
    scan_size: int,
    tool_call_count: int,
    *,
    policy: ToolBudgetPolicy | None,
    warning_emitted: bool,
    handoff_emitted: bool,
) -> BudgetLifecycleScan:
    """Emit phase transitions once and reject clearly-discovery work after handoff."""
    text, next_scan_size = _read_new_log_text(event_log_path, scan_size)
    events: list[BudgetLifecycleEvent] = []
    for line in text.splitlines():
        item = _started_tool_item(line)
        if item is None:
            continue
        tool_call_count += 1
        if policy is None:
            continue
        already_in_handoff = handoff_emitted
        if not warning_emitted and tool_call_count >= policy.warning_threshold:
            warning_emitted = True
            events.append(
                BudgetLifecycleEvent(
                    "budget_warning",
                    tool_call_count,
                    f"Tool budget warning at call {tool_call_count}; reserve {policy.reserved_calls} calls",
                )
            )
        if not handoff_emitted and tool_call_count >= policy.handoff_threshold:
            handoff_emitted = True
            events.append(
                BudgetLifecycleEvent(
                    "budget_handoff",
                    tool_call_count,
                    f"Tool budget handoff at call {tool_call_count}; finalize with reserve",
                )
            )
        if already_in_handoff and _is_discovery_item(item):
            return BudgetLifecycleScan(
                next_scan_size,
                tool_call_count,
                tuple(events),
                f"Codex started new discovery after budget handoff at call {tool_call_count}",
            )
    return BudgetLifecycleScan(next_scan_size, tool_call_count, tuple(events), None)


def _started_tool_item(line: str) -> dict[str, Any] | None:
    event = _json_object(line)
    if event is None or event.get("type") != "item.started":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") not in {"mcp_tool_call", "command_execution", "file_change"}:
        return None
    return item


def _is_discovery_item(item: dict[str, Any]) -> bool:
    tool = str(item.get("tool") or "")
    if tool in CODEX_DISCOVERY_TOOLS:
        return True
    arguments = item.get("arguments")
    command = arguments.get("command") if isinstance(arguments, dict) else None
    if not isinstance(command, str):
        return False
    normalized = command.lstrip().lower()
    return normalized.startswith(("rg ", "rg.exe ", "get-content "))


def _ceil_fraction(value: int, percentage: int) -> int:
    return (value * percentage + 99) // 100


def progress_signature(spec: AgentRunSpec) -> tuple[tuple[str, ...] | None, bool]:
    """Return durable progress markers and whether target workspace is dirty."""
    progress_path = spec.result_path.parent / "agent-progress.md"
    markers: list[str] = []
    verification_path = spec.result_path.with_name("verification.json")
    for path in (progress_path, spec.result_path, verification_path):
        try:
            stat = path.stat()
        except OSError:
            continue
        markers.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")

    workspace_dirty = False
    try:
        state = workspace_state(spec.workspace_path)
        workspace_dirty = state.dirty
        if state.porcelain.strip():
            markers.extend(dirty_file_markers_from_porcelain(spec.workspace_path, state.porcelain))
    except GitError as exc:
        markers.append(f"git-error:{type(exc).__name__}:{exc}")

    signature = tuple(markers) if markers else None
    return signature, workspace_dirty


def dirty_file_markers_from_porcelain(workspace_path: Path, porcelain: str) -> list[str]:
    markers: list[str] = []
    for line in porcelain.splitlines():
        path_text = porcelain_changed_path(line)
        if not path_text or is_known_temporary_patch_artifact(path_text):
            continue
        path = workspace_path / path_text
        try:
            stat = path.stat()
        except OSError:
            markers.append(f"dirty-file:{path_text}:missing")
            continue
        markers.append(f"dirty-file:{path_text}:{stat.st_mtime_ns}:{stat.st_size}")
    return markers


def updated_calls_without_durable_progress(
    previous_signature: tuple[str, ...] | None,
    current_signature: tuple[str, ...] | None,
    consecutive_calls: int,
    new_calls: int,
) -> int:
    if current_signature != previous_signature:
        return 0
    return consecutive_calls + new_calls


def porcelain_changed_path(line: str) -> str | None:
    if len(line) < 4:
        return None
    path_text = line[3:]
    if " -> " in path_text:
        path_text = path_text.rsplit(" -> ", maxsplit=1)[-1]
    return path_text.strip().strip('"') or None


def _read_new_log_text(log_path: Path, scan_size: int) -> tuple[str, int]:
    try:
        with log_path.open("rb") as handle:
            handle.seek(scan_size)
            chunk = handle.read()
            next_scan_size = handle.tell()
    except OSError:
        return "", scan_size
    return chunk.decode("utf-8", errors="replace"), next_scan_size


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
