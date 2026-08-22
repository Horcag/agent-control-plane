"""A single-copy, bounded wire contract for Agent Control Plane MCP tools."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, NamedTuple

from mcp.types import CallToolResult, TextContent

MAX_STRUCTURED_BYTES = 48 * 1024
MAX_WIRE_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 512


class ToolPolicy(NamedTuple):
    scope: str
    mutation: bool
    default: str
    detail_param: str | None
    identity_keys: tuple[str, ...]
    follow_up: str | None


def _p(
    scope: str,
    mutation: bool = False,
    default: str = "projected",
    detail_param: str | None = None,
    identity_keys: tuple[str, ...] = (),
    follow_up: str | None = None,
) -> ToolPolicy:
    return ToolPolicy(scope, mutation, default, detail_param, identity_keys, follow_up)


# Exhaustive registration validation keeps new MCP surfaces from bypassing this contract.
MCP_TOOL_POLICIES: dict[str, ToolPolicy] = {
    "agent_smoke": _p("route", default="preview", detail_param="full", follow_up="agent_smoke"),
    "agent_model_catalog": _p("global", default="page", identity_keys=("model",)),
    "agent_model_routing_explain": _p(
        "route", default="preview", identity_keys=("policy", "route")
    ),
    "agent_start_job": _p("job", True, "projected", None, ("job_id", "status"), "agent_status_job"),
    "agent_watch_job": _p("job", False, "preview", None, ("job_id", "status"), "agent_status_job"),
    "agent_watch_events": _p("job", False, "page", None, ("job_id", "cursor"), "agent_status_job"),
    "agent_terminal_statuses": _p("global", default="scalar"),
    "agent_status_job": _p(
        "job", False, "preview", None, ("job_id", "status"), "agent_summary_job"
    ),
    "agent_reconcile": _p(
        "job", True, "projected", "full", ("job_id", "status"), "agent_status_job"
    ),
    "agent_summary_job": _p("job", False, "preview", "full", ("job_id",), "agent_result_job"),
    "agent_analytics": _p("global", default="page", identity_keys=("model",)),
    "agent_plan_create": _p("plan", True, "projected", None, ("plan_id",), "agent_plan_snapshot"),
    "agent_plan_add_task": _p(
        "plan", True, "projected", None, ("plan_id", "task_id"), "agent_plan_snapshot"
    ),
    "agent_plan_edit_task": _p(
        "plan", True, "projected", None, ("plan_id", "task_id"), "agent_plan_snapshot"
    ),
    "agent_plan_bind_job": _p(
        "plan", True, "projected", None, ("plan_id", "task_id", "job_id"), "agent_plan_snapshot"
    ),
    "agent_plan_snapshot": _p("plan", default="page", identity_keys=("plan_id", "cursor")),
    "agent_plan_watch": _p("plan", default="page", identity_keys=("plan_id", "cursor")),
    "agent_plan_accept_task": _p(
        "plan", True, "projected", None, ("plan_id", "task_id"), "agent_plan_snapshot"
    ),
    "agent_plan_reject_task": _p(
        "plan", True, "projected", None, ("plan_id", "task_id"), "agent_plan_snapshot"
    ),
    "agent_plan_dispatch": _p(
        "plan", True, "projected", None, ("plan_id", "job_id"), "agent_watch_events"
    ),
    "agent_plan_run_until_review": _p(
        "plan", True, "projected", None, ("plan_id",), "agent_plan_snapshot"
    ),
    "agent_plan_retry_task": _p(
        "plan", True, "projected", None, ("plan_id", "task_id", "job_id"), "agent_plan_snapshot"
    ),
    "agent_plan_cancel": _p("plan", True, "projected", None, ("plan_id",), "agent_plan_snapshot"),
    "agent_plan_archive": _p("plan", True, "projected", None, ("plan_id",), "agent_plan_snapshot"),
    "agent_plan_list": _p("global", False, "page", "full", ("plan_id",), "agent_plan_snapshot"),
    "agent_retention_gc": _p("global", True, "projected", "full", ("job_id",), "agent_status_job"),
    "agent_review_inbox_list": _p(
        "global", False, "page", "full", ("item_id",), "agent_review_inbox_get"
    ),
    "agent_review_inbox_get": _p("item", False, "preview", "full", ("item_id",)),
    "agent_review_inbox_resolve": _p(
        "item", True, "projected", "full", ("item_id",), "agent_review_inbox_get"
    ),
    "agent_review_inbox_requalify": _p(
        "item", True, "projected", "full", ("item_id",), "agent_review_inbox_get"
    ),
    "agent_accept_handoff": _p(
        "plan", True, "projected", None, ("plan_id", "task_id"), "agent_plan_snapshot"
    ),
    "agent_sync_subagent_results": _p(
        "global", True, "projected", None, ("item_id",), "agent_review_inbox_list"
    ),
    "agent_tail_job": _p("job", False, "preview", "full", ("job_id",), "agent_tail_job"),
    "agent_result_job": _p("job", False, "preview", "full", ("job_id",), "agent_result_job"),
    "agent_cancel_job": _p(
        "job", True, "projected", None, ("job_id", "status"), "agent_status_job"
    ),
    "agent_archive_jobs": _p("global", True, "projected", "full", ("job_id",), "agent_status_job"),
    "agent_slots_sync": _p(
        "route", True, "projected", "full", ("name", "route"), "agent_slots_list"
    ),
    "agent_slots_list": _p("route", False, "page", "full", ("name", "route")),
    "agent_slots_create": _p("slot", True, "projected", None, ("name", "path"), "agent_slots_list"),
    "agent_slots_bootstrap": _p(
        "slot", True, "projected", "full", ("name", "path"), "agent_slots_list"
    ),
    "agent_slots_delete": _p("slot", True, "projected", None, ("name", "path"), "agent_slots_list"),
    "agent_slots_checkout": _p(
        "slot", True, "projected", None, ("name", "branch"), "agent_slots_list"
    ),
    "agent_slots_ensure_module": _p("slot", True, "projected", None, ("name",), "agent_slots_list"),
    "agent_slots_ensure_root_module": _p(
        "global", True, "projected", "full", ("name",), "agent_slots_list"
    ),
    "agent_slots_unload_module": _p("slot", True, "projected", None, ("name",), "agent_slots_list"),
    "agent_slots_unload_root_module": _p(
        "global", True, "projected", None, ("name",), "agent_slots_list"
    ),
    "agent_slots_remove_module": _p("slot", True, "projected", None, ("name",), "agent_slots_list"),
    "agent_slots_prepare": _p("slot", True, "projected", None, ("name",), "agent_slots_list"),
    "agent_slots_checkpoint": _p(
        "slot", True, "projected", "full", ("name", "job_id"), "agent_review_inbox_get"
    ),
    "agent_slots_cleanup": _p(
        "route", True, "projected", "full", ("name", "route"), "agent_slots_list"
    ),
}


def response_for(tool_name: str, value: Any, arguments: Mapping[str, Any]) -> CallToolResult:
    """Sanitize one MCP tool result into a single authoritative structured payload."""
    policy = MCP_TOOL_POLICIES[tool_name]
    is_error = False
    if isinstance(value, CallToolResult):
        is_error = value.isError
        payload = (
            dict(value.structuredContent)
            if isinstance(value.structuredContent, Mapping)
            else _prebuilt_metadata(value)
        )
    else:
        payload = dict(value) if isinstance(value, Mapping) else {"result": value}
    explicit_detail = policy.detail_param is not None and arguments.get(policy.detail_param) is True
    measured = serialized_bytes(payload)
    if not explicit_detail and measured > MAX_STRUCTURED_BYTES:
        payload = _guard_payload(tool_name, payload, measured, policy)
    result = _result(tool_name, payload, is_error)
    if (
        not explicit_detail
        and serialized_bytes(result.model_dump(by_alias=True, exclude_none=True)) >= MAX_WIRE_BYTES
    ):
        payload = _guard_payload(tool_name, payload, measured, policy)
        result = _result(tool_name, payload, is_error)
    return result


def _result(tool_name: str, payload: dict[str, Any], is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=_summary(tool_name, payload, is_error))],
        structuredContent=payload,
        isError=is_error,
    )


def _prebuilt_metadata(value: CallToolResult) -> dict[str, Any]:
    blocks = list(value.content)
    types = Counter(_block_type(block) for block in blocks)
    return {
        "ok": not value.isError,
        "isError": value.isError,
        "source_call_tool_result": True,
        "content_block_count": len(blocks),
        "content_block_type_counts": dict(sorted(types.items())),
        "content_sha256": hashlib.sha256(_stable_json(blocks).encode("utf-8")).hexdigest(),
    }


def _block_type(block: Any) -> str:
    if isinstance(block, Mapping):
        return str(block.get("type", "unknown"))
    return str(getattr(block, "type", "unknown"))


def serialized_bytes(value: Any) -> int:
    return len(_stable_json(value).encode("utf-8"))


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=_json_default, separators=(",", ":")
    )


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    return str(value)


def _summary(tool_name: str, payload: dict[str, Any], is_error: bool) -> str:
    outcome = "error" if is_error or payload.get("ok") is False else "ok"
    text = f"{tool_name}: {outcome}; read structuredContent for the authoritative result."
    return text.encode("utf-8")[:MAX_SUMMARY_BYTES].decode("utf-8", "ignore")


def _guard_payload(
    tool_name: str, payload: dict[str, Any], measured: int, policy: ToolPolicy
) -> dict[str, Any]:
    guarded: dict[str, Any] = {
        "ok": payload.get("ok", True),
        "status": payload.get("status"),
        "error": payload.get("error"),
        "tool": tool_name,
        "guarded": True,
        "truncated": True,
        "measured_bytes": measured,
        "structured_cap_bytes": MAX_STRUCTURED_BYTES,
        "wire_cap_bytes": MAX_WIRE_BYTES,
        "payload_sha256": hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest(),
    }
    for key in policy.identity_keys:
        if key in payload and _small_scalar(payload[key]):
            guarded[key] = payload[key]
    for key, item in payload.items():
        if isinstance(item, (list, tuple, dict)):
            guarded[f"{key}_count"] = len(item)
    if policy.follow_up:
        guarded["detail_tool"] = policy.follow_up
    return {key: item for key, item in guarded.items() if item is not None}


def _small_scalar(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, (bool, int, float))
        or (isinstance(value, str) and len(value.encode("utf-8")) <= 256)
    )
