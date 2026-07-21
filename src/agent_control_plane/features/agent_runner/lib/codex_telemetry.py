from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.features.agent_runner.lib.model_catalog import CatalogRate, ModelCatalog

_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "image_generation",
    "web_search",
}


def parse_codex_jsonl(
    path: Path,
    *,
    model: str,
    duration_sec: float,
    sessions_root: Path | None = None,
    catalog: ModelCatalog | None = None,
) -> AttemptMetrics:
    event_count = 0
    thread_id: str | None = None
    turn_completed = False
    usage_available = False
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    reasoning_output_tokens = 0
    tool_calls = 0
    failed_tool_calls = 0
    error_events = 0
    tool_counts: Counter[str] = Counter()

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []

    for line in lines:
        event = _parse_event(line)
        if event is None:
            continue
        event_count += 1
        event_type = str(event.get("type") or "")
        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        if event_type in {"error", "turn.failed"}:
            error_events += 1
        if event_type == "turn.completed":
            turn_completed = True
            usage = event.get("usage")
            if isinstance(usage, dict):
                usage_available = True
                input_tokens += _integer(usage.get("input_tokens"))
                cached_input_tokens += _integer(usage.get("cached_input_tokens"))
                output_tokens += _integer(usage.get("output_tokens"))
                reasoning_output_tokens += _integer(usage.get("reasoning_output_tokens"))
        if event_type != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        tool_key = _tool_key(item)
        if tool_key is None:
            continue
        tool_calls += 1
        tool_counts[tool_key] += 1
        if item.get("error") or str(item.get("status") or "") in {"error", "failed"}:
            failed_tool_calls += 1

    if not usage_available and thread_id and sessions_root is not None:
        recovered = _recover_session_usage(sessions_root, thread_id)
        if recovered is not None:
            usage_available = True
            input_tokens = recovered["input_tokens"]
            cached_input_tokens = recovered["cached_input_tokens"]
            output_tokens = recovered["output_tokens"]
            reasoning_output_tokens = recovered["reasoning_output_tokens"]

    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
    rate_metadata = catalog.rate_metadata_for(model) if catalog is not None else None
    estimated_credits = _estimate(
        rate_metadata.credit_rate if rate_metadata is not None else None,
        usage_available=usage_available,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    estimated_api_usd = _estimate(
        rate_metadata.api_usd_rate if rate_metadata is not None else None,
        usage_available=usage_available,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )

    return AttemptMetrics(
        duration_sec=max(0.0, duration_sec),
        thread_id=thread_id,
        event_count=event_count,
        turn_completed=turn_completed,
        usage_available=usage_available,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        tool_calls=tool_calls,
        failed_tool_calls=failed_tool_calls,
        error_events=error_events,
        tool_counts=tuple(sorted(tool_counts.items())),
        estimated_credits=estimated_credits,
        estimated_api_usd=estimated_api_usd,
        rate_card_version=(
            rate_metadata.rate_card_version
            if rate_metadata is not None and rate_metadata.rate_card_version is not None
            else "unknown"
        ),
        event_log_path=path,
        cache_creation_input_tokens=0,
    )


def codex_turn_completed(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(
        (event := _parse_event(line)) is not None and event.get("type") == "turn.completed"
        for line in lines
    )


def render_codex_json_line(line: str) -> str:
    event = _parse_event(line)
    if event is None:
        return line.rstrip("\r\n") + "\n"

    event_type = str(event.get("type") or "event")
    item = event.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type") or "item")
        state = (
            "started" if event_type == "item.started" else str(item.get("status") or "completed")
        )
        if item_type == "mcp_tool_call":
            return f"mcp: {item.get('server')}/{item.get('tool')} {state}\n"
        if item_type == "command_execution":
            return f"exec\n{item.get('command') or ''}\n[{state}]\n"
        if item_type == "web_search":
            return f"web search: {item.get('query') or item.get('url') or ''} [{state}]\n"
        if item_type == "agent_message":
            return str(item.get("text") or "").rstrip("\r\n") + "\n"
        return f"{item_type}: {state}\n"
    if event_type == "turn.completed":
        return f"turn.completed usage={json.dumps(event.get('usage') or {}, sort_keys=True)}\n"
    if event_type in {"error", "turn.failed"}:
        message = event.get("message") or event.get("error") or "unknown error"
        return f"ERROR {message}\n"
    return f"{event_type}\n"


def _recover_session_usage(
    sessions_root: Path,
    thread_id: str,
) -> dict[str, int] | None:
    if not sessions_root.exists():
        return None
    candidates = sorted(
        sessions_root.rglob(f"*{thread_id}*.jsonl"),
        key=_safe_mtime,
        reverse=True,
    )
    for path in candidates[:4]:
        usage = _latest_session_usage(path)
        if usage is not None:
            return usage
    return None


def _latest_session_usage(path: Path) -> dict[str, int] | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    latest_total: dict[str, int] | None = None
    for line in lines:
        event = _parse_event(line)
        if event is None or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        last_usage = _usage_mapping(info.get("last_token_usage"))
        if last_usage is not None:
            latest_total = last_usage
            continue
        total_usage = _usage_mapping(info.get("total_token_usage"))
        if total_usage is not None:
            latest_total = total_usage
    return latest_total


def _usage_mapping(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        "input_tokens": _integer(value.get("input_tokens")),
        "cached_input_tokens": _integer(value.get("cached_input_tokens")),
        "output_tokens": _integer(value.get("output_tokens")),
        "reasoning_output_tokens": _integer(value.get("reasoning_output_tokens")),
    }


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _parse_event(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _tool_key(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type") or "")
    if item_type == "mcp_tool_call":
        return f"mcp:{item.get('server')}/{item.get('tool')}"
    if item_type in _TOOL_ITEM_TYPES or item_type.endswith("_tool_call"):
        return item_type
    return None


def _integer(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _estimate(
    rates: CatalogRate | None,
    *,
    usage_available: bool,
    uncached_input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> float | None:
    if rates is None or not usage_available:
        return None
    total = (
        uncached_input_tokens * rates.input
        + cached_input_tokens * rates.cached_input
        + output_tokens * rates.output
    )
    return total / 1_000_000
