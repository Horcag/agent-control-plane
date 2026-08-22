from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from agent_control_plane.app.runtime.mcp_byte_windows import serialized_bytes, utf8_prefix
from agent_control_plane.app.runtime.mcp_collection_rows import compact_execution_summary

MAX_COLLECTION_LIMIT = 100
DEFAULT_COLLECTION_LIMIT = 20
MAX_ANALYTICS_SAMPLES = 100
MAX_MCP_BYTES = 64 * 1024


def validate_page(offset: int, limit: int) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    validate_positive_limit(limit, name="limit", maximum=MAX_COLLECTION_LIMIT)
    return offset, limit


def validate_positive_limit(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def validate_optional_cursor(value: int | None, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def collection_page(
    items: Sequence[dict[str, Any]], *, offset: int, limit: int, total_count: int
) -> dict[str, Any]:
    returned = list(items)
    next_offset = offset + len(returned)
    return {
        "items": returned,
        "offset": offset,
        "limit": limit,
        "total_count": total_count,
        "returned": len(returned),
        "truncated": next_offset < total_count,
        "next_cursor": next_offset if next_offset < total_count else None,
    }


def compact_model_catalog(payload: Mapping[str, Any], *, offset: int, limit: int) -> dict[str, Any]:
    models = payload.get("models")
    rows = list(models) if isinstance(models, list) else []
    result = _compact_top_level(payload, excluded={"models"})
    result.update(
        collection_page(
            [_compact_model(row) for row in rows[offset : offset + limit]],
            offset=offset,
            limit=limit,
            total_count=len(rows),
        )
    )
    return _within_budget(result, subject="model catalog")


def compact_analytics(payload: Mapping[str, Any], *, sample_limit: int) -> dict[str, Any]:
    attempts = payload.get("attempts")
    rows = list(attempts) if isinstance(attempts, list) else []
    result = _compact_top_level(payload, excluded={"attempts"})
    result["attempts"] = [_compact_attempt(row) for row in rows[:sample_limit]]
    result["sample_count"] = len(result["attempts"])
    result["sample_limit"] = sample_limit
    result["sample_truncated"] = len(rows) > sample_limit
    return _within_budget(result, subject="analytics")


def compact_plan_snapshot(
    payload: Mapping[str, Any], *, event_limit: int, item_limit: int
) -> dict[str, Any]:
    excluded = {
        "running",
        "awaiting_review",
        "blocked",
        "ready_next",
        "requires_root_decision",
        "completed_tasks",
        "changes",
        "completed",
    }
    result = _compact_top_level(payload, excluded=excluded)
    for name in ("running", "awaiting_review", "blocked", "ready_next", "requires_root_decision"):
        value = payload.get(name)
        if isinstance(value, list):
            result[name] = [_compact_plan_task(item) for item in value[:item_limit]]
    for name in ("completed_tasks",):
        value = payload.get(name)
        if isinstance(value, list):
            result[name] = [_compact_plan_task(item) for item in value[:item_limit]]
    for name in ("changes", "completed"):
        value = payload.get(name)
        if isinstance(value, list):
            result[name] = [_compact_plan_change(item) for item in value[:event_limit]]
    return _within_budget(result, subject="plan snapshot")


def compact_slot(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    result = {
        key: _compact_scalar(row.get(key), limit=256)
        for key in (
            "name",
            "route",
            "status",
            "scope",
            "configured",
            "exists",
            "is_git_workspace",
            "active_job_id",
            "use_count",
            "last_used_at",
        )
        if key in row
    }
    for key in ("path", "branch", "note"):
        if key in row:
            result[key] = _compact_scalar(row.get(key), limit=512)
    dirty = row.get("dirty")
    if isinstance(dirty, str):
        result["dirty"] = utf8_prefix(dirty, 512)
        result["dirty_total_bytes"] = len(dirty.encode("utf-8"))
        result["dirty_sha256"] = hashlib.sha256(dirty.encode("utf-8")).hexdigest()
    problems = row.get("problems")
    if isinstance(problems, Sequence) and not isinstance(problems, str):
        result["problems"] = [utf8_prefix(str(item), 256) for item in problems[:16]]
        result["problems_total_count"] = len(problems)
    return result


def compact_slots_page(
    slots: Sequence[dict[str, Any]], *, offset: int, limit: int
) -> dict[str, Any]:
    result = collection_page(
        [compact_slot(item) for item in slots[offset : offset + limit]],
        offset=offset,
        limit=limit,
        total_count=len(slots),
    )
    return _within_budget(result, subject="slot inventory")


def _compact_model(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    result = {
        key: _compact_scalar(row.get(key), limit=256)
        for key in (
            "model",
            "visible",
            "priority",
            "default_reasoning_effort",
            "quota_domain",
            "premium",
            "premium_state",
            "rate_card_version",
            "rate_card_source",
            "has_credit_rate",
            "has_api_usd_rate",
            "inventory_state",
            "metadata_state",
            "launch_disposition",
            "last_seen_at",
        )
        if key in row
    }
    efforts = row.get("supported_reasoning_efforts")
    result["supported_reasoning_efforts"] = (
        [utf8_prefix(str(effort), 128) for effort in efforts[:16]]
        if isinstance(efforts, list)
        else []
    )
    return result


def _compact_attempt(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    result = {
        key: _compact_scalar(row.get(key), limit=160)
        for key in (
            "job_id",
            "attempt_no",
            "status",
            "result_status",
            "model",
            "reasoning_effort",
            "backend",
            "duration_sec",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "tool_calls",
            "failed_tool_calls",
            "error_events",
            "estimated_credits",
            "estimated_api_usd",
        )
        if key in row
    }
    tools = row.get("tool_counts")
    if isinstance(tools, Mapping):
        pairs = sorted(
            (utf8_prefix(str(key), 80), _compact_scalar(item, limit=80))
            for key, item in tools.items()
        )[:8]
        result["tool_counts"] = dict(pairs)
        result["tool_counts_total"] = len(tools)
        result["tool_counts_truncated"] = len(tools) > len(pairs)
    return result


def _compact_plan_task(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    fields = (
        "task_id",
        "title",
        "state",
        "job_id",
        "job_status",
        "attempt_no",
        "review_status",
        "accepted_sha",
        "dispatch_error",
        "result_path",
        "needs_strategy_revision",
        "retry_fingerprint",
        "depends_on",
        "route",
        "backend",
        "result_summary",
    )
    result = {key: _compact_value(row[key], depth=1) for key in fields if key in row}
    execution = compact_execution_summary(row.get("execution"))
    if execution is not None:
        result["execution"] = execution
    return result


def _compact_plan_change(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    return {
        key: _compact_value(row[key], depth=1)
        for key in ("cursor", "event", "event_type", "task_id", "at", "state", "job_id")
        if key in row
    }


def _compact_top_level(payload: Mapping[str, Any], *, excluded: set[str]) -> dict[str, Any]:
    return {
        key: _compact_value(value, depth=2) for key, value in payload.items() if key not in excluded
    }


def _compact_value(value: Any, *, depth: int) -> Any:
    if isinstance(value, str):
        return utf8_prefix(value, 512)
    if isinstance(value, Mapping):
        if depth <= 0:
            return {"total_keys": len(value)}
        pairs = list(value.items())[:20]
        result = {
            utf8_prefix(str(key), 128): _compact_value(item, depth=depth - 1) for key, item in pairs
        }
        if len(value) > len(pairs):
            result["truncated_keys"] = len(value) - len(pairs)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if depth <= 0:
            return {"total_count": len(value)}
        items = [_compact_value(item, depth=depth - 1) for item in value[:20]]
        if len(value) > len(items):
            return {"items": items, "total_count": len(value), "truncated": True}
        return items
    return value


def _compact_scalar(value: Any, *, limit: int) -> Any:
    return utf8_prefix(value, limit) if isinstance(value, str) else value


def _within_budget(payload: dict[str, Any], *, subject: str) -> dict[str, Any]:
    if serialized_bytes(payload) >= MAX_MCP_BYTES:
        raise ValueError(f"{subject} exceeds 64 KiB after compaction; request a smaller page")
    return payload
