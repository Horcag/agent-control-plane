from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.verification_report import inspect_verification_report

STATUS_PATTERNS = (
    re.compile(r"(?im)^\s*`Status\s*:\s*(completed|success|partial|blocked)\b`\s*$"),
    re.compile(r"(?im)^\s*Status\s*:\s*(completed|success|partial|blocked)\b"),
    re.compile(
        r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:Status|Статус)(?:\*\*)?\s*:\s*"
        r"(?:\*\*)?(completed|success|partial|blocked|завершено|частично|заблокировано)\b"
    ),
    re.compile(r"(?im)^\s*#+\s*Status\s*$[\s\r\n]+(completed|success|partial|blocked)\b"),
)

STATUS_ALIASES = {
    "success": "completed",
    "завершено": "completed",
    "частично": "partial",
    "заблокировано": "blocked",
}
ESCALATION_CLASSIFICATIONS = frozenset(
    {
        "model_capability",
        "infrastructure",
        "workspace",
        "dependency",
        "quota",
        "spawn",
        "tooling",
        "guardrail",
    }
)
ESCALATION_PATTERN = re.compile(r"(?im)^\s*Escalation-Classification\s*:\s*(\S+)\s*$")

CAPACITY_PATTERNS = (
    re.compile(r"(?i)you(?:'|\u2019)ve hit (?:your )?(?:usage|rate) limit"),
    re.compile(r"(?i)(?:usage|rate) limit (?:has been )?(?:reached|exceeded)"),
    re.compile(r"(?i)insufficient (?:quota|capacity)"),
    re.compile(r"(?i)capacity temporarily unavailable"),
)


@dataclass(frozen=True)
class ResultState:
    done: bool
    status: str | None
    reason: str | None = None
    verification_state: str | None = None
    verification_error: str | None = None
    verification_sha256: str | None = None
    escalation_classification: str | None = None


@dataclass(frozen=True)
class ResultContractAssessment:
    expected_status: str
    reported_status: str
    matches: bool
    effective_terminal_status: str


def assess_result_contract(
    expected_status: str,
    reported_status: str,
) -> ResultContractAssessment:
    matches = expected_status == reported_status
    return ResultContractAssessment(
        expected_status=expected_status,
        reported_status=reported_status,
        matches=matches,
        effective_terminal_status=reported_status if matches else "contract_mismatch",
    )


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
            status = _normalize_status(match.group(1))
            verification = inspect_verification_report(
                path,
                expected_status=status,
                started_at=started_at,
            )
            return ResultState(
                done=True,
                status=status,
                verification_state=verification.state,
                verification_error=verification.error,
                verification_sha256=verification.sha256,
                escalation_classification=parse_escalation_classification(text),
            )
    return ResultState(done=False, status=None, reason="result status marker is missing")


def parse_escalation_classification(text: str) -> str | None:
    matches = [match.group(1).strip().lower() for match in ESCALATION_PATTERN.finditer(text)]
    if len(matches) != 1 or matches[0] not in ESCALATION_CLASSIFICATIONS:
        return None
    return matches[0]


def recover_result_from_last_message(
    result_path: Path,
    last_message_path: Path,
    started_at: float,
) -> ResultState:
    current = inspect_result(result_path, started_at)
    if current.done:
        return current

    candidate = inspect_result(last_message_path, started_at)
    if not candidate.done:
        return candidate
    text = last_message_path.read_text(encoding="utf-8", errors="replace")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(text, encoding="utf-8")
    return inspect_result(result_path, started_at)


def contains_capacity_marker(*paths: Path) -> bool:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in CAPACITY_PATTERNS):
            return True
    return False


def _normalize_status(value: str) -> str:
    status = value.strip().lower().strip("*")
    return STATUS_ALIASES.get(status, status)
