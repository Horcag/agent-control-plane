from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.shared.path_rules import is_same_or_child


@dataclass(frozen=True)
class CodexSubagentCompletion:
    thread_id: str
    parent_thread_id: str | None
    agent_path: str | None
    agent_nickname: str | None
    cwd: Path
    route: str
    result: str
    completed_at: str | int | float | None
    rollout_path: Path


def scan_codex_subagent_completions(
    sessions_root: Path,
    *,
    workspace_roots: Mapping[str, Path],
    parent_thread_id: str | None = None,
    since_hours: float | None = 72.0,
    max_files: int = 500,
    tail_bytes: int = 2 * 1024 * 1024,
) -> list[CodexSubagentCompletion]:
    if max_files <= 0:
        raise ValueError("max_files must be positive")
    if tail_bytes <= 0:
        raise ValueError("tail_bytes must be positive")
    if since_hours is not None and since_hours < 0:
        raise ValueError("since_hours must be non-negative")
    root = sessions_root.resolve(strict=False)
    if not root.exists():
        return []
    cutoff = None if since_hours is None else time.time() - since_hours * 3600
    candidates: list[tuple[float, Path]] = []
    for path in root.rglob("*.jsonl"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if cutoff is None or modified >= cutoff:
            candidates.append((modified, path))
    candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)

    completions: list[CodexSubagentCompletion] = []
    for _modified, path in candidates[:max_files]:
        completion = _read_completion(
            path,
            workspace_roots=workspace_roots,
            parent_thread_id=parent_thread_id,
            tail_bytes=tail_bytes,
        )
        if completion is not None:
            completions.append(completion)
    return completions


def _read_completion(
    path: Path,
    *,
    workspace_roots: Mapping[str, Path],
    parent_thread_id: str | None,
    tail_bytes: int,
) -> CodexSubagentCompletion | None:
    meta = _read_meta(path)
    if meta is None or not _is_subagent(meta):
        return None
    parent_id = _subagent_text(meta, "parent_thread_id")
    if parent_thread_id is not None and parent_id != parent_thread_id.strip():
        return None
    thread_id = str(meta.get("id") or "").strip()
    cwd_text = str(meta.get("cwd") or "").strip()
    if not thread_id or not cwd_text:
        return None
    cwd = Path(cwd_text).resolve(strict=False)
    route = _matching_route(cwd, workspace_roots)
    if route is None:
        return None
    terminal = _read_last_task_complete(path, tail_bytes=tail_bytes)
    if terminal is None:
        return None
    return CodexSubagentCompletion(
        thread_id=thread_id,
        parent_thread_id=parent_id,
        agent_path=_subagent_text(meta, "agent_path"),
        agent_nickname=_subagent_text(meta, "agent_nickname"),
        cwd=cwd,
        route=route,
        result=str(terminal.get("last_agent_message") or ""),
        completed_at=terminal.get("completed_at") or terminal.get("timestamp"),
        rollout_path=path.resolve(strict=False),
    )


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(64):
                line = handle.readline()
                if not line:
                    return None
                event = _json_object(line)
                if event is not None and event.get("type") == "session_meta":
                    payload = event.get("payload")
                    return payload if isinstance(payload, dict) else None
    except OSError:
        return None
    return None


def _read_last_task_complete(path: Path, *, tail_bytes: int) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
        start = max(0, size - tail_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    separator = data.find(b"\n")
                    data = data[separator + 1 :] if separator >= 0 else b""
    except OSError:
        return None
    terminal: dict[str, Any] | None = None
    for raw_line in data.splitlines():
        event = _json_object(raw_line.decode("utf-8", errors="replace"))
        if event is None or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "task_complete":
            terminal = dict(payload)
            terminal["timestamp"] = event.get("timestamp")
    return terminal


def _matching_route(cwd: Path, workspace_roots: Mapping[str, Path]) -> str | None:
    matches = [
        (len(str(root.resolve(strict=False))), route)
        for route, root in workspace_roots.items()
        if is_same_or_child(cwd, root)
    ]
    if not matches:
        return None
    return max(matches)[1]


def _is_subagent(meta: Mapping[str, Any]) -> bool:
    if meta.get("thread_source") == "subagent":
        return True
    source = meta.get("source")
    return isinstance(source, dict) and isinstance(source.get("subagent"), dict)


def _subagent_text(meta: Mapping[str, Any], key: str) -> str | None:
    direct = _optional_text(meta.get(key))
    if direct is not None:
        return direct
    source = meta.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    nested = _optional_text(subagent.get(key))
    if nested is not None:
        return nested
    thread_spawn = subagent.get("thread_spawn")
    if not isinstance(thread_spawn, dict):
        return None
    return _optional_text(thread_spawn.get(key))


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
