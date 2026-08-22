from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from agent_control_plane.app.runtime.mcp_byte_windows import serialized_bytes, utf8_prefix

MAX_MCP_BYTES = 64 * 1024


def compact_plan_list_page(
    items: list[dict[str, Any]], *, offset: int, limit: int, total_count: int
) -> dict[str, Any]:
    result = _page(
        [compact_plan_list_item(item) for item in items],
        offset=offset,
        limit=limit,
        total_count=total_count,
    )
    return _within_budget(result, subject="plan list")


def compact_plan_list_item(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    result = _fields(
        row,
        (
            "plan_id",
            "status",
            "progress",
            "task_count",
            "completed_count",
            "updated_at",
            "cancel_requested_at",
            "cancelled_at",
            "archived_at",
        ),
    )
    if "title" in row:
        result["title"] = _bounded_text(row["title"], limit=512)
    return result


def compact_review_list_page(
    items: list[dict[str, Any]], *, offset: int, limit: int, total_count: int
) -> dict[str, Any]:
    result = _page(
        [compact_review_list_item(item) for item in items],
        offset=offset,
        limit=limit,
        total_count=total_count,
    )
    return _within_budget(result, subject="review inbox")


def compact_review_list_item(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    result = _fields(
        row,
        (
            "item_id",
            "source_kind",
            "source_id",
            "source_status",
            "source_completed_at",
            "delivery_status",
            "review_status",
            "task_id",
            "route",
            "workspace_path",
            "slot_name",
            "parent_thread_id",
            "agent_path",
            "result_path",
            "rollout_path",
            "checkpoint_ref",
            "checkpoint_sha",
            "checkpoint_tree_sha",
            "base_sha",
            "checkpoint_error",
            "slot_released",
            "created_at",
            "updated_at",
            "reviewed_at",
            "requalify_count",
            "requalified_at",
            "result_sha256",
            "verification_state",
            "verification_schema",
            "verification_sha256",
            "payload_captured_at",
            "result_excerpt_truncated",
        ),
    )
    excerpt = row.get("result_excerpt")
    if isinstance(excerpt, str):
        result["result_excerpt"] = utf8_prefix(excerpt, 768)
        result["result_excerpt_total_bytes"] = len(excerpt.encode("utf-8"))
        result["result_excerpt_sha256"] = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    summary = row.get("verification_summary")
    if isinstance(summary, Mapping):
        result["verification_summary"] = _fields(
            summary,
            (
                "review_ready",
                "review_blocked_reason",
                "format_valid",
                "status",
                "verification_claim_count",
                "actual_changed_file_count",
                "artifact_kind",
                "checkpoint_verified",
                "artifact_error",
            ),
        )
    return result


def compact_execution_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return _fields(
        value,
        (
            "route",
            "slot",
            "backend",
            "workspace_access",
            "read_only",
            "codex_quality_tier",
            "codex_premium_override_reason",
            "codex_model",
            "codex_reasoning_effort",
            "claude_model",
            "claude_reasoning_effort",
            "expected_result_status",
            "controller_gate_mode",
            "expected_base_sha",
            "effective_scope_sha256",
            "codex_tool_call_budget",
            "retry_override_reason",
            "brief_sha256",
            "brief_chars",
        ),
    )


def _page(
    items: list[dict[str, Any]], *, offset: int, limit: int, total_count: int
) -> dict[str, Any]:
    next_offset = offset + len(items)
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "total_count": total_count,
        "returned": len(items),
        "truncated": next_offset < total_count,
        "next_cursor": next_offset if next_offset < total_count else None,
    }


def _fields(row: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: _bounded_text(row[name], limit=256) for name in names if name in row}


def _bounded_text(value: Any, *, limit: int) -> Any:
    return utf8_prefix(value, limit) if isinstance(value, str) else value


def _within_budget(payload: dict[str, Any], *, subject: str) -> dict[str, Any]:
    if serialized_bytes(payload) >= MAX_MCP_BYTES:
        raise ValueError(f"{subject} exceeds 64 KiB after compaction; request a smaller page")
    return payload
