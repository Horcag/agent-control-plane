from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

STATUS_PATTERNS = (
    re.compile(r"(?im)^\s*Status\s*:\s*(completed|partial|blocked)\b"),
    re.compile(
        r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:Status|Статус)(?:\*\*)?\s*:\s*"
        r"(?:\*\*)?(completed|partial|blocked|завершено|частично|заблокировано)\b"
    ),
    re.compile(r"(?im)^\s*#+\s*Status\s*$[\s\r\n]+(completed|partial|blocked)\b"),
)

STATUS_ALIASES = {
    "завершено": "completed",
    "частично": "partial",
    "заблокировано": "blocked",
}


@dataclass(frozen=True)
class ResultState:
    done: bool
    status: str | None
    reason: str | None = None


def inspect_result(path: Path, started_at: float) -> ResultState:
    if not path.exists():
        return ResultState(done=False, status=None, reason="result file does not exist")
    if path.stat().st_mtime < started_at:
        return ResultState(done=False, status=None, reason="result file is older than job start")

    text = path.read_text(encoding="utf-8", errors="replace")
    if (
        "Awaiting `agy`" in text
        or "Awaiting execution" in text
        or "Awaiting agent execution" in text
    ):
        return ResultState(done=False, status=None, reason="result is still a placeholder")
    if "Not reviewed yet" in text and "Status: blocked" in text:
        return ResultState(done=False, status=None, reason="result is still a placeholder")

    for pattern in STATUS_PATTERNS:
        match = pattern.search(text)
        if match:
            return ResultState(done=True, status=_normalize_status(match.group(1)))
    return ResultState(done=False, status=None, reason="result status marker is missing")


def _normalize_status(value: str) -> str:
    status = value.strip().lower().strip("*")
    return STATUS_ALIASES.get(status, status)
