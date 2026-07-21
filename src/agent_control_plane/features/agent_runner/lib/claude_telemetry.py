from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.features.agent_runner.lib.model_catalog import CatalogRate, ModelCatalog
from agent_control_plane.shared.claude_session_usage import (
    claude_session_path,
    claude_usage_from_mapping,
    latest_claude_session_usage,
)
from agent_control_plane.shared.codex_session_usage import TokenUsage

CLAUDE_CLI_RATE_CARD_VERSION = "claude-code-cli"
_RESULT_ERROR_SUBTYPES = frozenset({"error_max_turns", "error_during_execution"})


def parse_claude_jsonl(
    path: Path,
    *,
    model: str,
    duration_sec: float,
    sessions_root: Path | None = None,
    workspace_path: Path | None = None,
    catalog: ModelCatalog | None = None,
    session_id_hint: str | None = None,
) -> AttemptMetrics:
    event_count = 0
    thread_id: str | None = None
    turn_completed = False
    result_usage: TokenUsage | None = None
    result_cache_creation_input_tokens = 0
    total_cost_usd: float | None = None
    message_usage: dict[str, TokenUsage] = {}
    message_cache_creation_input_tokens: dict[str, int] = {}
    error_events = 0
    tool_counts: Counter[str] = Counter()
    seen_tool_use_ids: set[str] = set()
    failed_tool_use_ids: set[str] = set()

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
        if thread_id is None and isinstance(event.get("session_id"), str):
            thread_id = event["session_id"]
        if event_type == "result":
            subtype = str(event.get("subtype") or "")
            if subtype == "success" and event.get("is_error") is not True:
                turn_completed = True
            else:
                error_events += 1
            usage = claude_usage_from_mapping(event.get("usage"))
            if usage is not None:
                result_usage = usage
                result_cache_creation_input_tokens = _cache_creation_from_mapping(
                    event.get("usage")
                )
            cost = event.get("total_cost_usd")
            if isinstance(cost, int | float):
                total_cost_usd = float(cost)
            continue
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            usage = claude_usage_from_mapping(message.get("usage"))
            if usage is not None:
                message_id = message.get("id")
                key = message_id if isinstance(message_id, str) and message_id else line[:96]
                message_usage[key] = usage
                message_cache_creation_input_tokens[key] = _cache_creation_from_mapping(
                    message.get("usage")
                )
            for block in _content_blocks(message):
                if block.get("type") != "tool_use":
                    continue
                block_id = block.get("id")
                if isinstance(block_id, str) and block_id:
                    if block_id in seen_tool_use_ids:
                        continue
                    seen_tool_use_ids.add(block_id)
                tool_counts[_claude_tool_key(str(block.get("name") or "unknown"))] += 1
            continue
        if event_type == "user":
            for block in _content_blocks(event.get("message")):
                if block.get("type") != "tool_result" or block.get("is_error") is not True:
                    continue
                tool_use_id = block.get("tool_use_id")
                key = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else line[:96]
                failed_tool_use_ids.add(key)

    if session_id_hint and thread_id is None:
        thread_id = session_id_hint
    usage_total = result_usage
    cache_creation_input_tokens = result_cache_creation_input_tokens
    if usage_total is None and message_usage:
        usage_total = _sum_usage(message_usage.values())
        cache_creation_input_tokens = sum(message_cache_creation_input_tokens.values())
    if (
        usage_total is None
        and thread_id
        and sessions_root is not None
        and workspace_path is not None
    ):
        session_path = claude_session_path(sessions_root, workspace_path, thread_id)
        recovered = latest_claude_session_usage(session_path)
        if recovered is not None:
            usage_total = recovered.usage
            cache_creation_input_tokens = _recover_cache_creation_input_tokens(session_path)

    usage_available = usage_total is not None
    input_tokens = usage_total.input_tokens if usage_total else 0
    cached_input_tokens = usage_total.cached_input_tokens if usage_total else 0
    output_tokens = usage_total.output_tokens if usage_total else 0
    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
    rate_metadata = catalog.rate_metadata_for(model) if catalog is not None else None
    estimated_credits = _estimate(
        rate_metadata.credit_rate if rate_metadata is not None else None,
        usage_available=usage_available,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    if total_cost_usd is not None:
        estimated_api_usd: float | None = total_cost_usd
        rate_card_version = CLAUDE_CLI_RATE_CARD_VERSION
    else:
        estimated_api_usd = _estimate(
            rate_metadata.api_usd_rate if rate_metadata is not None else None,
            usage_available=usage_available,
            uncached_input_tokens=uncached_input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )
        rate_card_version = (
            rate_metadata.rate_card_version
            if rate_metadata is not None and rate_metadata.rate_card_version is not None
            else "unknown"
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
        reasoning_output_tokens=0,
        tool_calls=sum(tool_counts.values()),
        failed_tool_calls=len(failed_tool_use_ids),
        error_events=error_events,
        tool_counts=tuple(sorted(tool_counts.items())),
        estimated_credits=estimated_credits,
        estimated_api_usd=estimated_api_usd,
        rate_card_version=rate_card_version,
        event_log_path=path,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def claude_turn_completed(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(
        (event := _parse_event(line)) is not None and event.get("type") == "result"
        for line in lines
    )


def extract_claude_final_message(event_log_path: Path) -> str | None:
    """Return the worker's final assistant text from a Claude stream-json event log.

    Prefers the terminal ``result`` event's ``result`` field (the CLI's own final message)
    and falls back to the last assistant message's concatenated text blocks. This
    materializes the ``.last-message.md`` that read-only recovery reads, since Claude has no
    ``--output-last-message`` equivalent the way Codex does.
    """

    try:
        lines = event_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    final_result: str | None = None
    final_assistant: str | None = None
    for line in lines:
        event = _parse_event(line)
        if event is None:
            continue
        event_type = event.get("type")
        if event_type == "result":
            result_text = event.get("result")
            if isinstance(result_text, str) and result_text.strip():
                final_result = result_text
        elif event_type == "assistant":
            joined = "".join(
                str(block.get("text") or "")
                for block in _content_blocks(event.get("message"))
                if block.get("type") == "text"
            ).strip()
            if joined:
                final_assistant = joined
    return final_result or final_assistant


def render_claude_json_line(line: str) -> str:
    event = _parse_event(line)
    if event is None:
        return line.rstrip("\r\n") + "\n"
    event_type = str(event.get("type") or "event")
    if event_type == "stream_event":
        return ""
    if event_type == "system":
        subtype = str(event.get("subtype") or "system")
        if subtype == "init":
            return f"session: {event.get('session_id')} model={event.get('model')}\n"
        return f"system: {subtype}\n"
    if event_type == "assistant":
        return _render_assistant(event.get("message"))
    if event_type == "user":
        rendered = []
        for block in _content_blocks(event.get("message")):
            if block.get("type") == "tool_result":
                state = "error" if block.get("is_error") is True else "ok"
                rendered.append(f"tool result [{state}]\n")
        return "".join(rendered)
    if event_type == "result":
        usage = event.get("usage")
        return (
            f"result {event.get('subtype')} "
            f"cost_usd={event.get('total_cost_usd')} "
            f"usage={json.dumps(usage if isinstance(usage, dict) else {}, sort_keys=True)}\n"
        )
    return f"{event_type}\n"


def scan_claude_tool_constraints(
    event_log_path: Path,
    scan_size: int,
    tool_call_count: int,
    *,
    tool_call_budget: int,
) -> tuple[str | None, int, int]:
    """Count Claude tool_use blocks against the configured hard budget."""
    text, next_scan_size = _read_new_text(event_log_path, scan_size)
    for line in text.splitlines():
        event = _parse_event(line)
        if event is None or event.get("type") != "assistant":
            continue
        for block in _content_blocks(event.get("message")):
            if block.get("type") == "tool_use":
                tool_call_count += 1
    if 0 < tool_call_budget < tool_call_count:
        return (
            f"Claude tool-call budget of {tool_call_budget} exceeded "
            f"({tool_call_count} tool calls observed)",
            next_scan_size,
            tool_call_count,
        )
    return None, next_scan_size, tool_call_count


def _render_assistant(message: Any) -> str:
    rendered: list[str] = []
    for block in _content_blocks(message):
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text") or "").rstrip("\r\n")
            if text:
                rendered.append(text + "\n")
            continue
        if block_type != "tool_use":
            continue
        name = str(block.get("name") or "unknown")
        raw_input = block.get("input")
        block_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        if name == "Bash":
            rendered.append(f"exec\n{block_input.get('command') or ''}\n[requested]\n")
        elif name == "WebSearch":
            rendered.append(f"web search: {block_input.get('query') or ''} [requested]\n")
        elif name == "WebFetch":
            rendered.append(f"web fetch: {block_input.get('url') or ''} [requested]\n")
        elif name.startswith("mcp__"):
            rendered.append(f"{_claude_tool_key(name)} requested\n".replace("mcp:", "mcp: ", 1))
        else:
            target = block_input.get("file_path") or block_input.get("path") or ""
            rendered.append(f"{name}: {target}\n" if target else f"{name}\n")
    return "".join(rendered)


def _claude_tool_key(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"mcp:{parts[1]}/{parts[2]}"
    return name


def _content_blocks(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _sum_usage(values: Any) -> TokenUsage:
    total = TokenUsage(0, 0, 0, 0)
    for usage in values:
        total = TokenUsage(
            input_tokens=total.input_tokens + usage.input_tokens,
            cached_input_tokens=total.cached_input_tokens + usage.cached_input_tokens,
            output_tokens=total.output_tokens + usage.output_tokens,
            reasoning_output_tokens=0,
        )
    return total


def _cache_creation_from_mapping(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get("cache_creation_input_tokens")
    return int(raw) if isinstance(raw, int | float) else 0


def _recover_cache_creation_input_tokens(path: Path) -> int:
    """Sum cache-creation tokens from a Claude session transcript.

    Mirrors the message dedup/sidechain-skip logic in
    ``latest_claude_session_usage`` since the shared ``TokenUsage`` schema
    cannot carry a cache-creation field.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    cache_creation_by_message: dict[str, int] = {}
    for line in lines:
        entry = _parse_event(line)
        if entry is None or entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        message_id = message.get("id")
        key = message_id if isinstance(message_id, str) and message_id else str(entry.get("uuid"))
        cache_creation_by_message[key] = _cache_creation_from_mapping(usage)
    return sum(cache_creation_by_message.values())


def _read_new_text(path: Path, scan_size: int) -> tuple[str, int]:
    try:
        with path.open("rb") as handle:
            handle.seek(scan_size)
            raw = handle.read()
    except OSError:
        return "", scan_size
    return raw.decode("utf-8", errors="replace"), scan_size + len(raw)


def _parse_event(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


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
