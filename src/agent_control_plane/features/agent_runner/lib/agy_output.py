"""Consume one child's NDJSON without retaining private model/tool payloads."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from agent_control_plane.shared.agy_activity import EVENT_LIMIT, SAFE_SUMMARIES, agy_activity_paths

LINE_LIMIT = 262_144
STEP_INDEX_LIMIT = 1_000_000_000
DIAGNOSTIC_LIMIT = 12_000
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
        self.diagnostics = ""
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_LIMIT)
        self.usage_history_complete = True
        self.highest_step: dict[str, int] = {}
        self.results = 0
        self.steps: dict[tuple[str, int], str] = {}
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
    ) -> str:
        now = datetime.now(UTC).isoformat()
        self.snapshot["seq"] += 1
        self.snapshot["last_event_at"] = now
        if kind in ("tool", "model", "result"):
            self.snapshot["last_progress_at"] = now
        self.snapshot["current_action"] = SAFE_SUMMARIES[kind]
        self.snapshot["current_tool_name"] = tool
        self.snapshot["current_state"] = (
            "done" if kind in ("result", "closed", "controller_stopped") else state
        )
        self.snapshot["current_step_index"] = step_index
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

    def _conversation(self, value: Any) -> None:
        if value is None:
            return
        try:
            conversation = str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            self.invalid = True
            return
        previous = self.snapshot["conversation_id"]
        if previous is not None and previous != conversation:
            self.invalid = True
        else:
            self.snapshot["conversation_id"] = conversation

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
        kind = event.get("event")
        if kind == "init":
            self._conversation(event.get("conversation_id"))
        elif kind == "result":
            self.results += 1
            result = event.get("result")
            if not isinstance(result, dict):
                self.invalid = True
                return
            self._conversation(result.get("conversation_id"))
            response = result.get("response")
            if (
                self.results != 1
                or result.get("status") != "SUCCESS"
                or not isinstance(response, str)
                or len(response) > 64_000
            ):
                self.invalid = True
            else:
                self.response = response
            usage = self._usage(result.get("usage"))
            if usage is not None:
                self.snapshot["usage"] = {"available": True, **usage}
            if self.results == 1:
                log.write(self._publish("result"))
        elif kind == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                self.invalid = True
                return
            self._conversation(step.get("conversation_id"))
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
            if previous is None and index <= self.highest_step.get(step_type, -1):
                return
            self.highest_step[step_type] = max(index, self.highest_step.get(step_type, -1))
            usage = self._usage(step.get("usage"))
            if usage is not None and usage != self.step_usage.get(index):
                self.snapshot["last_event_at"] = datetime.now(UTC).isoformat()
            if usage is not None and self.results == 0:
                if index not in self.step_usage and len(self.step_usage) >= EVENT_LIMIT:
                    self.usage_history_complete = False
                    self.step_usage.pop(next(iter(self.step_usage)))
                self.step_usage[index] = usage
                self.snapshot["usage"] = {
                    "available": self.usage_history_complete,
                    **{
                        name: sum(item[name] for item in self.step_usage.values())
                        for name in USAGE_KEYS
                    },
                }
            if previous == state or previous == "done":
                self._save()
                return
            if key not in self.steps and len(self.steps) >= EVENT_LIMIT:
                self.steps.pop(next(iter(self.steps)))
            self.steps[key] = state
            tool = step.get("tool_name")
            tool = (
                tool
                if isinstance(tool, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", tool)
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
                )
            )
        self._save()
        log.flush()

    def finish(self, *, clean: bool, controller_stopped: bool = False) -> str | None:
        self.closed = True
        if self.buffer.strip() and not controller_stopped:
            self.invalid = True
        self.snapshot["stream_state"] = (
            "closed" if (clean or controller_stopped) and not self.invalid else "error"
        )
        self._publish("controller_stopped" if controller_stopped else "closed")
        return self.response if clean and not self.invalid and self.results == 1 else None
