"""Consume one child's NDJSON without retaining private model/tool payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from agent_control_plane.shared.agy_activity import EVENT_LIMIT, SAFE_SUMMARIES, agy_activity_paths

LINE_LIMIT = 262_144
STEP_INDEX_LIMIT = 1_000_000_000
DIAGNOSTIC_LIMIT = 12_000
PROGRESS_EVENT_INTERVAL_SEC = 2.0
PROGRESS_FILTER_BYTES = 8192
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b].*?(?:\x07|\x1b\\)")
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


def private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)


def vendor_log_path(log_path: Path) -> Path:
    return log_path.with_suffix(".agy-vendor.log")


class AgyOutput:
    def __init__(self, log_path: Path, job_id: str) -> None:
        self.snapshot_path, self.events_path = agy_activity_paths(log_path)
        private_file(self.events_path)
        self.closed = False
        self.discarding = False
        self.buffer = ""
        self.invalid = False
        self.response: str | None = None
        self.result_error: str | None = None
        self.failure_message: str | None = None
        self.diagnostics = ""
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_LIMIT)
        self.usage_history_complete = True
        self.highest_step: dict[str, int] = {}
        self.results = 0
        self.steps: dict[tuple[str, int], str] = {}
        self.progress_sequence = 0
        self.progress_filters: dict[tuple[str, int], bytearray] = {}
        self.last_progress_event_mono = 0.0
        self.quiet = False
        self.step_usage: dict[int, dict[str, int]] = {}
        self.snapshot: dict[str, Any] = {
            "schema": 1,
            "job_id": job_id,
            "attempt_ref": log_path.stem,
            "seq": 0,
            "last_event_at": None,
            "last_progress_at": None,
            "current_action": SAFE_SUMMARIES["started"],
            "current_tool_name": None,
            "current_state": None,
            "current_step_index": None,
            "action_started_at": None,
            "event_count": 0,
            "tool_calls": 0,
            "model_responses": 0,
            "usage": {"available": False},
            "stream_state": "running",
            "conversation_id": None,
        }
        self._publish("started")

    def _publish(
        self,
        kind: str,
        *,
        state: str | None = None,
        tool: str | None = None,
        step_index: int | None = None,
        continuing: bool = False,
    ) -> str:
        now = datetime.now(UTC).isoformat()
        self.snapshot["seq"] += 1
        self.snapshot["last_event_at"] = now
        if kind in ("tool", "model", "result"):
            if not continuing:
                self._progress()
            self.last_progress_event_mono = time.monotonic()
        self.snapshot["current_action"] = SAFE_SUMMARIES[kind]
        self.snapshot["current_tool_name"] = tool
        self.snapshot["current_state"] = (
            "done" if kind in ("result", "closed", "controller_stopped") else state
        )
        self.snapshot["current_step_index"] = step_index
        if not continuing:
            self.snapshot["action_started_at"] = now
        event = {key: self.snapshot[key] for key in ("seq", "job_id", "attempt_ref")}
        event.update(at=now, kind=kind, summary=SAFE_SUMMARIES[kind])
        if state is not None:
            event["state"] = state
        if tool is not None:
            event["tool_name"] = tool
        self.events.append(event)
        if self.snapshot["seq"] <= EVENT_LIMIT:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
        else:
            temporary = self.events_path.with_suffix(".tmp")
            private_file(temporary)
            with temporary.open("w", encoding="utf-8") as handle:
                for retained in self.events:
                    handle.write(json.dumps(retained) + "\n")
            temporary.replace(self.events_path)
        self._save()
        return (
            f"[agy] {SAFE_SUMMARIES[kind]}"
            + (f" ({tool})" if tool else "")
            + (f": {state}" if state else "")
            + "\n"
        )

    def _progress(self) -> None:
        self.progress_sequence += 1
        self.snapshot["last_progress_at"] = datetime.now(UTC).isoformat()
        self.quiet = False

    @property
    def has_active_operation(self) -> bool:
        return self.results == 0 and self.snapshot["current_state"] == "active"

    def note_quiet(self, log: TextIO) -> None:
        if self.quiet or not self.has_active_operation:
            return
        self.quiet = True
        log.write(
            self._publish(
                "quiet",
                state="active",
                tool=self.snapshot["current_tool_name"],
                step_index=self.snapshot["current_step_index"],
                continuing=True,
            )
        )
        log.flush()

    def _content_progress(self, key: tuple[str, int], step: dict[str, Any]) -> bool:
        content = step.get("text_delta")
        if key[0] == "tool":
            info = step.get("tool_info")
            content = info.get("output") if isinstance(info, dict) else None
        if not content or not isinstance(content, (str, dict, list)):
            return False
        # Fixed 65,536-bit seen filter, never cleared within a retained step. Collisions
        # can conservatively suppress progress; replay never becomes fresh after eviction.
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).digest()
        seen = self.progress_filters.setdefault(key, bytearray(PROGRESS_FILTER_BYTES))
        positions: list[int] = []
        for offset in range(0, 8, 2):
            position = int.from_bytes(fingerprint[offset : offset + 2], "big")
            while position in positions:
                position = (position + 1) & 0xFFFF
            positions.append(position)
        fresh = any(not seen[position // 8] & (1 << (position % 8)) for position in positions)
        for position in positions:
            seen[position // 8] |= 1 << (position % 8)
        return fresh

    def _save(self) -> None:
        temporary = self.snapshot_path.with_suffix(".tmp")
        private_file(temporary)
        temporary.write_text(json.dumps(self.snapshot), encoding="utf-8")
        temporary.replace(self.snapshot_path)

    def feed(self, chunk: str, log: TextIO) -> None:
        for part in chunk.splitlines(keepends=True):
            self.buffer += part
            if len(self.buffer) > LINE_LIMIT:
                self.invalid = True
                self.buffer = ""
                self.discarding = not part.endswith("\n")
                continue
            if self.discarding:
                self.buffer = ""
                self.discarding = not part.endswith("\n")
                continue
            if part.endswith("\n"):
                line, self.buffer = self.buffer.strip(), ""
                if line:
                    self._line(line, log)

    @property
    def diagnostic_tail(self) -> str:
        # JSON payloads, including quoted model/tool output, are never diagnostics.
        partial = ANSI_RE.sub("", self.buffer).lstrip()
        if partial.startswith(("{", "[", '"')):
            partial = ""
        return (self.diagnostics + partial)[-DIAGNOSTIC_LIMIT:]

    def _conversation(self, value: Any) -> bool:
        if value is None:
            return True
        try:
            conversation = str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            self.invalid = True
            return False
        previous = self.snapshot["conversation_id"]
        if previous is not None and previous != conversation:
            self.invalid = True
            return False
        self.snapshot["conversation_id"] = conversation
        return True

    @staticmethod
    def _usage(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict) or not any(key in value for key in USAGE_KEYS):
            return None
        if any(
            type(value.get(key, 0)) is not int or not 0 <= value.get(key, 0) <= 10**15
            for key in USAGE_KEYS
        ):
            return None
        return {key: value.get(key, 0) for key in USAGE_KEYS}

    def _line(self, line: str, log: TextIO) -> None:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            diagnostic = ANSI_RE.sub("", line).lstrip()
            if not diagnostic.startswith(("{", "[", '"')):
                self.diagnostics = (self.diagnostics + "\n" + diagnostic)[-DIAGNOSTIC_LIMIT:]
            self.invalid = True
            return
        if not isinstance(event, dict):
            self.invalid = True
            return
        if log.tell() >= LINE_LIMIT:
            log.seek(0)
            log.truncate()
            log.write("[agy] Earlier safe activity omitted\n")
        self.snapshot["event_count"] += 1
        if (
            event.get("job_id", self.snapshot["job_id"]) != self.snapshot["job_id"]
            or event.get("attempt_ref", self.snapshot["attempt_ref"])
            != self.snapshot["attempt_ref"]
            or not self._conversation(event.get("conversation_id"))
        ):
            self.invalid = True
            return
        kind = event.get("event")
        if kind == "result":
            self.results += 1
            result = event.get("result")
            if not isinstance(result, dict):
                self.invalid = True
                return
            if not self._conversation(result.get("conversation_id")):
                return
            response = result.get("response")
            if not isinstance(response, str):
                self.invalid = True
                return
            valid_result = (
                self.results == 1
                and isinstance(result.get("status"), str)
                and bool(result["status"])
                and len(response) <= 64_000
            )
            if not valid_result:
                self.invalid = True
            elif result["status"] != "SUCCESS":
                self.result_error = "AGY CLI returned a non-success result status"
            elif not response.strip():
                # Native print-timeout probes can exit zero with SUCCESS and no response.
                self.result_error = "AGY CLI returned SUCCESS with an empty final response"
            else:
                self.response = response
            usage = self._usage(result.get("usage"))
            if usage is not None:
                self.snapshot["usage"] = {"available": True, **usage}
            if valid_result:
                log.write(self._publish("result"))
        elif kind == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                self.invalid = True
                return
            if not self._conversation(step.get("conversation_id")):
                return
            step_type, index, state = (
                step.get("step_type"),
                step.get("step_index"),
                step.get("state"),
            )
            if (
                step_type not in ("tool", "agent_response")
                or type(index) is not int
                or not 0 <= index <= STEP_INDEX_LIMIT
            ):
                return
            state = state.lower() if isinstance(state, str) else None
            if state not in ("active", "done"):
                return
            key = (step_type, index)
            previous = self.steps.get(key)
            if (
                self.results
                or previous == "done"
                or index < max(self.highest_step.values(), default=-1)
                or (previous is None and index <= self.highest_step.get(step_type, -1))
            ):
                return
            self.highest_step[step_type] = max(index, self.highest_step.get(step_type, -1))
            usage = self._usage(step.get("usage"))
            previous_usage = self.step_usage.get(index, dict.fromkeys(USAGE_KEYS, 0))
            usage_progress = usage is not None and any(
                usage[name] > previous_usage[name] for name in USAGE_KEYS
            )
            content_progress = self._content_progress(key, step)
            if usage is not None and self.results == 0:
                if index not in self.step_usage and len(self.step_usage) >= EVENT_LIMIT:
                    self.usage_history_complete = False
                    self.step_usage.pop(next(iter(self.step_usage)))
                self.step_usage[index] = {
                    name: max(usage[name], previous_usage[name]) for name in USAGE_KEYS
                }
                self.snapshot["usage"] = {
                    "available": self.usage_history_complete,
                    **{
                        name: sum(item[name] for item in self.step_usage.values())
                        for name in USAGE_KEYS
                    },
                }
            continuing = previous == state
            if continuing:
                if not usage_progress and not content_progress:
                    return
                was_quiet = self.quiet
                self._progress()
                if (
                    not was_quiet
                    and time.monotonic() - self.last_progress_event_mono
                    < PROGRESS_EVENT_INTERVAL_SEC
                ):
                    self._save()
                    return
            if key not in self.steps and len(self.steps) >= EVENT_LIMIT:
                evicted = next(iter(self.steps))
                self.steps.pop(evicted)
                self.progress_filters.pop(evicted, None)
            self.steps[key] = state
            tool = step.get("tool_name")
            if not (isinstance(tool, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", tool)):
                tool = (
                    self.snapshot["current_tool_name"] or "tool"
                    if continuing and self.snapshot["current_step_index"] == index
                    else "tool"
                )
            category = "tool" if step_type == "tool" else "model"
            if previous is None:
                self.snapshot["tool_calls" if category == "tool" else "model_responses"] += 1
            if self.snapshot["seq"] >= EVENT_LIMIT and self.snapshot["seq"] % EVENT_LIMIT == 0:
                log.seek(0)
                log.truncate()
                log.write("[agy] Earlier safe activity omitted\n")
            log.write(
                self._publish(
                    category,
                    state=state,
                    tool=tool if category == "tool" else None,
                    step_index=index,
                    continuing=continuing,
                )
            )
        self._save()
        log.flush()

    def finish(self, *, clean: bool, controller_stopped: bool = False) -> str | None:
        self.closed = True
        if self.buffer.strip() and not controller_stopped:
            self.invalid = True
        self.failure_message = (
            self.result_error if clean and not self.invalid and self.results == 1 else None
        )
        self.snapshot["stream_state"] = (
            "closed"
            if (clean or controller_stopped) and not self.invalid and self.result_error is None
            else "error"
        )
        self._publish("controller_stopped" if controller_stopped else "closed")
        return self.response if clean and not self.invalid and self.results == 1 else None
