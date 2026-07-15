from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_control_plane.features.result_handoff.lib.slot_checkpoint import (
    SlotCheckpoint,
    SlotCheckpointError,
    checkpoint_changed_files,
    verify_slot_checkpoint,
)
from agent_control_plane.shared.verification_report import inspect_verification_report

_SECTION_LABELS = {
    "changed_files": "Changed files",
    "what_changed": "What changed",
    "verification_performed": "Verification performed",
    "remaining_risks": "Not verified / remaining risks",
}
_SECTION_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:#{1,6}\s*)?(?:\*\*)?"
    r"(Changed files|What changed|Verification performed|"
    r"Not verified\s*/\s*remaining risks)(?:\*\*)?\s*:?[ \t]*(.*)$",
    re.IGNORECASE,
)
_STATUS_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?Status(?:\*\*)?\s*:\s*"
    r"(?:\*\*)?(completed|success|partial|blocked)(?:\*\*)?\s*$",
    re.IGNORECASE,
)
_EMPTY_CLAIMS = frozenset({"none", "no files", "nothing", "n/a", "not applicable"})


def parse_result_report(text: str) -> dict[str, Any]:
    """Parse the mandatory result envelope without treating worker claims as proof."""
    status: str | None = None
    current_section: str | None = None
    seen: set[str] = set()
    section_lines: dict[str, list[str]] = {name: [] for name in _SECTION_LABELS}
    for raw_line in text.splitlines():
        status_match = _STATUS_PATTERN.match(raw_line)
        if status_match:
            status = "completed" if status_match.group(1).lower() == "success" else status_match.group(1).lower()
            current_section = None
            continue
        section_match = _SECTION_PATTERN.match(raw_line)
        if section_match:
            current_section = _section_key(section_match.group(1))
            seen.add(current_section)
            inline = section_match.group(2).strip()
            if inline:
                section_lines[current_section].append(inline)
            continue
        if current_section is not None and raw_line.strip():
            section_lines[current_section].append(raw_line.strip())

    missing = [name for name in _SECTION_LABELS if name not in seen]
    cleaned = {
        name: [_clean_claim(line) for line in lines if _clean_claim(line)]
        for name, lines in section_lines.items()
    }
    return {
        "status": status,
        "format_valid": status is not None and not missing,
        "missing_sections": missing,
        "sections": cleaned,
        "changed_files_claimed": _claimed_files(cleaned["changed_files"]),
        "verification_claims": _nonempty_claims(cleaned["verification_performed"]),
        "remaining_risks": _nonempty_claims(cleaned["remaining_risks"]),
        "claims_trust": "worker_reported",
    }


def build_verification_bundle(
    result_path: Path | None,
    *,
    workspace_path: Path | None = None,
    checkpoint: SlotCheckpoint | None = None,
    checkpoint_error: str | None = None,
) -> dict[str, Any]:
    result_error: str | None = None
    try:
        text = result_path.read_text(encoding="utf-8", errors="replace") if result_path else ""
    except OSError as exc:
        text = ""
        result_error = str(exc)
    result = parse_result_report(text)
    worker_verification = (
        inspect_verification_report(
            result_path,
            expected_status=result["status"],
        ).as_dict()
        if result_path is not None
        else {
            "state": "missing",
            "path": None,
            "schema_version": None,
            "payload": None,
            "sha256": None,
            "error": "result path is unavailable",
            "claims_trust": "worker_reported",
        }
    )
    actual_changes: list[dict[str, str]] = []
    artifact_error = checkpoint_error
    checkpoint_verified = False
    if checkpoint is not None:
        try:
            verify_slot_checkpoint(workspace_path or checkpoint.workspace_path, checkpoint)
            actual_changes = checkpoint_changed_files(
                workspace_path or checkpoint.workspace_path,
                checkpoint,
            )
            checkpoint_verified = True
        except (OSError, SlotCheckpointError) as exc:
            artifact_error = artifact_error or str(exc)

    claimed = set(result["changed_files_claimed"])
    actual = {change["path"] for change in actual_changes}
    worker_payload = worker_verification.get("payload")
    worker_changes = {
        change["path"]
        for change in worker_payload.get("changed_files", [])
    } if isinstance(worker_payload, dict) else set()
    return {
        "schema_version": 1,
        "review_ready": bool(
            result["format_valid"]
            and result_error is None
            and artifact_error is None
            and worker_verification["state"] == "valid"
        ),
        "result": result,
        "result_error": result_error,
        "worker_verification": worker_verification,
        "changed_files_actual": actual_changes,
        "changed_file_claims_not_observed": sorted(claimed - actual) if actual_changes else [],
        "changed_files_not_claimed": sorted(actual - claimed) if claimed else sorted(actual),
        "worker_changed_files_not_observed": (
            sorted(worker_changes - actual) if actual_changes else []
        ),
        "actual_changed_files_missing_from_worker_bundle": sorted(actual - worker_changes),
        "artifact": {
            "kind": "checkpoint" if checkpoint is not None else "result_only",
            "checkpoint_ref": checkpoint.ref_name if checkpoint is not None else None,
            "checkpoint_sha": checkpoint.commit_sha if checkpoint is not None else None,
            "checkpoint_verified": checkpoint_verified,
            "error": artifact_error,
        },
    }


def _section_key(label: str) -> str:
    normalized = " ".join(label.lower().split())
    if normalized.startswith("changed files"):
        return "changed_files"
    if normalized.startswith("what changed"):
        return "what_changed"
    if normalized.startswith("verification performed"):
        return "verification_performed"
    return "remaining_risks"


def _clean_claim(value: str) -> str:
    cleaned = re.sub(r"^\s*[-*+]\s+", "", value.strip())
    return cleaned.strip()


def _nonempty_claims(values: list[str]) -> list[str]:
    return [value for value in values if value.lower().rstrip(".") not in _EMPTY_CLAIMS]


def _claimed_files(values: list[str]) -> list[str]:
    claimed: list[str] = []
    for value in values:
        if value.lower().rstrip(".") in _EMPTY_CLAIMS:
            continue
        for part in value.split(";"):
            code_match = re.search(r"`([^`]+)`", part)
            candidate = code_match.group(1) if code_match else part
            candidate = re.sub(r"^(?:[MADRCU?!]{1,2})\s+", "", candidate.strip())
            candidate = re.split(r"\s+(?:-|—)\s+", candidate, maxsplit=1)[0].strip()
            if " -> " in candidate:
                candidate = candidate.rsplit(" -> ", maxsplit=1)[1].strip()
            if candidate:
                claimed.append(candidate.replace("\\", "/"))
    return list(dict.fromkeys(claimed))
