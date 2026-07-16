from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_VERIFICATION_BYTES = 64 * 1024
_REQUIRED_KEYS = frozenset({"schema_version", "status", "changed_files", "checks", "unverified"})
_STATUSES = frozenset({"completed", "partial", "blocked"})
_CHANGE_KINDS = frozenset({"added", "modified", "deleted", "renamed", "untracked"})
_CHECK_OUTCOMES = frozenset({"passed", "failed", "not_run"})


@dataclass(frozen=True)
class VerificationInspection:
    state: str
    path: Path
    schema_version: int | None = None
    payload: dict[str, Any] | None = None
    sha256: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "path": str(self.path),
            "schema_version": self.schema_version,
            "payload": self.payload,
            "sha256": self.sha256,
            "error": self.error,
            "claims_trust": "worker_reported",
        }


def verification_path_for_result(result_path: Path) -> Path:
    return result_path.with_name("verification.json")


def inspect_verification_report(
    result_path: Path,
    *,
    expected_status: str | None = None,
    started_at: float | None = None,
) -> VerificationInspection:
    path = verification_path_for_result(result_path)
    if not path.exists():
        return VerificationInspection(state="missing", path=path)
    try:
        stat = path.stat()
        if started_at is not None and stat.st_mtime < started_at:
            raise ValueError("verification.json is older than job start")
        if stat.st_size > MAX_VERIFICATION_BYTES:
            raise ValueError(f"verification.json exceeds {MAX_VERIFICATION_BYTES} byte limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        normalized = _validate_payload(payload, expected_status=expected_status)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return VerificationInspection(state="invalid", path=path, error=str(exc))
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return VerificationInspection(
        state="valid",
        path=path,
        schema_version=1,
        payload=normalized,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _validate_payload(payload: Any, *, expected_status: str | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("verification.json must contain a JSON object")
    keys = set(payload)
    missing = sorted(_REQUIRED_KEYS - keys)
    unknown = sorted(keys - _REQUIRED_KEYS)
    if missing:
        raise ValueError(f"verification.json is missing keys: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"verification.json has unknown keys: {', '.join(unknown)}")
    if payload["schema_version"] != 1:
        raise ValueError("verification.json schema_version must be 1")
    status = _choice("status", payload["status"], _STATUSES)
    if expected_status is not None and status != expected_status:
        raise ValueError(
            f"verification status {status!r} does not match result status {expected_status!r}"
        )
    changed_files = _changed_files(payload["changed_files"])
    checks = _checks(payload["checks"])
    if status == "completed" and changed_files:
        if not checks:
            raise ValueError("completed changes require at least one check")
        if any(
            check["outcome"] != "passed" or check["exit_code"] not in {None, 0} for check in checks
        ):
            raise ValueError("completed changes require only passed checks with a zero exit code")
    unverified = _string_list("unverified", payload["unverified"], limit=200)
    return {
        "schema_version": 1,
        "status": status,
        "changed_files": changed_files,
        "checks": checks,
        "unverified": unverified,
    }


def _changed_files(value: Any) -> list[dict[str, str]]:
    items = _object_list("changed_files", value, limit=1000)
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if set(item) != {"path", "change"}:
            raise ValueError(f"changed_files[{index}] must contain only path and change")
        path = _nonempty_string(f"changed_files[{index}].path", item["path"])
        change = _choice(
            f"changed_files[{index}].change",
            item["change"],
            _CHANGE_KINDS,
        )
        normalized.append({"path": path.replace("\\", "/"), "change": change})
    return normalized


def _checks(value: Any) -> list[dict[str, Any]]:
    items = _object_list("checks", value, limit=200)
    normalized: list[dict[str, Any]] = []
    required = {"command", "cwd", "outcome", "exit_code", "summary"}
    for index, item in enumerate(items):
        if set(item) != required:
            raise ValueError(
                f"checks[{index}] must contain only command, cwd, outcome, exit_code, summary"
            )
        exit_code = item["exit_code"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError(f"checks[{index}].exit_code must be an integer or null")
        normalized.append(
            {
                "command": _nonempty_string(f"checks[{index}].command", item["command"]),
                "cwd": _nonempty_string(f"checks[{index}].cwd", item["cwd"]),
                "outcome": _choice(
                    f"checks[{index}].outcome",
                    item["outcome"],
                    _CHECK_OUTCOMES,
                ),
                "exit_code": exit_code,
                "summary": _nonempty_string(f"checks[{index}].summary", item["summary"]),
            }
        )
    return normalized


def _object_list(name: str, value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be an array of objects")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} item limit")
    return value


def _string_list(name: str, value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of strings")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} item limit")
    return [_nonempty_string(f"{name}[{index}]", item) for index, item in enumerate(value)]


def _choice(name: str, value: Any, choices: frozenset[str]) -> str:
    normalized = _nonempty_string(name, value)
    if normalized not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return normalized


def _nonempty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()
