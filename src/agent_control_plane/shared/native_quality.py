from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_control_plane.shared.config import (
    ControlConfig,
    NativeQualityGateConfig,
)

NATIVE_QUALITY_CONTRACT_SCHEMA_VERSION = 2
CHANGED_PYTHON_FILES_PLACEHOLDER = "{changed_python_files}"


@dataclass(frozen=True)
class NativeQualityContract:
    policy: str
    gates: tuple[NativeQualityGateConfig, ...] = ()
    max_parallel: int = 1

    def __post_init__(self) -> None:
        if self.policy not in {"off", "worker", "controller"}:
            raise ValueError("native quality policy must be off, worker, or controller")
        if not 1 <= self.max_parallel <= 4:
            raise ValueError("native quality max_parallel must be between 1 and 4")
        if self.policy == "controller" and not any(
            gate.run_on in {"controller", "both"} for gate in self.gates
        ):
            raise ValueError("controller native quality policy requires a controller gate")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NATIVE_QUALITY_CONTRACT_SCHEMA_VERSION,
            "policy": self.policy,
            "max_parallel": self.max_parallel,
            "gates": [
                {
                    "name": gate.name,
                    "command": list(gate.command),
                    "working_dir": gate.working_dir.as_posix(),
                    "timeout_sec": gate.timeout_sec,
                    "include_globs": list(gate.include_globs),
                    "run_on": gate.run_on,
                }
                for gate in self.gates
            ],
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def from_dict(cls, payload: Any) -> NativeQualityContract:
        if not isinstance(payload, dict):
            raise ValueError("native quality contract must be a JSON object")
        schema_version = payload.get("schema_version")
        if schema_version not in {1, NATIVE_QUALITY_CONTRACT_SCHEMA_VERSION}:
            raise ValueError("unsupported native quality contract schema")
        expected_keys = (
            {"schema_version", "policy", "gates"}
            if schema_version == 1
            else {"schema_version", "policy", "max_parallel", "gates"}
        )
        if set(payload) != expected_keys:
            raise ValueError("native quality contract has an unexpected shape")
        gates_raw = payload["gates"]
        if not isinstance(gates_raw, list):
            raise ValueError("native quality contract gates must be an array")
        gates: list[NativeQualityGateConfig] = []
        for item in gates_raw:
            expected_gate_keys = {
                "name",
                "command",
                "working_dir",
                "timeout_sec",
                "include_globs",
            }
            if schema_version == NATIVE_QUALITY_CONTRACT_SCHEMA_VERSION:
                expected_gate_keys.add("run_on")
            if not isinstance(item, dict) or set(item) != expected_gate_keys:
                raise ValueError("native quality contract gate has an unexpected shape")
            command = item["command"]
            include_globs = item["include_globs"]
            if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
                raise ValueError("native quality contract command must be an array of strings")
            if not isinstance(include_globs, list) or not all(
                isinstance(pattern, str) for pattern in include_globs
            ):
                raise ValueError("native quality contract globs must be an array of strings")
            gates.append(
                NativeQualityGateConfig(
                    name=str(item["name"]),
                    command=tuple(command),
                    working_dir=Path(str(item["working_dir"])),
                    timeout_sec=int(item["timeout_sec"]),
                    include_globs=tuple(include_globs),
                    run_on=str(item.get("run_on", "both")),
                )
            )
        return cls(
            policy=str(payload["policy"]),
            gates=tuple(gates),
            max_parallel=int(payload.get("max_parallel", 1)),
        )


@dataclass(frozen=True)
class NativeQualityContractInspection:
    expected: NativeQualityContract
    path: Path
    state: str
    persisted: NativeQualityContract | None = None
    error: str | None = None

    @property
    def persisted_sha256(self) -> str | None:
        return self.persisted.sha256 if self.persisted is not None else None


def resolve_native_quality_contract(
    config: ControlConfig,
    route: str,
    *,
    workspace_access: str,
    read_only: bool,
) -> NativeQualityContract:
    if workspace_access != "native" or read_only:
        return NativeQualityContract(policy="off")
    route_config = config.routes.get(route)
    policy = (
        route_config.native_quality_policy
        if route_config is not None and route_config.native_quality_policy is not None
        else config.defaults.native_quality_policy
    )
    gates = route_config.native_quality_gates if route_config is not None else ()
    max_parallel = route_config.native_quality_max_parallel if route_config is not None else 1
    return NativeQualityContract(policy=policy, gates=gates, max_parallel=max_parallel)


def native_quality_contract_path(run_dir: Path) -> Path:
    return run_dir / "native-quality-contract.json"


def write_native_quality_contract(run_dir: Path, contract: NativeQualityContract) -> Path:
    path = native_quality_contract_path(run_dir)
    _write_json_atomic(path, contract.as_dict())
    return path


def load_native_quality_contract(run_dir: Path) -> NativeQualityContract | None:
    path = native_quality_contract_path(run_dir)
    if not path.exists():
        return None
    return NativeQualityContract.from_dict(json.loads(path.read_text(encoding="utf-8")))


def inspect_native_quality_contract(
    run_dir: Path,
    expected: NativeQualityContract,
) -> NativeQualityContractInspection:
    path = native_quality_contract_path(run_dir)
    try:
        persisted = load_native_quality_contract(run_dir)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return NativeQualityContractInspection(
            expected=expected,
            path=path,
            state="invalid",
            error=str(exc),
        )
    if persisted is None:
        required = expected.policy != "off"
        return NativeQualityContractInspection(
            expected=expected,
            path=path,
            state="missing" if required else "legacy_missing",
            error="persisted native quality contract is missing" if required else None,
        )
    if persisted.sha256 != expected.sha256:
        return NativeQualityContractInspection(
            expected=expected,
            path=path,
            state="drifted",
            persisted=persisted,
            error="persisted native quality contract drifted from controller config",
        )
    return NativeQualityContractInspection(
        expected=expected,
        path=path,
        state="matches",
        persisted=persisted,
    )


def selected_native_quality_gates(
    contract: NativeQualityContract,
    changed_files: tuple[str, ...],
    *,
    stage: str | None = None,
    command_files: tuple[str, ...] | None = None,
    controller_gate_mode: str = "full",
) -> tuple[NativeQualityGateConfig, ...]:
    normalized = tuple(path.replace("\\", "/").removeprefix("./") for path in changed_files)
    available_python_files = _changed_python_files(
        changed_files if command_files is None else command_files
    )
    return tuple(
        gate
        for gate in native_quality_gates_for_stage(
            contract, stage, controller_gate_mode=controller_gate_mode
        )
        if (
            not gate.include_globs
            or any(_matches_any(path, gate.include_globs) for path in normalized)
        )
        and (CHANGED_PYTHON_FILES_PLACEHOLDER not in gate.command or available_python_files)
    )


def native_quality_gates_for_stage(
    contract: NativeQualityContract,
    stage: str | None,
    *,
    controller_gate_mode: str = "full",
) -> tuple[NativeQualityGateConfig, ...]:
    if stage not in {None, "worker", "controller"}:
        raise ValueError("native quality stage must be worker or controller")
    if controller_gate_mode not in {"full", "focused", "none"}:
        raise ValueError("controller gate mode must be full, focused, or none")
    if stage is None:
        return contract.gates
    if stage == "controller" and controller_gate_mode == "none":
        return ()
    if stage == "controller" and controller_gate_mode == "focused":
        return tuple(gate for gate in contract.gates if gate.run_on == "both")
    return tuple(gate for gate in contract.gates if gate.run_on in {stage, "both"})


def expand_native_quality_command(
    gate: NativeQualityGateConfig,
    command_files: tuple[str, ...],
) -> tuple[str, ...]:
    if CHANGED_PYTHON_FILES_PLACEHOLDER not in gate.command:
        return gate.command
    python_files = _changed_python_files(command_files)
    if not python_files:
        raise ValueError(f"quality gate {gate.name} has no changed Python files to expand")
    expanded: list[str] = []
    working_dir = gate.working_dir.as_posix()
    for argument in gate.command:
        if argument == CHANGED_PYTHON_FILES_PLACEHOLDER:
            for path in python_files:
                relative_path = posixpath.relpath(path, start=working_dir)
                expanded.append(
                    relative_path if relative_path.startswith("../") else f"./{relative_path}"
                )
            continue
        expanded.append(argument)
    return tuple(expanded)


def format_gate_command(
    gate: NativeQualityGateConfig,
    command_files: tuple[str, ...] | None = None,
) -> str:
    if command_files is None:
        return shlex.join(gate.command)
    return shlex.join(expand_native_quality_command(gate, command_files))


def _changed_python_files(command_files: tuple[str, ...]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw_path in command_files:
        path = raw_path.replace("\\", "/").removeprefix("./")
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("native quality command files must stay inside the workspace")
        if candidate.suffix.lower() == ".py":
            normalized.add(candidate.as_posix())
    return tuple(sorted(normalized))


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if candidate.match(normalized):
            return True
        if normalized.startswith("**/") and candidate.match(normalized[3:]):
            return True
    return False


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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
