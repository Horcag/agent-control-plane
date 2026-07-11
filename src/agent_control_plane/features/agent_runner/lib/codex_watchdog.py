from __future__ import annotations

import time
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.runner import AgentRunSpec
from agent_control_plane.shared.git_tools import GitError, workspace_state

CODEX_TOOL_TIMEOUT_LIMIT = 2
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
    return any(marker in text for marker in CODEX_PRODUCTIVE_LOG_MARKERS), next_scan_size


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


def progress_signature(spec: AgentRunSpec) -> tuple[tuple[str, ...] | None, bool]:
    """Return durable progress markers and whether target workspace is dirty."""
    progress_path = spec.result_path.parent / "agent-progress.md"
    markers: list[str] = []
    for path in (progress_path, spec.result_path):
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
            markers.append(f"git-status:{state.porcelain}")
            markers.extend(dirty_file_markers_from_porcelain(spec.workspace_path, state.porcelain))
    except GitError as exc:
        markers.append(f"git-error:{type(exc).__name__}:{exc}")

    signature = tuple(markers) if markers else None
    return signature, workspace_dirty


def dirty_file_markers_from_porcelain(workspace_path: Path, porcelain: str) -> list[str]:
    markers: list[str] = []
    for line in porcelain.splitlines():
        path_text = porcelain_changed_path(line)
        if not path_text:
            continue
        path = workspace_path / path_text
        try:
            stat = path.stat()
        except OSError:
            continue
        markers.append(f"dirty-file:{path_text}:{stat.st_mtime_ns}:{stat.st_size}")
    return markers


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
