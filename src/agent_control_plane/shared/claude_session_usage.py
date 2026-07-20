from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_control_plane.shared.codex_session_usage import SessionUsageSnapshot, TokenUsage


def claude_project_dir_name(workspace_path: Path) -> str:
    """Sanitize a workspace path the way Claude Code names its project directories."""
    normalized = str(workspace_path.resolve(strict=False))
    return "".join(char if char.isalnum() else "-" for char in normalized)


def claude_session_path(
    sessions_root: Path,
    workspace_path: Path,
    session_id: str,
) -> Path:
    return sessions_root / claude_project_dir_name(workspace_path) / f"{session_id}.jsonl"


def latest_claude_session_usage(path: Path) -> SessionUsageSnapshot | None:
    """Sum per-request Claude usage into one cumulative session snapshot.

    Claude Code transcripts carry per-API-call usage on assistant records, not
    cumulative token_count events, so the file must be summed forward; the
    append-only transcript keeps the resulting totals monotonic for span deltas.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    usage_by_message: dict[str, TokenUsage] = {}
    recorded_at: str | None = None
    for line in lines:
        entry = _parse_entry(line)
        if entry is None or entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = claude_usage_from_mapping(message.get("usage"))
        if usage is None:
            continue
        message_id = message.get("id")
        key = message_id if isinstance(message_id, str) and message_id else str(entry.get("uuid"))
        usage_by_message[key] = usage
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            recorded_at = timestamp
    if not usage_by_message:
        return None
    total = TokenUsage(0, 0, 0, 0)
    for usage in usage_by_message.values():
        total = TokenUsage(
            input_tokens=total.input_tokens + usage.input_tokens,
            cached_input_tokens=total.cached_input_tokens + usage.cached_input_tokens,
            output_tokens=total.output_tokens + usage.output_tokens,
            reasoning_output_tokens=0,
        )
    return SessionUsageSnapshot(usage=total, recorded_at=recorded_at)


def claude_usage_from_mapping(value: Any) -> TokenUsage | None:
    """Map Anthropic usage fields onto the controller's total-input convention.

    Anthropic reports uncached input separately from cache reads and cache
    writes; the controller schema treats cached tokens as a subset of input, so
    input becomes the sum of all three and cache writes stay in the uncached
    bucket (they bill above the base input rate).
    """
    if not isinstance(value, dict):
        return None
    uncached_input = _integer(value.get("input_tokens"))
    cache_read = _integer(value.get("cache_read_input_tokens"))
    cache_creation = _integer(value.get("cache_creation_input_tokens"))
    output = _integer(value.get("output_tokens"))
    if uncached_input == 0 and cache_read == 0 and cache_creation == 0 and output == 0:
        return None
    return TokenUsage(
        input_tokens=uncached_input + cache_read + cache_creation,
        cached_input_tokens=cache_read,
        output_tokens=output,
        reasoning_output_tokens=0,
    )


def _parse_entry(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _integer(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0
