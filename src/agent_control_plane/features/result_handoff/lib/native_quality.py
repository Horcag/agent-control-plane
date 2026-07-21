from __future__ import annotations

import json
import os
import shlex
import subprocess  # nosec B404
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from agent_control_plane.shared.config import NativeQualityGateConfig
from agent_control_plane.shared.native_quality import (
    NativeQualityContract,
    expand_native_quality_command,
    format_gate_command,
    selected_native_quality_gates,
)
from agent_control_plane.shared.path_rules import is_same_or_child

MAX_QUALITY_OUTPUT_CHARS = 8_000


def _resolve_gate_executable(cwd: Path, command: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve a workspace-relative executable path against ``cwd``.

    ``CreateProcess`` on some hosts refuses to resolve a relative path even
    when the process cwd is correct, so gates that name their executable as a
    workspace-relative path (e.g. ``.venv/Scripts/python.exe``) need it
    resolved to an absolute path before ``subprocess.run``. Bare program
    names (no directory separator) and already-absolute paths are left
    untouched so PATH lookup keeps working.
    """
    if not command:
        return command
    executable = command[0]
    if os.path.isabs(executable):
        return command
    if not (os.sep in executable or (os.altsep and os.altsep in executable)):
        return command
    candidate = cwd / executable
    if not candidate.exists():
        return command
    return (str(candidate.resolve(strict=False)), *command[1:])


class _BinaryOutput(Protocol):
    def flush(self) -> None: ...

    def tell(self) -> int: ...

    def seek(self, offset: int) -> int: ...

    def read(self, size: int = -1) -> bytes: ...


class NativeQualityGateRunner:
    """Run configured, non-shell checks and persist controller-owned evidence."""

    def run(
        self,
        *,
        workspace_path: Path,
        run_dir: Path,
        checkpoint_tree_sha: str,
        changed_files: tuple[str, ...],
        command_files: tuple[str, ...] | None = None,
        contract: NativeQualityContract,
        controller_gate_mode: str = "full",
    ) -> dict[str, Any]:
        resolved_command_files = changed_files if command_files is None else command_files
        selected = selected_native_quality_gates(
            contract,
            changed_files,
            stage="controller",
            command_files=resolved_command_files,
            controller_gate_mode=controller_gate_mode,
        )
        checks = self._run_selected_gates(
            workspace_path,
            selected,
            resolved_command_files,
            max_parallel=contract.max_parallel,
        )
        if not selected:
            status = "failed"
            reason = "no configured quality gate matched the changed files"
        elif all(check["outcome"] == "passed" for check in checks):
            status = "passed"
            reason = None
        else:
            status = "failed"
            reason = "one or more controller quality gates failed"
        report = {
            "schema_version": 3,
            "status": status,
            "reason": reason,
            "checkpoint_tree_sha": checkpoint_tree_sha,
            "contract_sha256": contract.sha256,
            "controller_gate_mode": controller_gate_mode,
            "changed_files": list(changed_files),
            "command_files": list(resolved_command_files),
            "max_parallel": contract.max_parallel,
            "checks": checks,
            "claims_trust": "controller_executed",
        }
        _write_report(run_dir / "native-quality.json", report)
        return report

    def _run_selected_gates(
        self,
        workspace_path: Path,
        gates: tuple[NativeQualityGateConfig, ...],
        command_files: tuple[str, ...],
        *,
        max_parallel: int,
    ) -> list[dict[str, Any]]:
        if not gates:
            return []
        worker_count = min(max_parallel, len(gates))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return list(
                executor.map(
                    lambda gate: self._run_gate(workspace_path, gate, command_files),
                    gates,
                )
            )

    def _run_gate(
        self,
        workspace_path: Path,
        gate: NativeQualityGateConfig,
        command_files: tuple[str, ...],
    ) -> dict[str, Any]:
        workspace = workspace_path.resolve(strict=False)
        cwd = (workspace / gate.working_dir).resolve(strict=False)
        started = time.monotonic()
        command = expand_native_quality_command(gate, command_files)
        if not is_same_or_child(cwd, workspace):
            return _check_result(
                gate,
                cwd,
                outcome="error",
                exit_code=None,
                duration_ms=_duration_ms(started),
                output="quality gate working_dir escaped the task workspace",
                command=command,
            )
        if not cwd.is_dir():
            return _check_result(
                gate,
                cwd,
                outcome="error",
                exit_code=None,
                duration_ms=_duration_ms(started),
                output="quality gate working_dir does not exist",
                command=command,
            )
        with tempfile.TemporaryFile() as output_file:
            spawn_command = _resolve_gate_executable(cwd, command)
            try:
                completed = subprocess.run(  # nosec B603
                    list(spawn_command),
                    cwd=cwd,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    timeout=gate.timeout_sec,
                )
            except subprocess.TimeoutExpired:
                output = _read_output_tail(output_file)
                return _check_result(
                    gate,
                    cwd,
                    outcome="timed_out",
                    exit_code=None,
                    duration_ms=_duration_ms(started),
                    output=output or f"timed out after {gate.timeout_sec}s",
                    command=command,
                )
            except (OSError, ValueError) as exc:
                return _check_result(
                    gate,
                    cwd,
                    outcome="error",
                    exit_code=None,
                    duration_ms=_duration_ms(started),
                    output=str(exc),
                    command=command,
                )
            output = _read_output_tail(output_file)
        return _check_result(
            gate,
            cwd,
            outcome="passed" if completed.returncode == 0 else "failed",
            exit_code=completed.returncode,
            duration_ms=_duration_ms(started),
            output=output,
            command=command,
        )


def _check_result(
    gate: NativeQualityGateConfig,
    cwd: Path,
    *,
    outcome: str,
    exit_code: int | None,
    duration_ms: int,
    output: str,
    command: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "name": gate.name,
        "command": list(command),
        "command_display": shlex.join(command),
        "cwd": str(cwd),
        "outcome": outcome,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "output_tail": output[-MAX_QUALITY_OUTPUT_CHARS:],
    }


def _read_output_tail(output_file: _BinaryOutput) -> str:
    output_file.flush()
    size = output_file.tell()
    output_file.seek(max(0, size - MAX_QUALITY_OUTPUT_CHARS * 4))
    return output_file.read().decode("utf-8", errors="replace")[-MAX_QUALITY_OUTPUT_CHARS:].strip()


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_native_quality_report(
    run_dir: Path,
    *,
    checkpoint_tree_sha: str,
    changed_files: tuple[str, ...],
    command_files: tuple[str, ...] | None = None,
    contract: NativeQualityContract,
    controller_gate_mode: str = "full",
) -> dict[str, Any]:
    path = run_dir / "native-quality.json"
    if not path.exists():
        return {
            "state": "missing",
            "path": str(path),
            "payload": None,
            "error": "controller quality report is missing",
            "claims_trust": "controller_executed",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _validate_report(
            payload,
            checkpoint_tree_sha=checkpoint_tree_sha,
            changed_files=changed_files,
            command_files=changed_files if command_files is None else command_files,
            contract=contract,
            controller_gate_mode=controller_gate_mode,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "state": "invalid",
            "path": str(path),
            "payload": None,
            "error": str(exc),
            "claims_trust": "controller_executed",
        }
    return {
        "state": "valid",
        "path": str(path),
        "payload": payload,
        "error": None,
        "claims_trust": "controller_executed",
    }


def _validate_report(
    payload: Any,
    *,
    checkpoint_tree_sha: str,
    changed_files: tuple[str, ...],
    command_files: tuple[str, ...],
    contract: NativeQualityContract,
    controller_gate_mode: str,
) -> None:
    required = {
        "schema_version",
        "status",
        "reason",
        "checkpoint_tree_sha",
        "contract_sha256",
        "controller_gate_mode",
        "changed_files",
        "command_files",
        "max_parallel",
        "checks",
        "claims_trust",
    }
    if isinstance(payload, dict) and payload.get("schema_version") == 2:
        required.remove("controller_gate_mode")
        if controller_gate_mode != "full":
            raise ValueError("legacy controller quality reports require full gate mode")
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("controller quality report has an unexpected shape")
    if payload["schema_version"] not in {2, 3}:
        raise ValueError("unsupported controller quality report schema")
    if payload["checkpoint_tree_sha"] != checkpoint_tree_sha:
        raise ValueError("controller quality report belongs to a different checkpoint tree")
    if payload["contract_sha256"] != contract.sha256:
        raise ValueError("controller quality report belongs to a different quality contract")
    if payload["schema_version"] == 3 and payload["controller_gate_mode"] != controller_gate_mode:
        raise ValueError("controller quality report gate mode drifted")
    if payload["changed_files"] != list(changed_files):
        raise ValueError(
            "controller quality report changed-files evidence does not match checkpoint"
        )
    if payload["command_files"] != list(command_files):
        raise ValueError("controller quality report command files do not match checkpoint")
    reported_parallelism = payload["max_parallel"]
    if (
        isinstance(reported_parallelism, bool)
        or not isinstance(reported_parallelism, int)
        or reported_parallelism != contract.max_parallel
    ):
        raise ValueError("controller quality report parallelism differs from its contract")
    if payload["claims_trust"] != "controller_executed":
        raise ValueError("controller quality report has an invalid trust marker")
    checks = payload["checks"]
    if not isinstance(checks, list) or not all(isinstance(check, dict) for check in checks):
        raise ValueError("controller quality report checks must be an array of objects")
    selected = selected_native_quality_gates(
        contract,
        changed_files,
        stage="controller",
        command_files=command_files,
        controller_gate_mode=controller_gate_mode,
    )
    if [check.get("name") for check in checks] != [gate.name for gate in selected]:
        raise ValueError("controller quality report does not contain the selected gates")
    for check, gate in zip(checks, selected, strict=True):
        required_check_keys = {
            "name",
            "command",
            "command_display",
            "cwd",
            "outcome",
            "exit_code",
            "duration_ms",
            "output_tail",
        }
        if set(check) != required_check_keys:
            raise ValueError(f"controller quality report check shape is invalid for {gate.name}")
        expected_command = expand_native_quality_command(gate, command_files)
        if check.get("command") != list(expected_command):
            raise ValueError(f"controller quality report command drifted for gate {gate.name}")
        if check.get("command_display") != format_gate_command(gate, command_files):
            raise ValueError(f"controller quality display command drifted for gate {gate.name}")
        if not isinstance(check.get("cwd"), str) or not check["cwd"].strip():
            raise ValueError(f"controller quality report cwd is invalid for gate {gate.name}")
        outcome = check.get("outcome")
        if outcome not in {"passed", "failed", "timed_out", "error"}:
            raise ValueError(f"controller quality report outcome is invalid for gate {gate.name}")
        exit_code = check.get("exit_code")
        if outcome == "passed" and exit_code != 0:
            raise ValueError(f"controller quality report exit code contradicts gate {gate.name}")
        if outcome == "failed" and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == 0
        ):
            raise ValueError(f"controller quality report exit code contradicts gate {gate.name}")
        if outcome in {"timed_out", "error"} and exit_code is not None:
            raise ValueError(f"controller quality report exit code contradicts gate {gate.name}")
        duration_ms = check.get("duration_ms")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError(f"controller quality report duration is invalid for gate {gate.name}")
        if not isinstance(check.get("output_tail"), str):
            raise ValueError(f"controller quality report output is invalid for gate {gate.name}")
    status = payload["status"]
    if status not in {"passed", "failed"}:
        raise ValueError("controller quality report status must be passed or failed")
    all_passed = bool(checks) and all(check.get("outcome") == "passed" for check in checks)
    if (status == "passed") != all_passed:
        raise ValueError("controller quality report status contradicts its checks")
    reason = payload["reason"]
    if status == "passed" and reason is not None:
        raise ValueError("passed controller quality report must not contain a failure reason")
    if status == "failed" and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("failed controller quality report requires a reason")
