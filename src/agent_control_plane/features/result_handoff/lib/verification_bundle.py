from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_control_plane.features.result_handoff.lib.native_quality import (
    inspect_native_quality_report,
)
from agent_control_plane.features.result_handoff.lib.slot_checkpoint import (
    SlotCheckpoint,
    SlotCheckpointError,
    checkpoint_changed_files,
    checkpoint_temporary_patch_artifacts,
    verify_slot_checkpoint,
)
from agent_control_plane.shared.native_quality import (
    NativeQualityContract,
    format_gate_command,
    selected_native_quality_gates,
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
            status = (
                "completed"
                if status_match.group(1).lower() == "success"
                else status_match.group(1).lower()
            )
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
    run_dir: Path | None = None,
    source_status: str | None = None,
    workspace_changed: bool | None = None,
    quality_contract: NativeQualityContract | None = None,
    quality_contract_error: str | None = None,
    expected_result_status: str | None = None,
    controller_gate_mode: str = "full",
) -> dict[str, Any]:
    contract = quality_contract or NativeQualityContract(policy="off")
    result_error: str | None = None
    try:
        text = result_path.read_text(encoding="utf-8", errors="replace") if result_path else ""
    except OSError as exc:
        text = ""
        result_error = str(exc)
    result = parse_result_report(text)
    expected_status = expected_result_status or result["status"]
    result_contract_matches = expected_status == result["status"]
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
    temporary_patch_artifacts: tuple[str, ...] = ()
    artifact_error = checkpoint_error
    checkpoint_verified = False
    if checkpoint is not None:
        try:
            verify_slot_checkpoint(workspace_path or checkpoint.workspace_path, checkpoint)
            actual_changes = checkpoint_changed_files(
                workspace_path or checkpoint.workspace_path,
                checkpoint,
            )
            temporary_patch_artifacts = checkpoint_temporary_patch_artifacts(
                workspace_path or checkpoint.workspace_path,
                checkpoint,
            )
            checkpoint_verified = True
        except (OSError, SlotCheckpointError) as exc:
            artifact_error = artifact_error or str(exc)

    claimed = set(result["changed_files_claimed"])
    actual = {change["path"] for change in actual_changes}
    worker_payload = worker_verification.get("payload")
    worker_changes = (
        {change["path"] for change in worker_payload.get("changed_files", [])}
        if isinstance(worker_payload, dict)
        else set()
    )
    changed_paths = tuple(change["path"] for change in actual_changes)
    if not changed_paths:
        changed_paths = tuple(sorted(worker_changes or claimed))
    command_paths = (
        tuple(change["path"] for change in actual_changes if not change["status"].startswith("D"))
        if actual_changes
        else changed_paths
    )
    has_changes = workspace_changed if workspace_changed is not None else bool(changed_paths)
    effective_status = source_status or result["status"]
    quality_required = bool(
        contract.policy != "off" and effective_status == "completed" and has_changes
    )
    worker_quality = _assess_worker_quality(
        worker_verification,
        workspace_path=workspace_path,
        changed_paths=changed_paths,
        command_paths=command_paths,
        checkpoint_paths=(
            tuple(change["path"] for change in actual_changes) if actual_changes else None
        ),
        contract=contract,
        required=quality_required,
    )
    controller_required = bool(
        quality_required
        and contract.policy == "controller"
        and controller_gate_mode != "none"
        and result_contract_matches
        and expected_status == "completed"
        and not temporary_patch_artifacts
    )
    if controller_required and run_dir is not None and checkpoint is not None:
        controller_quality = inspect_native_quality_report(
            run_dir,
            checkpoint_tree_sha=checkpoint.tree_sha,
            changed_files=changed_paths,
            command_files=command_paths,
            contract=contract,
            controller_gate_mode=controller_gate_mode,
        )
    elif controller_required:
        controller_quality = {
            "state": "missing",
            "path": str(run_dir / "native-quality.json") if run_dir is not None else None,
            "payload": None,
            "error": "controller quality requires a verified checkpoint report",
            "claims_trust": "controller_executed",
        }
    else:
        controller_quality = {
            "state": "not_required",
            "path": str(run_dir / "native-quality.json") if run_dir is not None else None,
            "payload": None,
            "error": None,
            "claims_trust": "controller_executed",
        }
    controller_passed = not controller_required or (
        controller_quality["state"] == "valid"
        and isinstance(controller_quality.get("payload"), dict)
        and controller_quality["payload"].get("status") == "passed"
    )
    return {
        "schema_version": 1,
        "review_ready": bool(
            result["format_valid"]
            and result["status"] == "completed"
            and expected_status == "completed"
            and result_contract_matches
            and controller_gate_mode == "full"
            and effective_status == "completed"
            and result_error is None
            and artifact_error is None
            and not temporary_patch_artifacts
            and worker_verification["state"] == "valid"
            and quality_contract_error is None
            and worker_quality["status"] in {"passed", "not_required"}
            and controller_passed
        ),
        "result": result,
        "result_contract": {
            "expected_status": expected_status,
            "reported_status": result["status"],
            "matches": result_contract_matches,
        },
        "controller_gate_mode": controller_gate_mode,
        "result_error": result_error,
        "worker_verification": worker_verification,
        "quality_contract": {
            "policy": contract.policy,
            "sha256": contract.sha256,
            "gates": [gate.name for gate in contract.gates],
            "worker_gates": [
                gate.name for gate in contract.gates if gate.run_on in {"worker", "both"}
            ],
            "controller_gates": [
                gate.name for gate in contract.gates if gate.run_on in {"controller", "both"}
            ],
            "max_parallel": contract.max_parallel,
            "error": quality_contract_error,
        },
        "worker_quality": worker_quality,
        "controller_quality": controller_quality,
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
            "disposition": "contaminated" if temporary_patch_artifacts else "normal",
            "matched_paths": list(temporary_patch_artifacts),
            "error": artifact_error,
        },
    }


def _assess_worker_quality(
    worker_verification: dict[str, Any],
    *,
    workspace_path: Path | None,
    changed_paths: tuple[str, ...],
    command_paths: tuple[str, ...],
    checkpoint_paths: tuple[str, ...] | None,
    contract: NativeQualityContract,
    required: bool,
) -> dict[str, Any]:
    if not required:
        return {
            "required": False,
            "status": "not_required",
            "required_gates": [],
            "missing_gates": [],
            "changed_files_missing": [],
            "changed_files_unobserved": [],
            "reason": None,
            "claims_trust": "worker_reported",
        }
    payload = worker_verification.get("payload")
    checks = payload.get("checks", []) if isinstance(payload, dict) else []
    if worker_verification.get("state") != "valid" or not isinstance(payload, dict):
        return {
            "required": True,
            "status": "failed",
            "required_gates": [],
            "missing_gates": [],
            "changed_files_missing": [],
            "changed_files_unobserved": [],
            "reason": "worker verification is not valid",
            "claims_trust": "worker_reported",
        }
    worker_paths = {
        str(change.get("path", ""))
        for change in payload.get("changed_files", [])
        if isinstance(change, dict) and change.get("path")
    }
    expected_paths = set(checkpoint_paths or ())
    changed_files_missing = sorted(expected_paths - worker_paths)
    changed_files_unobserved = sorted(worker_paths - expected_paths) if checkpoint_paths else []
    if changed_files_missing or changed_files_unobserved:
        selected = selected_native_quality_gates(
            contract,
            changed_paths,
            stage="worker",
            command_files=command_paths,
        )
        return {
            "required": True,
            "status": "failed",
            "required_gates": [gate.name for gate in selected],
            "missing_gates": [],
            "changed_files_missing": changed_files_missing,
            "changed_files_unobserved": changed_files_unobserved,
            "reason": "worker changed-files evidence does not match the checkpoint",
            "claims_trust": "worker_reported",
        }
    selected = selected_native_quality_gates(
        contract,
        changed_paths,
        stage="worker",
        command_files=command_paths,
    )
    mandatory = tuple(gate for gate in selected if gate.run_on == "worker")
    if not mandatory:
        passed = bool(checks) and all(
            check.get("outcome") == "passed" and check.get("exit_code") in {None, 0}
            for check in checks
        )
        return {
            "required": True,
            "status": "passed" if passed else "failed",
            "required_gates": [],
            "missing_gates": [],
            "changed_files_missing": [],
            "changed_files_unobserved": [],
            "reason": None if passed else "no successful worker check was reported",
            "claims_trust": "worker_reported",
        }
    missing: list[str] = []
    for gate in mandatory:
        expected_command = format_gate_command(gate, command_paths)
        if not any(
            _reported_gate_matches(
                check,
                expected_command=expected_command,
                expected_cwd=gate.working_dir,
                workspace_path=workspace_path,
            )
            for check in checks
        ):
            missing.append(gate.name)
    if missing:
        reason = "mandatory worker quality gates are missing from verification.json"
    else:
        reason = None
    return {
        "required": True,
        "status": "passed" if mandatory and not missing else "failed",
        "required_gates": [gate.name for gate in mandatory],
        "missing_gates": missing,
        "changed_files_missing": [],
        "changed_files_unobserved": [],
        "reason": reason,
        "claims_trust": "worker_reported",
    }


def _reported_gate_matches(
    check: dict[str, Any],
    *,
    expected_command: str,
    expected_cwd: Path,
    workspace_path: Path | None,
) -> bool:
    command = str(check.get("command", "")).strip().strip("`")
    if command != expected_command:
        return False
    if check.get("outcome") != "passed" or check.get("exit_code") not in {None, 0}:
        return False
    reported_cwd = str(check.get("cwd", "")).strip()
    if reported_cwd.replace("\\", "/") == expected_cwd.as_posix():
        return True
    if workspace_path is None:
        return False
    try:
        return Path(reported_cwd).resolve(strict=False) == (workspace_path / expected_cwd).resolve(
            strict=False
        )
    except OSError:
        return False


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
