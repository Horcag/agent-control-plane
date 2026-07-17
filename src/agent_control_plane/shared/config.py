from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, cast

from agent_control_plane.shared.agent_backends import (
    CODEX_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)


@dataclass(frozen=True)
class NativeQualityGateConfig:
    name: str
    command: tuple[str, ...]
    working_dir: Path = Path(".")
    timeout_sec: int = 300
    include_globs: tuple[str, ...] = ()
    run_on: str = "both"

    def __post_init__(self) -> None:
        if self.run_on not in {"worker", "controller", "both"}:
            raise ValueError("run_on must be worker, controller, or both")


@dataclass(frozen=True)
class CodexTokenRateConfig:
    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class CodexModelMetadataConfig:
    model: str
    quota_domain: str | None
    capacity_units: tuple[tuple[str, int], ...]
    credit_rate: CodexTokenRateConfig | None
    api_usd_rate: CodexTokenRateConfig | None
    rate_card_version: str | None
    rate_card_source: str | None


@dataclass(frozen=True)
class CodexQuotaDomainConfig:
    name: str
    max_concurrent_jobs: int
    max_burst_jobs: int
    soft_limit_percent: float


@dataclass(frozen=True)
class CodexModelCatalogConfig:
    cache_path: Path
    max_cache_age_sec: float
    models: tuple[CodexModelMetadataConfig, ...]
    quota_domains: tuple[CodexQuotaDomainConfig, ...]


@dataclass(frozen=True)
class CodexRoutingCandidateConfig:
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class CodexAdaptiveRoutingConfig:
    minimum_samples_per_candidate: int
    history_window: int
    quality_floor: float
    prior_quality: float
    prior_weight: float
    allow_missing_price: bool = False


@dataclass(frozen=True)
class CodexRoutingPolicyConfig:
    name: str
    task_class: str
    tool_call_budget: int
    candidates: tuple[CodexRoutingCandidateConfig, ...]
    adaptive: CodexAdaptiveRoutingConfig | None = None


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
    agy_model: str | None = None
    ide_mcp_project_root: Path | None = None
    backend: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    codex_forbidden_tool_markers: tuple[str, ...] | None = None
    workspace_access: str | None = None
    native_quality_policy: str | None = None
    native_quality_max_parallel: int = 1
    native_quality_gates: tuple[NativeQualityGateConfig, ...] = ()
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
    agy_model: str | None = None
    codex_model: str = "default"
    codex_reasoning_effort: str = "low"
    codex_sandbox_mode: str = "workspace-write"
    workspace_access: str = "ide_mcp"
    native_quality_policy: str = "worker"
    terminal_slot_policy: str = "preserve"
    codex_disabled_mcp_servers: tuple[str, ...] = ()
    codex_forbidden_tool_markers: tuple[str, ...] = ()
    codex_no_progress_timeout_sec: int = 240
    codex_quality_tier: str = "deep"
    codex_mechanical_model: str = "default"
    codex_mechanical_reasoning_effort: str = "low"
    codex_balanced_model: str = "default"
    codex_balanced_reasoning_effort: str = "medium"
    codex_deep_model: str = "default"
    codex_deep_reasoning_effort: str = "medium"
    codex_mechanical_tool_call_budget: int = 45
    codex_balanced_tool_call_budget: int = 80
    codex_deep_tool_call_budget: int = 120
    codex_global_quota_database: Path | None = None
    codex_global_max_concurrent_jobs: int = 2
    codex_global_max_burst_jobs: int = 8
    codex_spark_max_concurrent_jobs: int = 8
    codex_five_hour_soft_limit_percent: float = 75.0
    codex_spark_soft_limit_percent: float = 100.0
    codex_quota_poll_sec: float = 30.0
    codex_spark_models: tuple[str, ...] = ()
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


def _default_model_catalog_config() -> CodexModelCatalogConfig:
    return CodexModelCatalogConfig(
        cache_path=Path.home() / ".codex" / "models_cache.json",
        max_cache_age_sec=86_400.0,
        models=(),
        quota_domains=(
            CodexQuotaDomainConfig(
                name="primary",
                max_concurrent_jobs=2,
                max_burst_jobs=8,
                soft_limit_percent=75.0,
            ),
        ),
    )


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
    model_catalog: CodexModelCatalogConfig = field(default_factory=_default_model_catalog_config)
    routing_policies: tuple[CodexRoutingPolicyConfig, ...] = ()


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "workspaces.toml"


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    config_contents: bytes | None = None,
) -> ControlConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    config_path = config_path.resolve(strict=False)
    if config_contents is None and not config_path.exists():
        example_path = config_path.with_name("workspaces.example.toml")
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Copy {example_path} to {config_path} and edit it, or pass --config."
        )
    if config_contents is None:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    else:
        raw = tomllib.loads(config_contents.decode("utf-8"))

    project_root = config_path.parent.parent.resolve(strict=False)
    control = _table(raw, "control")
    defaults_raw = control.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        raise ValueError("[control.defaults] must be a table")
    codex_global_max_concurrent_jobs = _positive_int(
        defaults_raw.get("codex_global_max_concurrent_jobs", 2),
        "codex_global_max_concurrent_jobs",
    )
    codex_global_max_burst_jobs = _positive_int(
        defaults_raw.get(
            "codex_global_max_burst_jobs",
            codex_global_max_concurrent_jobs * 4,
        ),
        "codex_global_max_burst_jobs",
    )
    codex_spark_max_concurrent_jobs = _positive_int(
        defaults_raw.get("codex_spark_max_concurrent_jobs", 8),
        "codex_spark_max_concurrent_jobs",
    )
    if codex_global_max_burst_jobs < codex_global_max_concurrent_jobs:
        raise ValueError(
            "codex_global_max_burst_jobs must be at least codex_global_max_concurrent_jobs"
        )

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
        agy_model=_optional_string_value(defaults_raw.get("agy_model")),
        codex_model=_string_value(defaults_raw.get("codex_model", "default")),
        codex_reasoning_effort=_string_value(defaults_raw.get("codex_reasoning_effort", "low")),
        codex_sandbox_mode=_codex_sandbox_mode_value(
            defaults_raw.get("codex_sandbox_mode", "workspace-write")
        ),
        workspace_access=_workspace_access_value(defaults_raw.get("workspace_access", "ide_mcp")),
        native_quality_policy=_native_quality_policy_value(
            defaults_raw.get("native_quality_policy", "worker")
        ),
        terminal_slot_policy=_terminal_slot_policy_value(
            defaults_raw.get("terminal_slot_policy", "preserve")
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
        codex_quality_tier=_routing_policy_name_value(
            defaults_raw.get("codex_quality_tier", "deep")
        ),
        codex_mechanical_model=_string_value(defaults_raw.get("codex_mechanical_model", "default")),
        codex_mechanical_reasoning_effort=_string_value(
            defaults_raw.get("codex_mechanical_reasoning_effort", "low")
        ),
        codex_balanced_model=_string_value(defaults_raw.get("codex_balanced_model", "default")),
        codex_balanced_reasoning_effort=_string_value(
            defaults_raw.get("codex_balanced_reasoning_effort", "medium")
        ),
        codex_deep_model=_string_value(defaults_raw.get("codex_deep_model", "default")),
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
        codex_global_max_concurrent_jobs=codex_global_max_concurrent_jobs,
        codex_global_max_burst_jobs=codex_global_max_burst_jobs,
        codex_spark_max_concurrent_jobs=codex_spark_max_concurrent_jobs,
        codex_five_hour_soft_limit_percent=_percent_value(
            defaults_raw.get("codex_five_hour_soft_limit_percent", 75.0),
            "codex_five_hour_soft_limit_percent",
        ),
        codex_spark_soft_limit_percent=_percent_value(
            defaults_raw.get("codex_spark_soft_limit_percent", 100.0),
            "codex_spark_soft_limit_percent",
        ),
        codex_spark_models=_string_tuple(defaults_raw.get("codex_spark_models", [])),
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
    model_catalog = _model_catalog_config(
        control.get("model_catalog", {}),
        project_root=project_root,
        legacy_primary=CodexQuotaDomainConfig(
            name="primary",
            max_concurrent_jobs=codex_global_max_concurrent_jobs,
            max_burst_jobs=codex_global_max_burst_jobs,
            soft_limit_percent=defaults.codex_five_hour_soft_limit_percent,
        ),
        legacy_secondary=CodexQuotaDomainConfig(
            name="spark",
            max_concurrent_jobs=codex_spark_max_concurrent_jobs,
            max_burst_jobs=codex_spark_max_concurrent_jobs * 4,
            soft_limit_percent=defaults.codex_spark_soft_limit_percent,
        ),
        legacy_secondary_models=defaults.codex_spark_models,
    )
    routing_policies = _routing_policy_configs(control.get("model_routing", {}))

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
        native_quality_gates = _native_quality_gates(name, value.get("native_quality_gates", []))
        native_quality_policy = _optional_native_quality_policy_value(
            value.get("native_quality_policy")
        )
        native_quality_max_parallel = _native_quality_max_parallel_value(
            value.get("native_quality_max_parallel", 1)
        )
        effective_native_quality_policy = native_quality_policy or defaults.native_quality_policy
        if effective_native_quality_policy == "controller" and not native_quality_gates:
            raise ValueError(
                f"routes.{name} native_quality_policy='controller' requires at least one "
                "native_quality_gate"
            )
        if effective_native_quality_policy == "controller" and not any(
            gate.run_on in {"controller", "both"} for gate in native_quality_gates
        ):
            raise ValueError(
                f"routes.{name} native_quality_policy='controller' requires at least one "
                "controller quality gate"
            )
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
            agy_model=_optional_string_value(value.get("agy_model")),
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
            workspace_access=_optional_workspace_access_value(value.get("workspace_access")),
            native_quality_policy=native_quality_policy,
            native_quality_max_parallel=native_quality_max_parallel,
            native_quality_gates=native_quality_gates,
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
        model_catalog=model_catalog,
        routing_policies=routing_policies,
        routes=MappingProxyType(routes),
        slots=MappingProxyType(slots),
        slot_prepare=tuple(slot_prepare),
    )


def _table(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a table")
    return value


def _model_catalog_config(
    raw: Any,
    *,
    project_root: Path,
    legacy_primary: CodexQuotaDomainConfig,
    legacy_secondary: CodexQuotaDomainConfig,
    legacy_secondary_models: tuple[str, ...],
) -> CodexModelCatalogConfig:
    if not isinstance(raw, dict):
        raise ValueError("[control.model_catalog] must be a table")
    cache_path = _optional_path(raw, "cache_path", project_root)
    if cache_path is None:
        cache_path = Path.home() / ".codex" / "models_cache.json"
    max_cache_age_sec = _positive_float(
        raw.get("max_cache_age_sec", 86_400.0),
        "control.model_catalog.max_cache_age_sec",
    )
    configured_domains = raw.get("quota_domains", [])
    if not isinstance(configured_domains, list):
        raise ValueError("control.model_catalog.quota_domains must be an array of tables")
    quota_domains = (
        tuple(_quota_domain_config(value) for value in configured_domains)
        if configured_domains
        else (legacy_primary, legacy_secondary)
    )
    domain_names = [domain.name for domain in quota_domains]
    if len(domain_names) != len(set(domain_names)):
        raise ValueError("control.model_catalog.quota_domains contains duplicate names")
    if "primary" not in domain_names:
        raise ValueError("control.model_catalog.quota_domains must include primary")
    configured_models = raw.get("models", [])
    if not isinstance(configured_models, list):
        raise ValueError("control.model_catalog.models must be an array of tables")
    models = [_model_metadata_config(value) for value in configured_models]
    configured_model_ids = {model.model.lower() for model in models}
    for legacy_model in legacy_secondary_models:
        if legacy_model.lower() not in configured_model_ids:
            models.append(
                CodexModelMetadataConfig(
                    model=legacy_model,
                    quota_domain=legacy_secondary.name,
                    capacity_units=(),
                    credit_rate=None,
                    api_usd_rate=None,
                    rate_card_version=None,
                    rate_card_source=None,
                )
            )
    model_ids = [model.model.lower() for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise ValueError("control.model_catalog.models contains duplicate model IDs")
    configured_domains_by_name = set(domain_names)
    for model in models:
        if model.quota_domain is not None and model.quota_domain not in configured_domains_by_name:
            raise ValueError(
                f"Model catalog metadata references unknown quota domain: {model.quota_domain}"
            )
    return CodexModelCatalogConfig(
        cache_path=cache_path,
        max_cache_age_sec=max_cache_age_sec,
        models=tuple(models),
        quota_domains=quota_domains,
    )


def _routing_policy_configs(raw: Any) -> tuple[CodexRoutingPolicyConfig, ...]:
    if not isinstance(raw, Mapping):
        raise ValueError("[control.model_routing] must be a table")
    policies_raw = raw.get("policies", [])
    if not isinstance(policies_raw, list):
        raise ValueError("control.model_routing.policies must be an array of tables")
    policies = tuple(
        _routing_policy_config(item, index=index)
        for index, item in enumerate(policies_raw, start=1)
    )
    names = [policy.name.lower() for policy in policies]
    if len(names) != len(set(names)):
        raise ValueError("control.model_routing.policies contains duplicate names")
    return policies


def _routing_policy_config(raw: Any, *, index: int) -> CodexRoutingPolicyConfig:
    location = f"control.model_routing.policies[{index}]"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{location} must be a table")
    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise ValueError(f"{location}.candidates must be a non-empty array of tables")
    candidates = tuple(
        _routing_candidate_config(candidate, location=f"{location}.candidates[{candidate_index}]")
        for candidate_index, candidate in enumerate(candidates_raw, start=1)
    )
    candidate_keys = [
        (candidate.model.lower(), candidate.reasoning_effort.lower()) for candidate in candidates
    ]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise ValueError(f"{location}.candidates contains duplicate model and effort pairs")
    adaptive = _adaptive_routing_config(raw.get("adaptive"), location=location)
    if adaptive is not None:
        minimum_history_window = adaptive.minimum_samples_per_candidate * len(candidates)
        if adaptive.history_window < minimum_history_window:
            raise ValueError(
                f"{location}.adaptive.history_window must be at least "
                "minimum_samples_per_candidate * len(candidates) "
                f"({adaptive.minimum_samples_per_candidate} * {len(candidates)} "
                f"= {minimum_history_window}); got {adaptive.history_window}"
            )
    return CodexRoutingPolicyConfig(
        name=_configured_text(raw, "name", location),
        task_class=_configured_text(raw, "task_class", location),
        tool_call_budget=_positive_int(
            _required(raw, "tool_call_budget"),
            f"{location}.tool_call_budget",
        ),
        candidates=candidates,
        adaptive=adaptive,
    )


def _routing_candidate_config(raw: Any, *, location: str) -> CodexRoutingCandidateConfig:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{location} must be a table")
    return CodexRoutingCandidateConfig(
        model=_configured_text(raw, "model", location),
        reasoning_effort=_configured_text(raw, "reasoning_effort", location).lower(),
    )


def _adaptive_routing_config(raw: Any, *, location: str) -> CodexAdaptiveRoutingConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{location}.adaptive must be a table")
    return CodexAdaptiveRoutingConfig(
        minimum_samples_per_candidate=_minimum_two_int(
            _required(raw, "minimum_samples_per_candidate"),
            f"{location}.adaptive.minimum_samples_per_candidate",
        ),
        history_window=_positive_int(
            _required(raw, "history_window"),
            f"{location}.adaptive.history_window",
        ),
        quality_floor=_unit_interval_value(
            _required(raw, "quality_floor"),
            f"{location}.adaptive.quality_floor",
        ),
        prior_quality=_unit_interval_value(
            _required(raw, "prior_quality"),
            f"{location}.adaptive.prior_quality",
        ),
        prior_weight=_positive_float(
            _required(raw, "prior_weight"),
            f"{location}.adaptive.prior_weight",
        ),
        allow_missing_price=bool(raw.get("allow_missing_price", False)),
    )


def _configured_text(raw: Mapping[str, Any], key: str, location: str) -> str:
    text = _optional_string_value(_required(raw, key))
    if text is None:
        raise ValueError(f"{location}.{key} must not be empty")
    return text


def _quota_domain_config(raw: Any) -> CodexQuotaDomainConfig:
    if not isinstance(raw, dict):
        raise ValueError("Each model catalog quota domain must be a table")
    name = _string_value(_required(raw, "name")).lower()
    max_concurrent_jobs = _positive_int(
        _required(raw, "max_concurrent_jobs"),
        f"control.model_catalog.quota_domains.{name}.max_concurrent_jobs",
    )
    max_burst_jobs = _positive_int(
        raw.get("max_burst_jobs", max_concurrent_jobs * 4),
        f"control.model_catalog.quota_domains.{name}.max_burst_jobs",
    )
    if max_burst_jobs < max_concurrent_jobs:
        raise ValueError(
            f"Model catalog quota domain {name} max_burst_jobs must be at least max_concurrent_jobs"
        )
    return CodexQuotaDomainConfig(
        name=name,
        max_concurrent_jobs=max_concurrent_jobs,
        max_burst_jobs=max_burst_jobs,
        soft_limit_percent=_percent_value(
            raw.get("soft_limit_percent", 100.0),
            f"control.model_catalog.quota_domains.{name}.soft_limit_percent",
        ),
    )


def _model_metadata_config(raw: Any) -> CodexModelMetadataConfig:
    if not isinstance(raw, dict):
        raise ValueError("Each model catalog metadata entry must be a table")
    model = _string_value(_required(raw, "model"))
    quota_domain = _optional_string_value(raw.get("quota_domain"))
    if quota_domain is not None:
        quota_domain = quota_domain.lower()
    capacity_raw = raw.get("capacity_units", {})
    if not isinstance(capacity_raw, dict):
        raise ValueError(f"Model catalog capacity_units must be a table: {model}")
    capacity_units = tuple(
        sorted(
            (
                _string_value(effort).lower(),
                _positive_int(units, f"Model catalog capacity_units.{effort}"),
            )
            for effort, units in capacity_raw.items()
        )
    )
    if len({effort for effort, _ in capacity_units}) != len(capacity_units):
        raise ValueError(f"Model catalog capacity_units contains duplicate efforts: {model}")
    credit_rate = _catalog_rate(raw, model, "credit")
    api_usd_rate = _catalog_rate(raw, model, "api_usd")
    rate_card_version = _optional_string_value(raw.get("rate_card_version"))
    rate_card_source = _optional_string_value(raw.get("rate_card_source"))
    if (credit_rate is not None or api_usd_rate is not None) and (
        rate_card_version is None or rate_card_source is None
    ):
        raise ValueError(f"Model catalog rate metadata needs version and source: {model}")
    return CodexModelMetadataConfig(
        model=model,
        quota_domain=quota_domain,
        capacity_units=capacity_units,
        credit_rate=credit_rate,
        api_usd_rate=api_usd_rate,
        rate_card_version=rate_card_version,
        rate_card_source=rate_card_source,
    )


def _catalog_rate(
    raw: Mapping[str, Any],
    model: str,
    prefix: str,
) -> CodexTokenRateConfig | None:
    rate = raw.get(f"{prefix}_rate")
    if rate is None:
        return None
    if not isinstance(rate, Mapping):
        raise ValueError(f"Model catalog {prefix} rate must be a table: {model}")
    values = [rate.get(part) for part in ("input", "cached_input", "output")]
    if any(value is None for value in values):
        raise ValueError(f"Model catalog {prefix} rate is incomplete: {model}")
    rate_values = [float(cast(int | float | str, value)) for value in values]
    if any(value < 0 for value in rate_values):
        raise ValueError(f"Model catalog {prefix} rate must not be negative: {model}")
    return CodexTokenRateConfig(*rate_values)


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


def _workspace_access_value(value: Any) -> str:
    access = _string_value(value).strip()
    if access not in {"ide_mcp", "native"}:
        raise ValueError("workspace_access must be either 'ide_mcp' or 'native'")
    return access


def _optional_workspace_access_value(value: Any) -> str | None:
    if value is None:
        return None
    return _workspace_access_value(value)


def _native_quality_policy_value(value: Any) -> str:
    policy = _string_value(value).strip().lower()
    if policy not in {"off", "worker", "controller"}:
        raise ValueError("native_quality_policy must be off, worker, or controller")
    return policy


def _optional_native_quality_policy_value(value: Any) -> str | None:
    if value is None:
        return None
    return _native_quality_policy_value(value)


def _native_quality_max_parallel_value(value: Any) -> int:
    max_parallel = int(value)
    if not 1 <= max_parallel <= 4:
        raise ValueError("native_quality_max_parallel must be between 1 and 4")
    return max_parallel


def _native_quality_gates(
    route_name: str,
    value: Any,
) -> tuple[NativeQualityGateConfig, ...]:
    if not isinstance(value, list):
        raise ValueError(f"routes.{route_name}.native_quality_gates must be an array of tables")
    gates: list[NativeQualityGateConfig] = []
    seen_names: set[str] = set()
    for index, item in enumerate(value):
        label = f"routes.{route_name}.native_quality_gates[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be a table")
        name = _string_value(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"{label}.name must be non-empty")
        if name in seen_names:
            raise ValueError(f"routes.{route_name} has duplicate native quality gate: {name}")
        seen_names.add(name)
        command_raw = item.get("command")
        if not isinstance(command_raw, list) or not command_raw:
            raise ValueError(f"{label}.command must be a non-empty array")
        command = tuple(_string_value(part).strip() for part in command_raw)
        if any(not part for part in command):
            raise ValueError(f"{label}.command entries must be non-empty")
        if not _native_quality_command_is_read_only(command):
            raise ValueError(f"{label}.command must be a read-only quality check")
        _validate_native_quality_placeholders(command, label)
        working_dir_text = _string_value(item.get("working_dir", ".")).strip() or "."
        working_dir = Path(working_dir_text)
        if (
            PurePosixPath(working_dir_text).is_absolute()
            or PureWindowsPath(working_dir_text).is_absolute()
            or ".." in PurePosixPath(working_dir_text.replace("\\", "/")).parts
        ):
            raise ValueError(f"{label}.working_dir must stay inside the task workspace")
        include_globs = _string_tuple(item.get("include_globs", []))
        if any(
            PurePosixPath(pattern.replace("\\", "/")).is_absolute()
            or ".." in PurePosixPath(pattern.replace("\\", "/")).parts
            for pattern in include_globs
        ):
            raise ValueError(f"{label}.include_globs must contain relative patterns")
        gates.append(
            NativeQualityGateConfig(
                name=name,
                command=command,
                working_dir=working_dir,
                timeout_sec=_positive_gate_timeout(item.get("timeout_sec", 300), label),
                include_globs=tuple(pattern.replace("\\", "/") for pattern in include_globs),
                run_on=_native_quality_run_on_value(item.get("run_on", "both")),
            )
        )
    return tuple(gates)


def _native_quality_run_on_value(value: Any) -> str:
    run_on = _string_value(value).strip().lower()
    if run_on not in {"worker", "controller", "both"}:
        raise ValueError("run_on must be worker, controller, or both")
    return run_on


def _validate_native_quality_placeholders(command: tuple[str, ...], label: str) -> None:
    placeholder = "{changed_python_files}"
    placeholder_count = command.count(placeholder)
    if placeholder_count > 1:
        raise ValueError(f"{label}.command may contain {placeholder} only once")
    for argument in command:
        if argument.startswith("{") and argument.endswith("}") and argument != placeholder:
            raise ValueError(f"{label}.command contains an unsupported command placeholder")


def _positive_gate_timeout(value: Any, label: str) -> int:
    timeout = int(value)
    if timeout <= 0:
        raise ValueError(f"{label}.timeout_sec must be positive")
    return timeout


def _native_quality_command_is_read_only(command: tuple[str, ...]) -> bool:
    executable = command[0].replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        executable = executable.removesuffix(suffix)
    arguments = tuple(part.lower() for part in command[1:])
    if executable.startswith("python") and len(arguments) >= 2 and arguments[0] == "-m":
        executable = arguments[1]
        arguments = arguments[2:]
    if executable in {"cmd", "powershell", "pwsh", "bash", "sh", "zsh"}:
        return False
    if executable in {"npx", "bunx", "pnpx", "uvx", "pipx"}:
        return False
    if any(flag in arguments for flag in {"--fix", "--unsafe-fixes", "--write"}):
        return False
    if executable == "ruff" and arguments[:1] == ("format",) and "--check" not in arguments:
        return False
    if executable in {"black", "prettier"} and "--check" not in arguments:
        return False
    if (
        executable == "uv"
        and arguments[:1]
        and arguments[0]
        in {
            "sync",
            "add",
            "remove",
            "lock",
            "pip",
            "run",
        }
    ):
        return False
    if executable in {"pip", "pip3"} and arguments[:1] in {("install",), ("uninstall",)}:
        return False
    if (
        executable in {"npm", "bun", "pnpm", "yarn", "poetry", "cargo"}
        and arguments[:1]
        and arguments[0] in {"install", "i", "ci", "add", "remove", "exec", "dlx", "x"}
    ):
        return False
    if executable == "go" and arguments[:1] in {("get",), ("install",)}:
        return False
    return not (
        executable == "git"
        and arguments[:1]
        and arguments[0]
        in {
            "add",
            "am",
            "apply",
            "checkout",
            "cherry-pick",
            "clean",
            "commit",
            "merge",
            "pull",
            "push",
            "rebase",
            "reset",
            "restore",
            "revert",
            "switch",
        }
    )


def _terminal_slot_policy_value(value: Any) -> str:
    policy = _string_value(value).strip()
    if policy not in {"preserve", "checkpoint"}:
        raise ValueError("terminal_slot_policy must be either 'preserve' or 'checkpoint'")
    return policy


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


def _unit_interval_value(value: Any, key: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{key} must be between 0 and 1")
    return parsed


def _routing_policy_name_value(value: Any) -> str:
    policy_name = _string_value(value).strip()
    if not policy_name:
        raise ValueError("control.defaults.codex_quality_tier must not be empty")
    return policy_name


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a TOML array of strings")
    return tuple(_string_value(item) for item in value if _string_value(item).strip())


def _minimum_two_int(value: Any, key: str) -> int:
    parsed = int(value)
    if parsed < 2:
        raise ValueError(
            f"{key} must be at least two; at least two comparable samples are required"
        )
    return parsed


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
