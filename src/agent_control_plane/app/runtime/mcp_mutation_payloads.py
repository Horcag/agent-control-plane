"""Small, tool-specific MCP projections for mutation responses."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from agent_control_plane.app.runtime.mcp_byte_windows import (
    MAX_RESPONSE_BYTES,
    serialized_bytes,
    utf8_prefix,
)
from agent_control_plane.app.runtime.mcp_collections import compact_slot

_ROW_FIELDS = (
    "job_id",
    "name",
    "route",
    "path",
    "checkpoint_ref",
    "checkpoint_sha",
    "action",
    "reason",
    "status",
    "error",
)
_SECTION_LIMIT = 20
_DIAGNOSTIC_FIELDS = (
    "status",
    "changed",
    "created",
    "present",
    "loaded",
    "error",
    "problem",
    "problems",
    "errors",
    "dirty",
    "path",
    "config_path",
    "slot_path",
    "module_file",
    "modules_xml",
    "workspace_xml",
    "profile_file",
    "vcs_xml",
    "module_name",
    "sdk_name",
)


def reconcile_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    categories = {
        name: [_text(item) for item in _rows(value)]
        for name, value in payload.items()
        if isinstance(value, Sequence) and not isinstance(value, str)
    }
    result: dict[str, Any] = {
        "ok": True,
        "counts": {name: len(value) for name, value in categories.items()},
    }
    result.update(categories)
    return _safe(result, "reconcile")


def retention_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidates = _mapping(payload.get("candidates"))
    categories = {name: len(_rows(rows)) for name, rows in candidates.items()}
    result = {
        "ok": True,
        "apply": payload.get("apply"),
        "cutoff": _text(payload.get("cutoff"), 128),
        "limit_per_category": payload.get("limit_per_category"),
        "limit_total": payload.get("limit_total"),
        "counts": _mapping(payload.get("counts")),
        "applied": _mapping(payload.get("applied")),
        "candidate_categories": categories,
        "blocked_checkpoint_refs": [
            _row(row) for row in _rows(payload.get("blocked_checkpoint_refs"))
        ],
    }
    return _safe(result, "retention")


def archive_payload(rows: Sequence[dict[str, Any]], *, apply: bool, limit: int) -> dict[str, Any]:
    affected = [_row(row) for row in rows]
    return _safe(
        {"ok": True, "apply": apply, "limit": limit, "affected": affected, "count": len(affected)},
        "archive",
    )


def slots_sync_payload(
    rows: Sequence[dict[str, Any]], *, route: str | None, all_routes: bool, limit: int
) -> dict[str, Any]:
    total_count = len(rows)
    affected = [compact_slot(row) for row in rows[:limit]]
    return _safe(
        {
            "ok": True,
            "route": route,
            "all_routes": all_routes,
            "registration_scope": "all selected configured slots; response is limited",
            "limit": limit,
            "total_count": total_count,
            "returned": len(affected),
            "truncated": total_count > len(affected),
            "affected": affected,
        },
        "slot sync",
    )


def slots_cleanup_payload(
    rows: Sequence[dict[str, Any]], *, apply: bool, limit: int
) -> dict[str, Any]:
    total_count = len(rows)
    decisions = [_row(row) for row in rows[:limit]]
    return _safe(
        {
            "ok": True,
            "apply": apply,
            "limit": limit,
            "total_count": total_count,
            "returned": len(decisions),
            "truncated": total_count > len(decisions),
            "decisions": decisions,
        },
        "slot cleanup",
    )


def bootstrap_payload(payload: Mapping[str, Any], *, subject: str) -> dict[str, Any]:
    result = {"ok": payload.get("ok", True)}
    bootstrap = _mapping(payload.get("bootstrap"))
    module = _mapping(payload.get("module"))
    if bootstrap:
        result["bootstrap"] = _bootstrap(bootstrap)
    if module:
        result["module"] = _ide(module)
    _copy_diagnostics(result, payload)
    return _safe(result, subject)


def _bootstrap(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    config = _mapping(value.get("config"))
    if config:
        result["config"] = _config(config)
    slot = value.get("slot")
    if isinstance(slot, Mapping):
        result["slot"] = compact_slot(slot)
    ide = _mapping(value.get("ide"))
    if ide:
        result["ide"] = _ide(ide)
    _copy_diagnostics(result, value)
    return result


def _config(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "config_path",
        "changed",
        "created",
        "status",
        "message",
        "route",
        "slot",
        "route_added",
        "slot_added",
        "repo_path",
        "slot_path",
        "required_branch",
    )
    result = {key: _bounded(value[key]) for key in fields if key in value}
    for key in ("source_roots", "test_roots", "exclude_dirs", "routes", "slots"):
        if key in value:
            result[key] = _section(value[key], _text)
    _copy_diagnostics(result, value)
    return result


def _ide(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("root_module", "duplicate_inspection"):
        section = _mapping(value.get(key))
        if section:
            result[key] = _ide_row(section)
    vcs_row = _mapping(value.get("vcs_mappings"))
    result["vcs_mappings"] = _section([vcs_row] if vcs_row else value.get("vcs_mappings"), _ide_row)
    for key in ("dedicated_slot_modules", "removed_slot_modules"):
        result[key] = _section(value.get(key), _ide_row)
    _copy_diagnostics(result, value)
    return result


def _ide_row(value: Any) -> dict[str, Any]:
    row = _mapping(value)
    fields = (
        "module_name",
        "module_file",
        "modules_xml",
        "workspace_xml",
        "profile_file",
        "vcs_xml",
        "path",
        "sdk_name",
        "status",
        "changed",
        "present",
        "loaded",
        "workspace_configured_loaded",
        "runtime_loaded",
        "ide_reload_required",
        "error",
        "problem",
    )
    result = {key: _bounded(row[key]) for key in fields if key in row}
    for key in ("problems", "errors", "mapped_directories"):
        if key in row:
            result[key] = _section(row[key], _text)
    return result


def _copy_diagnostics(result: dict[str, Any], value: Mapping[str, Any]) -> None:
    for key in ("error", "problem"):
        if key in value:
            result[key] = _text(value[key], 512)
    for key in ("errors", "problems"):
        if key in value:
            result[key] = _section(value[key], _text)
    for key in ("counts", "count"):
        if key in value:
            result[key] = _bounded(value[key])


def _section(value: Any, project) -> dict[str, Any]:
    rows = _rows(value)
    items = [project(item) for item in rows[:_SECTION_LIMIT]]
    return {
        "items": items,
        "total_count": len(rows),
        "returned": len(items),
        "truncated": len(rows) > len(items),
    }


def _row(value: Any) -> dict[str, Any]:
    row = _mapping(value)
    return {key: _bounded(row[key]) for key in _ROW_FIELDS if key in row}


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return utf8_prefix(value, 512)
    if isinstance(value, Mapping):
        return {str(key): _bounded(item) for key, item in list(value.items())[:12]}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _section(value, _bounded)
    return value


def _rows(value: Any) -> list[Any]:
    return (
        list(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
        else []
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int = 512) -> str:
    return utf8_prefix(str(value), limit)


def _safe(payload: dict[str, Any], subject: str) -> dict[str, Any]:
    if serialized_bytes(payload) < MAX_RESPONSE_BYTES:
        return payload
    fallback: dict[str, Any] = {
        "ok": payload.get("ok", True),
        "subject": subject,
        "truncated": True,
        "diagnostic": "payload reduced after side effect",
    }
    _copy_diagnostics(fallback, payload)
    for key in ("bootstrap", "module"):
        value = _mapping(payload.get(key))
        if value:
            fallback[key] = _fallback_diagnostics(value)
    for key in (
        "apply",
        "cutoff",
        "limit",
        "limit_total",
        "limit_per_category",
        "total_count",
        "returned",
    ):
        if key in payload:
            fallback[key] = _bounded(payload[key])
    return fallback


def _fallback_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    if {"items", "total_count", "returned", "truncated"}.issubset(value):
        items = _rows(value.get("items"))[:2]
        total_count = value.get("total_count")
        if not isinstance(total_count, int):
            total_count = len(_rows(value.get("items")))
        return {
            "items": [_diagnostic_item(item) for item in items],
            "total_count": total_count,
            "returned": len(items),
            "truncated": total_count > len(items),
        }
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _DIAGNOSTIC_FIELDS:
            result[key] = _bounded(item)
            if (
                key in {"dirty", "path", "config_path", "slot_path", "module_file"}
                and isinstance(item, str)
                and len(item.encode("utf-8")) > 128
            ):
                result[f"{key}_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
        elif isinstance(item, Mapping):
            nested = _fallback_diagnostics(item)
            if nested:
                result[key] = nested
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            result[f"{key}_count"] = len(item)
            result[f"{key}_sample"] = [_diagnostic_item(row) for row in item[:2]]
    return result


def _diagnostic_item(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _text(value, 128)
    result = _fallback_diagnostics(value)
    for key in ("path", "module_file", "config_path", "slot_path"):
        item = value.get(key)
        if isinstance(item, str) and len(item.encode("utf-8")) > 128:
            result[f"{key}_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
    return result
