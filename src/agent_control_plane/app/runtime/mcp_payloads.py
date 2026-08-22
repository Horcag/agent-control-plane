from __future__ import annotations

import codecs
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import islice
from pathlib import Path
from typing import Any

DEFAULT_PREVIEW_BYTES = 16 * 1024
MAX_PREVIEW_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
_MIN_UTF8_WINDOW_BYTES = 4
_MAX_CHECK_SUMMARIES = 32
_CHECK_SUMMARY_BYTES = 128


def file_preview(
    path: Path,
    *,
    stable_id: str,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    """Read one UTF-8-safe window while hashing the durable file incrementally."""
    effective_limit = _clamp_limit(limit)
    total_bytes = path.stat().st_size
    effective_offset = _utf8_start_offset(path, _clamp_offset(offset, total_bytes), total_bytes)
    with path.open("rb") as handle:
        handle.seek(effective_offset)
        raw = handle.read(effective_limit)
    complete = _complete_utf8_prefix(raw)
    returned_bytes = len(complete)
    next_offset = effective_offset + returned_bytes
    truncated = next_offset < total_bytes
    return {
        "id": stable_id,
        "path": str(path),
        "available": True,
        "content": complete.decode("utf-8", errors="replace"),
        "offset": effective_offset,
        "limit": effective_limit,
        "total_bytes": total_bytes,
        "returned_bytes": returned_bytes,
        "sha256": _sha256_file(path),
        "truncated": truncated,
        "next_offset": next_offset if truncated else None,
    }


def missing_file_preview(
    path: Path,
    *,
    stable_id: str,
    message: str,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    effective_limit = _clamp_limit(limit)
    return {
        "id": stable_id,
        "path": str(path),
        "available": False,
        "content": "",
        "message": _utf8_prefix(message, effective_limit),
        "offset": 0,
        "limit": effective_limit,
        "total_bytes": 0,
        "returned_bytes": 0,
        "sha256": None,
        "truncated": False,
        "next_offset": None,
    }


def tail_preview(
    path: Path,
    *,
    stable_id: str,
    lines: int,
    cursor: int | None = None,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    total_bytes = path.stat().st_size
    start = _tail_start_offset(path, max(1, lines))
    requested_offset = start if cursor is None else max(start, cursor)
    payload = file_preview(
        path,
        stable_id=stable_id,
        offset=requested_offset,
        limit=limit,
    )
    payload["cursor"] = payload["offset"]
    payload["next_cursor"] = payload["next_offset"]
    payload["tail_start"] = start
    payload["lines"] = max(1, lines)
    payload["total_bytes"] = total_bytes
    return payload


def text_preview(
    content: str,
    *,
    stable_id: str,
    path: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_BYTES,
    sha256: str | None = None,
) -> dict[str, Any]:
    raw = content.encode("utf-8")
    total_bytes = len(raw)
    effective_offset = _utf8_bytes_start(raw, _clamp_offset(offset, total_bytes))
    effective_limit = _clamp_limit(limit)
    complete = _complete_utf8_prefix(raw[effective_offset : effective_offset + effective_limit])
    next_offset = effective_offset + len(complete)
    truncated = next_offset < total_bytes
    return {
        "id": stable_id,
        "path": path,
        "available": True,
        "content": complete.decode("utf-8", errors="replace"),
        "offset": effective_offset,
        "limit": effective_limit,
        "total_bytes": total_bytes,
        "returned_bytes": len(complete),
        "sha256": sha256 or hashlib.sha256(raw).hexdigest(),
        "truncated": truncated,
        "next_offset": next_offset if truncated else None,
    }


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


def serialized_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


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


def _clamp_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    return min(MAX_PREVIEW_BYTES, max(_MIN_UTF8_WINDOW_BYTES, limit))


def _clamp_offset(offset: int, total_bytes: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    return min(total_bytes, max(0, offset))


def _utf8_start_offset(path: Path, offset: int, total_bytes: int) -> int:
    if offset <= 0 or offset >= total_bytes:
        return offset
    with path.open("rb") as handle:
        handle.seek(offset)
        byte = handle.read(1)
        while byte and byte[0] & 0xC0 == 0x80 and offset < total_bytes:
            offset += 1
            byte = handle.read(1)
    return offset


def _utf8_bytes_start(raw: bytes, offset: int) -> int:
    while offset < len(raw) and raw[offset] & 0xC0 == 0x80:
        offset += 1
    return offset


def _complete_utf8_prefix(raw: bytes) -> bytes:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder.decode(raw, final=False)
    pending, _ = decoder.getstate()
    return raw[: len(raw) - len(pending)] if pending else raw


def _utf8_prefix(value: str, limit: int) -> str:
    raw = value.encode("utf-8")[:limit]
    return _complete_utf8_prefix(raw).decode("utf-8", errors="replace")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_start_offset(path: Path, lines: int) -> int:
    size = path.stat().st_size
    if size == 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(size - 1)
        trailing_newline = handle.read(1) == b"\n"
        needed = lines + int(trailing_newline)
        position = size
        chunk_size = 64 * 1024
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            for index in range(len(chunk) - 1, -1, -1):
                if chunk[index] == 0x0A:
                    needed -= 1
                    if needed == 0:
                        return position + index + 1
    return 0
