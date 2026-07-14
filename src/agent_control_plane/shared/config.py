from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from agent_control_plane.shared.agent_backends import (
    CODEX_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)


@dataclass(frozen=True)
class RouteConfig:
    name: str
    path: Path
    required_branch: str
    worktree_root: Path | None
    worktree_base: Path
    source_roots: tuple[Path, ...]
    test_roots: tuple[Path, ...]
    exclude_dirs: tuple[Path, ...]
    ide_sdk_name: str | None = None
    ide_sdk_type: str = "Python SDK"
    ide_mcp_server: str | None = None
    agy_mcp_server: str | None = None
    ide_mcp_project_root: Path | None = None
    backend: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    codex_forbidden_tool_markers: tuple[str, ...] | None = None
    monitor_route_root: bool = True


@dataclass(frozen=True)
class SlotConfig:
    name: str
    route: str
    path: Path


@dataclass(frozen=True)
class SlotPrepareCommand:
    name: str
    working_dir: Path
    marker: Path | None
    command: tuple[str, ...]
    timeout_sec: int
    routes: tuple[str, ...]


@dataclass(frozen=True)
class ControlDefaults:
    timeout_sec: int
    idle_timeout_sec: int
    print_timeout: str
    max_restarts: int
    yolo: bool
    allow_dirty: bool
    prepare_slots: bool
    guardrail_poll_sec: float
    forbidden_status_globs: tuple[str, ...]
    runs_layout: str = "date"
    auto_archive_days: int | None = None
    auto_archive_limit: int = 200
    backend: str = CODEX_BACKEND
    codex_model: str = "gpt-5"
    codex_reasoning_effort: str = "low"
    codex_sandbox_mode: str = "workspace-write"
    codex_disabled_mcp_servers: tuple[str, ...] = ()
    codex_forbidden_tool_markers: tuple[str, ...] = ()
    codex_no_progress_timeout_sec: int = 240
    codex_quality_tier: str = "deep"
    codex_mechanical_model: str = "gpt-5.6-luna"
    codex_mechanical_reasoning_effort: str = "low"
    codex_balanced_model: str = "gpt-5.6-terra"
    codex_balanced_reasoning_effort: str = "medium"
    codex_deep_model: str = "gpt-5.6-terra"
    codex_deep_reasoning_effort: str = "medium"
    codex_mechanical_tool_call_budget: int = 45
    codex_balanced_tool_call_budget: int = 80
    codex_deep_tool_call_budget: int = 120
    codex_global_quota_database: Path | None = None
    codex_global_max_concurrent_jobs: int = 2
    codex_five_hour_soft_limit_percent: float = 75.0
    codex_quota_poll_sec: float = 30.0
    codex_sessions_root: Path | None = None
    auto_switch_agy_on_quota: bool = False
    auto_switch_agy_strategy: str = "best"
    auto_switch_agy_electron_command: tuple[str, ...] = (
        "cmd",
        "/c",
        "npx",
        "--no-install",
        "electron",
    )
    shared_ide_sdk_name: str | None = None
    shared_ide_sdk_type: str = "Python SDK"


@dataclass(frozen=True)
class ControlConfig:
    config_path: Path
    project_root: Path
    coordination_root: Path
    runs_root: Path
    database_path: Path
    worktree_root: Path
    worktree_base: Path
    slot_root: Path
    agy_command: str
    codex_command: str
    defaults: ControlDefaults
    routes: Mapping[str, RouteConfig]
    slots: Mapping[str, SlotConfig]
    slot_prepare: tuple[SlotPrepareCommand, ...]


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "workspaces.toml"


def load_config(path: str | os.PathLike[str] | None = None) -> ControlConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    config_path = config_path.resolve(strict=False)
    if not config_path.exists():
        example_path = config_path.with_name("workspaces.example.toml")
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Copy {example_path} to {config_path} and edit it, or pass --config."
        )
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    project_root = config_path.parent.parent.resolve(strict=False)
    control = _table(raw, "control")
    defaults_raw = control.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ValueError("[control.defaults] must be a table")

    defaults = ControlDefaults(
        timeout_sec=int(defaults_raw.get("timeout_sec", 3600)),
        idle_timeout_sec=int(defaults_raw.get("idle_timeout_sec", 900)),
        print_timeout=_string_value(defaults_raw.get("print_timeout", "60m")),
        max_restarts=int(defaults_raw.get("max_restarts", 0)),
        yolo=bool(defaults_raw.get("yolo", False)),
        allow_dirty=bool(defaults_raw.get("allow_dirty", False)),
        prepare_slots=bool(defaults_raw.get("prepare_slots", True)),
        shared_ide_sdk_name=_optional_string_value(defaults_raw.get("shared_ide_sdk_name")),
        shared_ide_sdk_type=_string_value(defaults_raw.get("shared_ide_sdk_type", "Python SDK")),
        guardrail_poll_sec=float(defaults_raw.get("guardrail_poll_sec", 2.0)),
        forbidden_status_globs=_string_tuple(
            defaults_raw.get(
                "forbidden_status_globs",
                [
                    "uv.lock",
                    "poetry.lock",
                    "package-lock.json",
                    "pnpm-lock.yaml",
                    "yarn.lock",
                    "bun.lock",
                    "bun.lockb",
                    ".venv/**",
                ],
            )
        ),
        runs_layout=_runs_layout_value(defaults_raw.get("runs_layout", "date")),
        auto_archive_days=_optional_non_negative_int(defaults_raw.get("auto_archive_days")),
        auto_archive_limit=_positive_int(
            defaults_raw.get("auto_archive_limit", 200), "auto_archive_limit"
        ),
        backend=_backend_value(defaults_raw.get("backend", CODEX_BACKEND)),
        codex_model=_string_value(defaults_raw.get("codex_model", "gpt-5")),
        codex_reasoning_effort=_string_value(defaults_raw.get("codex_reasoning_effort", "low")),
        codex_sandbox_mode=_codex_sandbox_mode_value(
            defaults_raw.get("codex_sandbox_mode", "workspace-write")
        ),
        codex_disabled_mcp_servers=_string_tuple(
            defaults_raw.get("codex_disabled_mcp_servers", [])
        ),
        codex_forbidden_tool_markers=_string_tuple(
            defaults_raw.get("codex_forbidden_tool_markers", [])
        ),
        codex_no_progress_timeout_sec=_non_negative_int(
            defaults_raw.get("codex_no_progress_timeout_sec", 240),
            "codex_no_progress_timeout_sec",
        ),
        codex_quality_tier=_quality_tier_value(defaults_raw.get("codex_quality_tier", "deep")),
        codex_mechanical_model=_string_value(
            defaults_raw.get("codex_mechanical_model", "gpt-5.6-luna")
        ),
        codex_mechanical_reasoning_effort=_string_value(
            defaults_raw.get("codex_mechanical_reasoning_effort", "low")
        ),
        codex_balanced_model=_string_value(
            defaults_raw.get("codex_balanced_model", "gpt-5.6-terra")
        ),
        codex_balanced_reasoning_effort=_string_value(
            defaults_raw.get("codex_balanced_reasoning_effort", "medium")
        ),
        codex_deep_model=_string_value(defaults_raw.get("codex_deep_model", "gpt-5.6-terra")),
        codex_deep_reasoning_effort=_string_value(
            defaults_raw.get("codex_deep_reasoning_effort", "medium")
        ),
        codex_mechanical_tool_call_budget=_positive_int(
            defaults_raw.get("codex_mechanical_tool_call_budget", 45),
            "codex_mechanical_tool_call_budget",
        ),
        codex_balanced_tool_call_budget=_positive_int(
            defaults_raw.get("codex_balanced_tool_call_budget", 80),
            "codex_balanced_tool_call_budget",
        ),
        codex_deep_tool_call_budget=_positive_int(
            defaults_raw.get("codex_deep_tool_call_budget", 120),
            "codex_deep_tool_call_budget",
        ),
        codex_global_quota_database=_optional_path(
            defaults_raw,
            "codex_global_quota_database",
            project_root,
        ),
        codex_global_max_concurrent_jobs=_positive_int(
            defaults_raw.get("codex_global_max_concurrent_jobs", 2),
            "codex_global_max_concurrent_jobs",
        ),
        codex_five_hour_soft_limit_percent=_percent_value(
            defaults_raw.get("codex_five_hour_soft_limit_percent", 75.0),
            "codex_five_hour_soft_limit_percent",
        ),
        codex_quota_poll_sec=_positive_float(
            defaults_raw.get("codex_quota_poll_sec", 30.0),
            "codex_quota_poll_sec",
        ),
        codex_sessions_root=_optional_path(
            defaults_raw,
            "codex_sessions_root",
            project_root,
        ),
        auto_switch_agy_on_quota=bool(defaults_raw.get("auto_switch_agy_on_quota", False)),
        auto_switch_agy_strategy=_string_value(
            defaults_raw.get("auto_switch_agy_strategy", "best")
        ),
        auto_switch_agy_electron_command=_string_tuple(
            defaults_raw.get(
                "auto_switch_agy_electron_command",
                ["cmd", "/c", "npx", "--no-install", "electron"],
            )
        ),
    )

    global_worktree_root = _path(control, "worktree_root", project_root)
    worktree_base: Path | None = _optional_path(control, "worktree_base", project_root)
    slot_root = _path(control, "slot_root", project_root)
    routes_raw = _table(raw, "routes")
    routes: dict[str, RouteConfig] = {}
    for name, value in routes_raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"[routes.{name}] must be a table")
        route_worktree_root = _optional_path(value, "worktree_root", project_root)
        route_path = _path(value, "path", project_root)
        routes[name] = RouteConfig(
            name=name,
            path=route_path,
            required_branch=str(_required(value, "required_branch")),
            worktree_root=route_worktree_root or global_worktree_root,
            worktree_base=_optional_path(value, "worktree_base", project_root) or route_path,
            source_roots=_relative_path_tuple(
                value.get("source_roots", ["backend", "frontend/src"])
            ),
            test_roots=_relative_path_tuple(value.get("test_roots", ["backend/tests"])),
            exclude_dirs=_relative_path_tuple(value.get("exclude_dirs", [])),
            ide_sdk_name=_optional_string_value(value.get("ide_sdk_name")),
            ide_sdk_type=_string_value(value.get("ide_sdk_type", "Python SDK")),
            ide_mcp_server=_optional_string_value(value.get("ide_mcp_server")),
            agy_mcp_server=_optional_string_value(value.get("agy_mcp_server")),
            ide_mcp_project_root=_optional_path(
                value,
                "ide_mcp_project_root",
                project_root,
            ),
            backend=_optional_backend_value(value.get("backend")),
            codex_model=_optional_string_value(value.get("codex_model")),
            codex_reasoning_effort=_optional_string_value(value.get("codex_reasoning_effort")),
            codex_forbidden_tool_markers=_optional_string_tuple(
                value.get("codex_forbidden_tool_markers")
            ),
            monitor_route_root=bool(value.get("monitor_route_root", True)),
        )

    if not routes:
        raise ValueError("At least one route must be configured")

    slots_raw = raw.get("slots", {})
    if not isinstance(slots_raw, dict):
        raise ValueError("[slots] must be a table when configured")
    slots: dict[str, SlotConfig] = {}
    for name, value in slots_raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"[slots.{name}] must be a table")
        route = str(_required(value, "route"))
        if route not in routes:
            raise ValueError(f"[slots.{name}] references unknown route: {route}")
        slots[name] = SlotConfig(
            name=name,
            route=route,
            path=_path(value, "path", project_root),
        )

    slot_prepare_raw = raw.get("slot_prepare", {})
    if not isinstance(slot_prepare_raw, dict):
        raise ValueError("[slot_prepare] must be a table when configured")
    slot_prepare: list[SlotPrepareCommand] = []
    for name, value in slot_prepare_raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"[slot_prepare.{name}] must be a table")
        command = value.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError(f"[slot_prepare.{name}.command] must be a non-empty array")
        timeout_sec = int(value.get("timeout_sec", 1200))
        slot_prepare.append(
            SlotPrepareCommand(
                name=str(name),
                working_dir=Path(_string_value(value.get("working_dir", "."))),
                marker=(
                    Path(_string_value(value["marker"]))
                    if value.get("marker") is not None
                    else None
                ),
                command=tuple(_string_value(part) for part in command),
                timeout_sec=timeout_sec,
                routes=_string_tuple(value.get("routes", [])),
            )
        )

    if worktree_base is None:
        first_route = next(iter(routes.values()))
        worktree_base = Path(cast(Path, first_route.path))
    if worktree_base is None:
        raise ValueError("control.worktree_base could not be inferred")
    resolved_worktree_base = Path(worktree_base)

    return ControlConfig(
        config_path=config_path,
        project_root=project_root,
        coordination_root=_path(control, "coordination_root", project_root),
        runs_root=_path(control, "runs_root", project_root),
        database_path=_path(control, "database", project_root),
        worktree_root=global_worktree_root,
        worktree_base=resolved_worktree_base,
        slot_root=slot_root,
        agy_command=str(control.get("agy_command", "agy")),
        codex_command=str(control.get("codex_command", "codex")),
        defaults=defaults,
        routes=MappingProxyType(routes),
        slots=MappingProxyType(slots),
        slot_prepare=tuple(slot_prepare),
    )


def _table(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a table")
    return value


def _required(raw: Mapping[str, Any], key: str) -> Any:
    if key not in raw:
        raise ValueError(f"Missing required config key: {key}")
    return raw[key]


def _path(raw: Mapping[str, Any], key: str, base: Path) -> Path:
    value = _required(raw, key)
    return _coerce_path(value, base)


def _optional_path(raw: Mapping[str, Any], key: str, base: Path) -> Path | None:
    if key not in raw:
        return None
    return _coerce_path(raw[key], base)


def _coerce_path(value: Any, base: Path) -> Path:
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    raise ValueError(f"Expected a scalar string-compatible value, got {type(value).__name__}")


def _optional_string_value(value: Any) -> str | None:
    if value is None:
        return None
    text = _string_value(value).strip()
    return text or None


def _backend_value(value: Any) -> str:
    backend = normalize_backend(_string_value(value).strip())
    if backend not in SUPPORTED_BACKENDS:
        allowed = ", ".join(SUPPORTED_BACKENDS)
        raise ValueError(f"Unsupported backend {backend!r}. Expected one of: {allowed}")
    return backend


def _optional_backend_value(value: Any) -> str | None:
    if value is None:
        return None
    return _backend_value(value)


def _runs_layout_value(value: Any) -> str:
    layout = _string_value(value).strip()
    if layout not in {"date", "flat"}:
        raise ValueError("control.defaults.runs_layout must be either 'date' or 'flat'")
    return layout


def _codex_sandbox_mode_value(value: Any) -> str:
    mode = _string_value(value).strip()
    allowed = {"read-only", "workspace-write", "danger-full-access"}
    if mode not in allowed:
        expected = ", ".join(sorted(allowed))
        raise ValueError(f"control.defaults.codex_sandbox_mode must be one of: {expected}")
    return mode


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError("control.defaults.auto_archive_days must be non-negative")
    return parsed


def _non_negative_int(value: Any, key: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"control.defaults.{key} must be non-negative")
    return parsed


def _positive_int(value: Any, key: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"control.defaults.{key} must be positive")
    return parsed


def _positive_float(value: Any, key: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"control.defaults.{key} must be positive")
    return parsed


def _percent_value(value: Any, key: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= 100:
        raise ValueError(f"control.defaults.{key} must be in (0, 100]")
    return parsed


def _quality_tier_value(value: Any) -> str:
    tier = _string_value(value).strip().lower()
    if tier not in {"mechanical", "balanced", "deep"}:
        raise ValueError(
            "control.defaults.codex_quality_tier must be mechanical, balanced, or deep"
        )
    return tier


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a TOML array of strings")
    return tuple(_string_value(item) for item in value if _string_value(item).strip())


def _optional_string_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _string_tuple(value)


def _relative_path_tuple(value: Any) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a TOML array of relative paths")
    paths: list[Path] = []
    for item in value:
        text = _string_value(item).strip()
        if not text:
            continue
        path = Path(text)
        if path.is_absolute():
            raise ValueError(f"Expected a relative path, got absolute path: {text}")
        paths.append(path)
    return tuple(paths)
