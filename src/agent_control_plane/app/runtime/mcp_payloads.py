from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Any

from agent_control_plane.app.runtime.mcp_payload_windows import (
    DEFAULT_PREVIEW_BYTES,
    MAX_RESPONSE_BYTES,
    _utf8_prefix,
    serialized_bytes,
    text_preview,
)

_MAX_CHECK_SUMMARIES = 32
_CHECK_SUMMARY_BYTES = 128


def compact_review_item(
    item: Mapping[str, Any],
    *,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    """Project a durable inbox row without returning its unbounded evidence payloads."""
    item_id = str(item.get("item_id") or item.get("source_id") or "review-item")
    result_text = item.get("result_text")
    if not isinstance(result_text, str):
        result_text = item.get("result_excerpt")
    if not isinstance(result_text, str):
        result_text = ""
    result = text_preview(
        result_text,
        stable_id=item_id,
        path=_optional_string(item.get("result_path")),
        offset=offset,
        limit=limit,
        sha256=_optional_string(item.get("result_sha256")),
    )

    compact = {
        key: _compact_scalar(item.get(key))
        for key in (
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
        )
    }
    compact["id"] = item_id
    compact["result"] = result
    compact["verification_summary"] = _verification_summary(item)
    return compact


def compact_checkpoint(
    payload: Mapping[str, Any],
    *,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    slot = payload.get("slot")
    if isinstance(slot, Mapping):
        result["slot"] = _compact_slot(slot, limit=limit)
    inbox = payload.get("inbox")
    if isinstance(inbox, Mapping):
        result["inbox"] = compact_review_item(inbox, offset=offset, limit=limit)
    return result


def compact_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bound variable summary metadata while preserving its review-critical fields."""
    compact = {
        key: value if key in {"dirty_status", "log_tail"} else _bounded_value(value)
        for key, value in payload.items()
    }
    forbidden = payload.get("forbidden_changes")
    if isinstance(forbidden, Sequence) and not isinstance(forbidden, str):
        compact["forbidden_changes_total"] = len(forbidden)
        compact["forbidden_changes_truncated"] = len(forbidden) > _MAX_CHECK_SUMMARIES
    if serialized_bytes(compact) > MAX_RESPONSE_BYTES:
        compact["latest_attempt_metrics"] = None
        compact["forbidden_changes"] = _bounded_value(
            forbidden[:8]
            if isinstance(forbidden, Sequence) and not isinstance(forbidden, str)
            else []
        )
        compact["summary_metadata_truncated"] = True
    return compact


def _verification_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    raw_bundle = item.get("verification_bundle")
    bundle = raw_bundle if isinstance(raw_bundle, Mapping) else {}
    raw_result = bundle.get("result")
    result = raw_result if isinstance(raw_result, Mapping) else {}
    raw_worker = bundle.get("worker_verification")
    worker = raw_worker if isinstance(raw_worker, Mapping) else {}
    raw_worker_payload = worker.get("payload")
    worker_payload = raw_worker_payload if isinstance(raw_worker_payload, Mapping) else {}
    raw_controller = bundle.get("controller_quality")
    controller = raw_controller if isinstance(raw_controller, Mapping) else {}
    raw_controller_payload = controller.get("payload")
    controller_payload = (
        raw_controller_payload if isinstance(raw_controller_payload, Mapping) else {}
    )
    raw_artifact = bundle.get("artifact")
    artifact = raw_artifact if isinstance(raw_artifact, Mapping) else {}
    raw_result_contract = bundle.get("result_contract")
    result_contract = raw_result_contract if isinstance(raw_result_contract, Mapping) else {}
    raw_worker_quality = bundle.get("worker_quality")
    worker_quality = raw_worker_quality if isinstance(raw_worker_quality, Mapping) else {}
    raw_quality_contract = bundle.get("quality_contract")
    quality_contract = raw_quality_contract if isinstance(raw_quality_contract, Mapping) else {}
    return {
        "review_ready": bundle.get("review_ready"),
        "review_blocked_reason": _compact_scalar(bundle.get("review_blocked_reason")),
        "controller_gate_mode": bundle.get("controller_gate_mode"),
        "result": {
            "status": result.get("status"),
            "format_valid": result.get("format_valid"),
            "missing_sections": _compact_string_sequence(result.get("missing_sections")),
        },
        "result_contract": {
            key: _compact_scalar(result_contract.get(key))
            for key in ("expected_status", "reported_status", "matches")
        },
        "worker_verification": {
            "state": worker.get("state", item.get("verification_state")),
            "schema_version": worker.get("schema_version", item.get("verification_schema")),
            "sha256": worker.get("sha256", item.get("verification_sha256")),
            "error": _compact_scalar(worker.get("error")),
            "checks": _check_outcomes(worker_payload.get("checks")),
        },
        "controller_quality": {
            "state": controller.get("state"),
            "status": controller_payload.get("status"),
            "error": _compact_scalar(controller.get("error")),
            "checks": _check_outcomes(controller_payload.get("checks")),
        },
        "worker_quality": {
            key: _compact_scalar(worker_quality.get(key))
            for key in ("status", "reason", "required")
        },
        "quality_contract": {
            key: _compact_scalar(quality_contract.get(key)) for key in ("policy", "sha256", "error")
        },
        "artifact": {
            key: _compact_scalar(artifact.get(key))
            for key in (
                "kind",
                "checkpoint_ref",
                "checkpoint_sha",
                "checkpoint_verified",
                "clean_tree_sha",
                "disposition",
                "error",
            )
        },
        "changed_file_count": _sequence_length(bundle.get("changed_files_actual")),
    }


def _check_outcomes(raw_checks: Any) -> dict[str, Any]:
    checks = (
        raw_checks if isinstance(raw_checks, Sequence) and not isinstance(raw_checks, str) else []
    )
    outcomes: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    for raw_check in checks:
        if not isinstance(raw_check, Mapping):
            continue
        outcome = str(raw_check.get("outcome") or "unknown")
        outcomes[outcome] += 1
        if len(items) >= _MAX_CHECK_SUMMARIES:
            continue
        items.append(
            {
                key: _compact_scalar(raw_check.get(key), limit=_CHECK_SUMMARY_BYTES)
                for key in ("name", "outcome", "exit_code", "summary")
                if raw_check.get(key) is not None
            }
        )
    total = sum(outcomes.values())
    return {
        "total": total,
        "returned": len(items),
        "truncated": total > len(items),
        "outcomes": dict(sorted(outcomes.items())),
        "items": items,
    }


def _compact_slot(slot: Mapping[str, Any], *, limit: int) -> dict[str, Any]:
    compact = {
        key: _compact_scalar(value)
        for key, value in slot.items()
        if key not in {"dirty", "problems"}
    }
    compact["dirty"] = text_preview(
        str(slot.get("dirty") or ""),
        stable_id=str(slot.get("name") or "slot"),
        path=_optional_string(slot.get("path")),
        limit=limit,
    )
    compact["problems"] = _compact_string_sequence(slot.get("problems"))
    return compact


def _compact_string_sequence(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [str(_compact_scalar(item)) for item in value[:_MAX_CHECK_SUMMARIES]]


def _sequence_length(value: Any) -> int:
    return len(value) if isinstance(value, Sequence) and not isinstance(value, str) else 0


def _compact_scalar(value: Any, *, limit: int = 512) -> Any:
    if isinstance(value, str):
        return _utf8_prefix(value, limit)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _utf8_prefix(str(value), limit)


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _utf8_prefix(value, 256)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= 3:
        return _utf8_prefix(str(value), 256)
    if isinstance(value, Mapping):
        items = list(islice(value.items(), _MAX_CHECK_SUMMARIES + 1))
        compact = {
            str(key): _bounded_value(item, depth=depth + 1)
            for key, item in items[:_MAX_CHECK_SUMMARIES]
        }
        if len(items) > _MAX_CHECK_SUMMARIES:
            compact["_truncated"] = True
        return compact
    if isinstance(value, Sequence):
        return [_bounded_value(item, depth=depth + 1) for item in value[:_MAX_CHECK_SUMMARIES]]
    return _utf8_prefix(str(value), 256)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
