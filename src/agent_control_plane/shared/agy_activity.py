"""Bounded, identity-checked readers for sanitized AGY attempt activity."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

ACTIVITY_LIMIT = 1_048_576
EVENT_LIMIT = 1000
SAFE_SUMMARIES = {
    "started": "AGY stream started",
    "tool": "Tool activity",
    "model": "Model activity",
    "result": "AGY result received",
    "native_history": "Native history advanced",
    "quiet": "No recent progress observed; turn remains in flight",
    "closed": "AGY stream closed",
    "controller_stopped": "Controller stopped AGY after durable result",
}


def agy_activity_paths(log_path: Path) -> tuple[Path, Path]:
    return log_path.with_suffix(".agy-activity.json"), log_path.with_suffix(".agy-events.jsonl")


def _read(path: Path) -> str:
    with path.open("rb") as handle:
        raw = handle.read(ACTIVITY_LIMIT + 1)
    if len(raw) > ACTIVITY_LIMIT:
        raise ValueError("activity exceeds size limit")
    return raw.decode("utf-8")


def _identity(value: Any, log_path: Path, job_id: str | None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("attempt_ref") != log_path.stem:
        raise ValueError("activity attempt identity mismatch")
    if job_id is not None and value.get("job_id") != job_id:
        raise ValueError("activity job identity mismatch")
    return value


def _native_fields(value: dict[str, Any]) -> None:
    if "native_step_index" not in value and "native_step_count" not in value:
        return
    index, count = value.get("native_step_index"), value.get("native_step_count")
    if (
        type(index) is not int
        or type(count) is not int
        or not 0 < count <= index + 1 <= 1_000_000_001
    ):
        raise ValueError("invalid native progress counters")


def read_agy_activity(log_path: Path, *, job_id: str | None = None) -> dict[str, Any]:
    try:
        value = _identity(json.loads(_read(agy_activity_paths(log_path)[0])), log_path, job_id)
        if value.get("schema") != 1:
            raise ValueError("unsupported activity schema")
        # Only controller-produced fields cross this public read boundary.
        fields = {
            "schema",
            "job_id",
            "attempt_ref",
            "seq",
            "last_event_at",
            "last_progress_at",
            "current_action",
            "current_tool_name",
            "current_state",
            "current_step_index",
            "action_started_at",
            "event_count",
            "tool_calls",
            "model_responses",
            "usage",
            "stream_state",
            "conversation_id",
        }
        optional = {"native_step_index", "native_step_count"}
        if set(value) - optional != fields:
            raise ValueError("invalid activity fields")
        _native_fields(value)
        if value["current_action"] not in SAFE_SUMMARIES.values():
            raise ValueError("invalid activity summary")
        if value["current_tool_name"] is not None and (
            not isinstance(value["current_tool_name"], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value["current_tool_name"])
        ):
            raise ValueError("invalid current tool")
        if value["current_state"] not in (None, "active", "done"):
            raise ValueError("invalid current state")
        index = value["current_step_index"]
        if index is not None and (type(index) is not int or not 0 <= index <= 1_000_000_000):
            raise ValueError("invalid current step")
        for key in ("seq", "event_count", "tool_calls", "model_responses"):
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError("invalid activity counter")
        for key in ("last_event_at", "last_progress_at", "action_started_at"):
            if value[key] is not None:
                datetime.fromisoformat(value[key])
        if value["stream_state"] not in ("running", "closed", "error"):
            raise ValueError("invalid stream state")
        if value["conversation_id"] is not None:
            UUID(value["conversation_id"])
        usage = value["usage"]
        if not isinstance(usage, dict) or type(usage.get("available")) is not bool:
            raise ValueError("invalid usage")
        if set(usage) - {
            "available",
            "input_tokens",
            "output_tokens",
            "thinking_tokens",
            "cache_read_tokens",
            "total_tokens",
        }:
            raise ValueError("invalid usage fields")
        if any(
            type(count) is not int or count < 0
            for key, count in usage.items()
            if key != "available"
        ):
            raise ValueError("invalid usage counter")
        return {**value, "available": True, "error": None}
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, AttributeError, RecursionError):
        return {"available": False, "error": "AGY activity missing, invalid, or identity mismatch"}


def read_agy_activity_events(
    log_path: Path, *, job_id: str, after_seq: int = 0, limit: int = 20
) -> dict[str, Any]:
    try:
        events = []
        first_seq = None
        for line in _read(agy_activity_paths(log_path)[1]).splitlines():
            value = _identity(json.loads(line), log_path, job_id)
            if (
                value.get("kind") not in SAFE_SUMMARIES
                or value.get("summary") != SAFE_SUMMARIES[value["kind"]]
                or type(value.get("seq")) is not int
                or set(value)
                - {
                    "seq",
                    "at",
                    "kind",
                    "summary",
                    "job_id",
                    "attempt_ref",
                    "tool_name",
                    "state",
                    "native_step_index",
                    "native_step_count",
                }
            ):
                raise ValueError("invalid activity event")
            _native_fields(value)
            if value["kind"] == "native_history" and "native_step_index" not in value:
                raise ValueError("missing native counters")
            datetime.fromisoformat(value["at"])
            if value.get("state") not in (None, "active", "done"):
                raise ValueError("invalid event state")
            if "tool_name" in value and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value["tool_name"]
            ):
                raise ValueError("invalid tool name")
            if first_seq is None:
                first_seq = value["seq"]
            if value["seq"] > after_seq:
                events.append(value)
        bounded = events[: max(1, min(limit, 100))]
        return {
            "events": bounded,
            "next_seq": bounded[-1]["seq"] if bounded else after_seq,
            "truncated": len(events) > len(bounded)
            or (first_seq is not None and after_seq < first_seq - 1),
            "error": None,
        }
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, AttributeError, RecursionError):
        return {
            "events": [],
            "next_seq": after_seq,
            "truncated": False,
            "error": "AGY activity events missing, invalid, or identity mismatch",
        }
