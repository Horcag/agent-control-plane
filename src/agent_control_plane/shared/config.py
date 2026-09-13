from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, cast

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


from agent_control_plane.shared.agent_backends import (
    CODEX_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)
from agent_control_plane.shared.git_tools import GitError, run_git
from agent_control_plane.shared.path_rules import glob_matches_whole_tree


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
    premium: bool = False


@dataclass(frozen=True)
class CodexModelRuleConfig:
    match: str
    quota_domain: str | None = None
    capacity_units: tuple[tuple[str, int], ...] = ()
    credit_rate: CodexTokenRateConfig | None = None
    api_usd_rate: CodexTokenRateConfig | None = None
    rate_card_version: str | None = None
    rate_card_source: str | None = None
    premium: bool = False


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
    model_rules: tuple[CodexModelRuleConfig, ...] = ()
    newness_window_days: float = 7.0
    unknown_model_policy: str = "warn"


@dataclass(frozen=True)
class ClaudeModelInventoryConfig:
    """Operator override or extension of the builtin Claude model inventory."""

    model: str
    visible: bool = True
    priority: int | None = None
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeModelCatalogConfig:
    """Claude Code has no CLI-owned cache file, so the inventory is builtin plus overrides."""

    models: tuple[CodexModelMetadataConfig, ...] = ()
    model_rules: tuple[CodexModelRuleConfig, ...] = ()
    inventory: tuple[ClaudeModelInventoryConfig, ...] = ()
    unknown_model_policy: str = "warn"


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


# Ceiling on uncommitted changed-line growth before the dirty-diff guardrail kills a
# writable worker. Calibration history: 500 killed legitimate Claude implementation
# jobs three times on 2026-07-20/21; 1200 killed a legitimate 1426-line slice on
# 2026-07-22 (DCC-012B0a). The guardrail targets runaway agents trashing a workspace
# (tens of thousands of junk lines), not honest implementation size, so the default
# stays far above real slices. 0 disables the check.
DEFAULT_DIRTY_DIFF_MAX_CHANGED_LINES = 8000


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
    # Where slot worktrees for this route live. None falls back to control.slot_root;
    # set it per route so unrelated projects never share one project's slot directory.
    slot_root: Path | None = None
    ide_sdk_name: str | None = None
    ide_sdk_type: str = "Python SDK"
    ide_mcp_server: str | None = None
    agy_mcp_server: str | None = None
    agy_model: str | None = None
    ide_mcp_project_root: Path | None = None
    backend: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    claude_model: str | None = None
    claude_reasoning_effort: str | None = None
    codex_forbidden_tool_markers: tuple[str, ...] | None = None
    workspace_access: str | None = None
    native_quality_policy: str | None = None
    native_quality_max_parallel: int = 1
    native_quality_gates: tuple[NativeQualityGateConfig, ...] = ()
    monitor_route_root: bool = True
    route_root_ignore_globs: tuple[str, ...] = ()
    dirty_diff_max_changed_lines: int | None = None


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
    native_quality_global_max_parallel: int = 4
    terminal_slot_policy: str = "preserve"
    codex_disabled_mcp_servers: tuple[str, ...] = ()
    codex_forbidden_tool_markers: tuple[str, ...] = ()
    no_progress_timeout_sec: int = 240
    dirty_diff_max_changed_lines: int = DEFAULT_DIRTY_DIFF_MAX_CHANGED_LINES
    tool_timeout_limit: int = 6
    tool_call_budget_grace_sec: int = 120
    invalid_verification_grace_sec: int = 120
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
    claude_model: str = "default"
    claude_reasoning_effort: str = "medium"
    claude_permission_mode: str = "acceptEdits"
    claude_allowed_tools: tuple[str, ...] = (
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "Bash",
    )
    claude_sessions_root: Path | None = None
    claude_max_turns: int = 0
    claude_bare: bool = True
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
        model_rules=(),
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
    claude_command: str = "claude"
    claude_model_catalog: ClaudeModelCatalogConfig = field(default_factory=ClaudeModelCatalogConfig)
    claude_mcp_servers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    claude_config_path: Path | None = None
    # Optional managed proxy launcher. Its presence is intentional policy: an unusable path
    # must block AGY launch rather than quietly selecting the real executable.
    agy_proxy_launcher: Path | None = None
    # Unmanaged is the compatible default: ACP executes the explicit agy_command as supplied.
    # Managed requires an explicit adapter and is the only mode that claims adapter mediation.
    agy_launch_mode: str = "unmanaged"

    def slot_root_for(self, route: str | None) -> Path:
        """Slot directory that owns this route's worktrees.

        Routes may declare their own ``slot_root`` so an unrelated project never
        materializes slots inside another project's slot directory.
        """
        route_config = self.routes.get(route) if route else None
        if route_config is not None and route_config.slot_root is not None:
            return route_config.slot_root
        return self.slot_root


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "workspaces.toml"


_CONFIG_LOCK_TIMEOUT_SEC = 5.0
_CONFIG_LOCK_RETRY_SEC = 0.02


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0)
        lock_file.write(b"\0")
        lock_file.truncate(1)
        lock_file.flush()
        deadline = time.monotonic() + _CONFIG_LOCK_TIMEOUT_SEC
        while True:
            try:
                if sys.platform == "win32":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"timed out after {_CONFIG_LOCK_TIMEOUT_SEC:.1f}s acquiring lock: {lock_path}"
                    ) from exc
                time.sleep(_CONFIG_LOCK_RETRY_SEC)
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def interprocess_config_lock(config_path: Path) -> Iterator[None]:
    canonical_path = os.path.normcase(str(Path(config_path).resolve(strict=False)))
    fingerprint = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    lock_path = (
        Path(tempfile.gettempdir()) / "agent-control-plane" / "config-locks" / f"{fingerprint}.lock"
    )
    with _file_lock(lock_path):
        yield


_interprocess_config_lock = interprocess_config_lock


KNOWN_CONFIGS_ENV_VAR = "ACP_KNOWN_CONFIGS_PATH"


def known_configs_path() -> Path:
    """Where the known-config index lives.

    Honours ``ACP_KNOWN_CONFIGS_PATH`` so a test run - including one that spawns a real
    server subprocess, which an in-process patch cannot reach - can keep its throwaway
    configs out of the operator's index. Config discovery reads this index on every
    resolution, so anything that leaks into it changes which control plane real calls
    talk to.
    """
    override = os.environ.get(KNOWN_CONFIGS_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-control-plane" / "known-configs.json"


def register_known_config(config_path: Path | str) -> Path:
    path_obj = Path(config_path).expanduser().resolve(strict=False)
    canonical = str(path_obj)

    cfg_file = known_configs_path()
    lock_file = cfg_file.with_suffix(".lock")

    with _file_lock(lock_file):
        existing: list[str] = []
        if cfg_file.is_file():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    existing = [str(item) for item in data if isinstance(item, str)]
            except (OSError, ValueError, KeyError):
                existing = []

        # Every resolution loads and parses each surviving entry, so a dead one is not
        # free: it is a stat on every lookup, and a stale-but-present copy can win the
        # match. Entries are cheap to re-earn - using a config registers it again.
        surviving = [item for item in existing if Path(item).is_file()]
        if canonical not in surviving:
            surviving.append(canonical)
        if surviving != existing:
            tmp_file = cfg_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(surviving, indent=2), encoding="utf-8")
            os.replace(tmp_file, cfg_file)

    return path_obj


def resolve_config_for(cwd: Path | str | None = None) -> Path:
    resolved_cwd = (Path(cwd) if cwd else Path.cwd()).expanduser().resolve(strict=False)
    norm_cwd_str = os.path.normcase(str(resolved_cwd))
    norm_cwd = Path(norm_cwd_str)

    # A repository's root config is the canonical implicit choice. Registered copies can
    # describe the same route but must not win merely because their paths sort first.
    repo_root = find_enclosing_git_repo(resolved_cwd)
    canonical_config = repo_root / ".agent-work" / "workspaces.toml"
    if (repo_root / ".git").exists() and canonical_config.is_file():
        return canonical_config
    linked_worktree_config = _linked_worktree_canonical_config(repo_root)
    if linked_worktree_config is not None:
        return linked_worktree_config

    candidate_config_paths: list[Path] = []

    def _add_config_path(p: Path) -> None:
        try:
            resolved_p = p.expanduser().resolve(strict=False)
            if resolved_p.is_file() and resolved_p not in candidate_config_paths:
                candidate_config_paths.append(resolved_p)
        except OSError:
            pass

    # Collect known configs from index
    cfg_file = known_configs_path()
    if cfg_file.is_file():
        try:
            raw_index = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(raw_index, list):
                for p in raw_index:
                    if isinstance(p, str):
                        _add_config_path(Path(p))
        except (OSError, ValueError, KeyError):
            pass

    # Collect nearest config upwards
    curr: Path | None = resolved_cwd
    while curr is not None:
        candidate = curr / ".agent-work" / "workspaces.toml"
        if candidate.is_file():
            _add_config_path(candidate)
            break
        parent = curr.parent
        if parent == curr:
            break
        curr = parent

    # Collect default config
    _add_config_path(default_config_path())

    # Load candidate configs
    loaded_configs: list[tuple[Path, ControlConfig]] = []
    for cfg_path in candidate_config_paths:
        try:
            cfg = load_config(cfg_path)
            loaded_configs.append((cfg_path, cfg))
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
            continue

    def _distance_from_cwd(norm_cfg_str: str) -> int:
        """How unrelated a config's own location is to the working directory.

        Used only to break a tie between configs whose routes match cwd equally well.
        Without it the tie fell to plain alphabetical order, which let a throwaway copy
        in a temp directory outrank the project's own config by drive letter alone.
        Lower is closer.
        """
        shared = 0
        for cfg_part, cwd_part in zip(Path(norm_cfg_str).parts, norm_cwd.parts, strict=False):
            if cfg_part != cwd_part:
                break
            shared += 1
        return -shared

    # 1. Longest matching route path where route path equals cwd or cwd is a subdirectory
    rule1_matches: list[tuple[tuple[int, int], str, Path]] = []
    for cfg_path, cfg in loaded_configs:
        norm_cfg_str = os.path.normcase(str(cfg_path))
        best_score_for_cfg: tuple[int, int] | None = None
        for route in cfg.routes.values():
            try:
                norm_route_str = os.path.normcase(str(route.path.resolve(strict=False)))
            except OSError:
                continue
            norm_route = Path(norm_route_str)

            if norm_cwd == norm_route or norm_route in norm_cwd.parents:
                score = (len(norm_route.parts), len(norm_route_str))
                if best_score_for_cfg is None or score > best_score_for_cfg:
                    best_score_for_cfg = score

        if best_score_for_cfg is not None:
            rule1_matches.append((best_score_for_cfg, norm_cfg_str, cfg_path))

    if rule1_matches:
        best_score = max(m[0] for m in rule1_matches)
        tied_configs = [m for m in rule1_matches if m[0] == best_score]
        tied_configs.sort(key=lambda m: (_distance_from_cwd(m[1]), m[1]))
        return tied_configs[0][2]

    # 2. Nearest .agent-work/workspaces.toml walking upwards from cwd
    curr = resolved_cwd
    while curr is not None:
        candidate = curr / ".agent-work" / "workspaces.toml"
        if candidate.is_file():
            return candidate
        parent = curr.parent
        if parent == curr:
            break
        curr = parent

    # 3. A config whose route path or coordination_root lies inside cwd (longest match first)
    rule3_matches: list[tuple[tuple[int, int], str, Path]] = []
    for cfg_path, cfg in loaded_configs:
        norm_cfg_str = os.path.normcase(str(cfg_path))
        best_rule3_score_for_cfg: tuple[int, int] | None = None

        candidate_targets: list[Path] = [route.path for route in cfg.routes.values()]
        candidate_targets.append(cfg.coordination_root)

        for target in candidate_targets:
            try:
                norm_target_str = os.path.normcase(str(target.resolve(strict=False)))
            except OSError:
                continue
            norm_target = Path(norm_target_str)

            if norm_cwd in norm_target.parents:
                score = (len(norm_target.parts), len(norm_target_str))
                if best_rule3_score_for_cfg is None or score > best_rule3_score_for_cfg:
                    best_rule3_score_for_cfg = score

        if best_rule3_score_for_cfg is not None:
            rule3_matches.append((best_rule3_score_for_cfg, norm_cfg_str, cfg_path))

    if rule3_matches:
        best_score = max(m[0] for m in rule3_matches)
        tied_configs = [m for m in rule3_matches if m[0] == best_score]
        tied_configs.sort(key=lambda m: (_distance_from_cwd(m[1]), m[1]))
        return tied_configs[0][2]

    # 4. Fallback default config path
    return default_config_path()


def _decode_initialize_payload(body: str) -> Any:
    """`initialize` comes back either as plain JSON or as a text/event-stream `data:` frame."""
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        pass

    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:") :].strip())
            except (ValueError, TypeError):
                continue

    return None


def probe_mcp_health_detailed(port: int, timeout_sec: float = 2.0) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/mcp"
    req_data = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "acp-probe", "version": "1.0.0"},
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    session_id: str | None = None
    try:
        # The URL is always a http://127.0.0.1:<port>/mcp literal this code constructs
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:  # nosec B310
            session_id = response.headers.get("Mcp-Session-Id")
            if response.status == 200:
                body = response.read().decode("utf-8")
                payload = _decode_initialize_payload(body)
                if payload is None:
                    return (
                        False,
                        "something answered but not as an MCP server "
                        "(body is neither JSON nor an SSE data frame)",
                    )
                if (
                    isinstance(payload, dict)
                    and payload.get("jsonrpc") == "2.0"
                    and ("result" in payload or "protocolVersion" in payload)
                ):
                    return True, "healthy"
                return (
                    False,
                    "something answered but not as an MCP server (payload missing result or protocolVersion)",
                )
            return False, f"something answered but not as an MCP server (HTTP {response.status})"
    except urllib.error.HTTPError as err:
        session_id = err.headers.get("Mcp-Session-Id") if err.headers else None
        return False, f"something answered but not as an MCP server (HTTP {err.code})"
    except (urllib.error.URLError, OSError) as err:
        return False, f"nothing is listening ({err})"
    except (ValueError, TypeError) as err:
        return False, f"probe error ({err})"
    finally:
        if session_id:
            try:
                delete_req = urllib.request.Request(
                    url,
                    headers={"Mcp-Session-Id": session_id},
                    method="DELETE",
                )
                with urllib.request.urlopen(delete_req, timeout=timeout_sec):  # nosec B310
                    pass
            except (urllib.error.URLError, OSError, ValueError):
                pass


def probe_mcp_health(port: int, timeout_sec: float = 2.0) -> bool:
    healthy, _reason = probe_mcp_health_detailed(port, timeout_sec=timeout_sec)
    return healthy


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def port_assignments_path() -> Path:
    return Path.home() / ".agent-control-plane" / "port-assignments.json"


def _probe_mcp_health_while_booting(port: int, grace_sec: float = 5.0) -> bool:
    """A server that has just bound its port does not answer yet.

    Callers that do not hold the config lock — `mcp wire`, `mcp ensure --no-start` —
    must not conclude from that silence that the port belongs to somebody else, or they
    move the config to a second port while its own server is still starting up.
    """
    deadline = time.monotonic() + grace_sec
    while True:
        if probe_mcp_health(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def port_for(config_path: Path | str) -> int:
    resolved = Path(config_path).expanduser().resolve(strict=False)
    canonical = os.path.normcase(str(resolved))

    assignments_file = port_assignments_path()
    lock_file = assignments_file.with_suffix(".lock")

    with _file_lock(lock_file):
        assignments: dict[str, int] = {}
        if assignments_file.is_file():
            try:
                raw_text = assignments_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"Failed to read port assignments file {assignments_file}: {exc}"
                ) from exc

            if raw_text.strip():
                try:
                    data = json.loads(raw_text)
                    if not isinstance(data, dict):
                        raise ValueError(
                            f"Expected dict in {assignments_file}, got {type(data).__name__}"
                        )
                    assignments = {str(k): int(v) for k, v in data.items() if isinstance(v, int)}
                except (ValueError, KeyError, TypeError) as exc:
                    timestamp = time.strftime("%Y%m%d-%H%M%S")
                    corrupt_path = assignments_file.with_name(
                        f"{assignments_file.name}.corrupt-{timestamp}"
                    )
                    if corrupt_path.exists():
                        corrupt_path = assignments_file.with_name(
                            f"{assignments_file.name}.corrupt-{timestamp}-{os.getpid()}"
                        )
                    moved = False
                    with suppress(OSError):
                        os.replace(assignments_file, corrupt_path)
                        moved = True
                    kept = f"; its contents were kept at {corrupt_path}" if moved else ""
                    raise RuntimeError(
                        f"Failed to parse port assignments file {assignments_file}: {exc}{kept}"
                    ) from exc

        if canonical in assignments:
            remembered = assignments[canonical]
            if 9230 <= remembered <= 9329 and (
                not _is_port_open(remembered) or _probe_mcp_health_while_booting(remembered)
            ):
                return remembered

        base_hash = int(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), 16)
        base_port = 9230 + (base_hash % 100)

        for offset in range(100):
            cand_port = 9230 + ((base_port - 9230 + offset) % 100)
            owner = next((cfg for cfg, p in assignments.items() if p == cand_port), None)
            if owner is not None and owner != canonical:
                continue

            if not _is_port_open(cand_port) or (owner == canonical and probe_mcp_health(cand_port)):
                assignments[canonical] = cand_port
                tmp_file = assignments_file.with_suffix(".tmp")
                tmp_file.write_text(json.dumps(assignments, indent=2), encoding="utf-8")
                os.replace(tmp_file, assignments_file)
                return cand_port

        # If every candidate port in 9230-9329 is exhausted (recorded for other configs
        # or occupied by non-own servers), raise RuntimeError rather than returning
        # the un-scanned base_port which would reintroduce the collision.
        raise RuntimeError(
            f"All control plane candidate ports in range 9230-9329 are exhausted or occupied for {canonical}"
        )


def ensure_mcp_server(
    *,
    cwd: Path | str | None = None,
    config_path: Path | str | None = None,
    print_url: bool = False,
    no_start: bool = False,
    timeout_sec: float = 90.0,
) -> dict[str, Any]:
    if config_path is not None:
        target_config = Path(config_path).expanduser().resolve(strict=False)
        register_known_config(target_config)
    else:
        target_cwd = (Path(cwd) if cwd else Path.cwd()).expanduser().resolve(strict=False)
        target_config = resolve_config_for(target_cwd)

    # The port is resolved under the same lock that starts the server: resolving it
    # first lets a second session read the port while the first is still booting on it,
    # decide the port is taken, and give the same config a second port and a second
    # server. One instance per config only holds if both steps happen under one lock.
    with interprocess_config_lock(target_config):
        target_port = port_for(target_config)
        mcp_url = f"http://127.0.0.1:{target_port}/mcp"

        if probe_mcp_health(target_port):
            return {
                "ok": True,
                "config_path": str(target_config),
                "port": target_port,
                "url": mcp_url,
                "running": True,
                "started": False,
            }

        if no_start:
            return {
                "ok": True,
                "config_path": str(target_config),
                "port": target_port,
                "url": mcp_url,
                "running": False,
                "started": False,
            }

        log_dir = Path.home() / ".agent-control-plane" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"mcp-server-{target_port}.log"

        out_handle = log_file.open("a", encoding="utf-8")

        cmd = [
            sys.executable,
            "-m",
            "agent_control_plane.app.runtime.mcp_server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(target_port),
            "--config",
            str(target_config),
        ]

        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached server has no console at
            # all, so the very next process in the chain — the venv launcher re-executing
            # the real interpreter — makes Windows allocate a fresh one, which the default
            # terminal turns into a window that pops up and steals focus. A windowless
            # console is inherited down the chain instead, and the new process group still
            # keeps the server clear of the caller's Ctrl+C.
            win32_create_no_window = 0x08000000
            win32_create_new_process_group = 0x00000200
            creationflags = win32_create_no_window | win32_create_new_process_group

        # Argv is built from sys.executable and values resolved by ACP itself, never from caller-supplied strings
        subprocess.Popen(  # nosec B603
            cmd,
            creationflags=creationflags,
            stdout=out_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if probe_mcp_health(target_port, timeout_sec=0.5):
                return {
                    "ok": True,
                    "config_path": str(target_config),
                    "port": target_port,
                    "url": mcp_url,
                    "running": True,
                    "started": True,
                }
            time.sleep(0.1)

        if probe_mcp_health(target_port, timeout_sec=0.5):
            return {
                "ok": True,
                "config_path": str(target_config),
                "port": target_port,
                "url": mcp_url,
                "running": True,
                "started": True,
            }

        raise RuntimeError(
            f"Timed out after {timeout_sec}s waiting for MCP server on port {target_port} "
            f"(config: {target_config}, log: {log_file})"
        )


def find_enclosing_git_repo(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    curr: Path | None = resolved
    while curr is not None:
        if (curr / ".git").exists():
            return curr
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    return resolved


def _linked_worktree_canonical_config(worktree_root: Path) -> Path | None:
    """Find the main checkout config only for a real linked Git worktree."""
    if not (worktree_root / ".git").is_file():
        return None
    try:
        git_dir = _git_path_from_worktree(worktree_root, "--git-dir")
        common_git_dir = _git_path_from_worktree(worktree_root, "--git-common-dir")
    except (GitError, OSError):
        return None
    if git_dir == common_git_dir or common_git_dir.name != ".git":
        return None
    candidate = common_git_dir.parent / ".agent-work" / "workspaces.toml"
    return candidate if candidate.is_file() else None


def _git_path_from_worktree(worktree_root: Path, argument: str) -> Path:
    git_path = Path(run_git(worktree_root, "rev-parse", argument))
    if not git_path.is_absolute():
        git_path = worktree_root / git_path
    return git_path.resolve(strict=False)


def wire_mcp_servers(
    *,
    cwd: Path | str | None = None,
    config_path: Path | str | None = None,
    apply: bool = False,
    print_output: bool = False,
) -> dict[str, Any]:
    if config_path is not None:
        target_config = Path(config_path).expanduser().resolve(strict=False)
        if not target_config.is_file():
            raise FileNotFoundError(f"Config file not found: {target_config}")
        register_known_config(target_config)
    else:
        target_cwd = (Path(cwd) if cwd else Path.cwd()).expanduser().resolve(strict=False)
        target_config = resolve_config_for(target_cwd)
        if not target_config.is_file():
            raise FileNotFoundError(f"Config file not found: {target_config}")

    cfg = load_config(target_config)
    target_port = port_for(target_config)
    mcp_url = f"http://127.0.0.1:{target_port}/mcp"

    candidate_paths: list[Path] = [cfg.coordination_root]
    for route in cfg.routes.values():
        candidate_paths.append(route.path)

    client_repos: list[Path] = []
    seen_repos: set[Path] = set()
    for cand in candidate_paths:
        repo = find_enclosing_git_repo(cand)
        if repo not in seen_repos:
            seen_repos.add(repo)
            client_repos.append(repo)

    targets: list[dict[str, Any]] = []
    for repo in client_repos:
        mcp_json_path = repo / ".mcp.json"
        existing_data: dict[str, Any] = {}
        if mcp_json_path.is_file():
            try:
                content = mcp_json_path.read_text(encoding="utf-8")
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    existing_data = parsed
            except (OSError, ValueError):
                existing_data = {}

        mcp_servers: dict[str, Any] = {}
        if isinstance(existing_data.get("mcpServers"), dict):
            mcp_servers = dict(existing_data["mcpServers"])

        mcp_servers["agent_control_plane"] = {
            "type": "http",
            "url": mcp_url,
        }

        new_data = dict(existing_data)
        new_data["mcpServers"] = mcp_servers

        existed = mcp_json_path.is_file()
        is_changed = new_data != existing_data

        if not existed:
            action = "created" if apply else "would_create"
        elif is_changed:
            action = "updated" if apply else "would_update"
        else:
            action = "unchanged" if apply else "would_keep"

        if apply and (not existed or is_changed):
            mcp_json_path.parent.mkdir(parents=True, exist_ok=True)
            mcp_json_path.write_text(json.dumps(new_data, indent=2) + "\n", encoding="utf-8")

        targets.append(
            {
                "repo_path": str(repo),
                "mcp_json_path": str(mcp_json_path),
                "action": action,
                "mcp_json": new_data,
            }
        )

    return {
        "ok": True,
        "config_path": str(target_config),
        "port": target_port,
        "url": mcp_url,
        "apply": apply,
        "print_output": print_output,
        "targets": targets,
    }


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    config_contents: bytes | None = None,
) -> ControlConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    config_path = config_path.resolve(strict=False)
    if path is not None:
        register_known_config(config_path)

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
        native_quality_global_max_parallel=_positive_int(
            defaults_raw.get("native_quality_global_max_parallel", 4),
            "native_quality_global_max_parallel",
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
        no_progress_timeout_sec=_non_negative_int(
            _aliased_default(
                defaults_raw, "no_progress_timeout_sec", "codex_no_progress_timeout_sec", 240
            ),
            "no_progress_timeout_sec",
        ),
        dirty_diff_max_changed_lines=_non_negative_int(
            defaults_raw.get(
                "dirty_diff_max_changed_lines",
                DEFAULT_DIRTY_DIFF_MAX_CHANGED_LINES,
            ),
            "dirty_diff_max_changed_lines",
        ),
        tool_timeout_limit=_non_negative_int(
            _aliased_default(defaults_raw, "tool_timeout_limit", "codex_tool_timeout_limit", 6),
            "tool_timeout_limit",
        ),
        tool_call_budget_grace_sec=_non_negative_int(
            _aliased_default(
                defaults_raw,
                "tool_call_budget_grace_sec",
                "codex_tool_call_budget_grace_sec",
                120,
            ),
            "tool_call_budget_grace_sec",
        ),
        invalid_verification_grace_sec=_non_negative_int(
            _aliased_default(
                defaults_raw,
                "invalid_verification_grace_sec",
                "codex_invalid_verification_grace_sec",
                120,
            ),
            "invalid_verification_grace_sec",
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
        claude_model=_string_value(defaults_raw.get("claude_model", "default")),
        claude_reasoning_effort=_string_value(
            defaults_raw.get("claude_reasoning_effort", "medium")
        ),
        claude_permission_mode=_claude_permission_mode_value(
            defaults_raw.get("claude_permission_mode", "acceptEdits")
        ),
        claude_allowed_tools=_string_tuple(
            defaults_raw.get(
                "claude_allowed_tools",
                ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
            )
        ),
        claude_sessions_root=_optional_path(
            defaults_raw,
            "claude_sessions_root",
            project_root,
        ),
        claude_max_turns=_non_negative_int(
            defaults_raw.get("claude_max_turns", 0),
            "claude_max_turns",
        ),
        claude_bare=bool(defaults_raw.get("claude_bare", True)),
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
    claude_model_catalog = _claude_model_catalog_config(control.get("claude_model_catalog", {}))

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
            slot_root=_optional_path(value, "slot_root", project_root),
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
            claude_model=_optional_string_value(value.get("claude_model")),
            claude_reasoning_effort=_optional_string_value(value.get("claude_reasoning_effort")),
            codex_forbidden_tool_markers=_optional_string_tuple(
                value.get("codex_forbidden_tool_markers")
            ),
            workspace_access=_optional_workspace_access_value(value.get("workspace_access")),
            native_quality_policy=native_quality_policy,
            native_quality_max_parallel=native_quality_max_parallel,
            native_quality_gates=native_quality_gates,
            monitor_route_root=bool(value.get("monitor_route_root", True)),
            route_root_ignore_globs=_route_root_ignore_globs(
                name,
                value.get("route_root_ignore_globs", []),
            ),
            dirty_diff_max_changed_lines=_optional_non_negative_int(
                value.get("dirty_diff_max_changed_lines"),
                f"routes.{name}.dirty_diff_max_changed_lines",
            ),
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

    agy_launch_mode = _agy_launch_mode(control)
    agy_proxy_launcher = _optional_unresolved_path(control, "agy_proxy_launcher", project_root)
    if agy_launch_mode == "managed" and agy_proxy_launcher is None:
        raise ValueError("[control].agy_launch_mode = managed requires agy_proxy_launcher")
    if agy_launch_mode == "managed" and agy_proxy_launcher is not None:
        _validate_managed_adapter_path(agy_proxy_launcher)

    return ControlConfig(
        config_path=config_path,
        project_root=project_root,
        coordination_root=_path(control, "coordination_root", project_root),
        runs_root=_path(control, "runs_root", project_root),
        database_path=_path(control, "database", project_root),
        worktree_root=global_worktree_root,
        worktree_base=resolved_worktree_base,
        slot_root=slot_root,
        agy_command=_string_value(control.get("agy_command", "agy")),
        codex_command=str(control.get("codex_command", "codex")),
        claude_command=str(control.get("claude_command", "claude")),
        claude_mcp_servers=MappingProxyType(_claude_mcp_servers(control)),
        claude_config_path=_claude_config_path_value(control),
        agy_proxy_launcher=agy_proxy_launcher,
        agy_launch_mode=agy_launch_mode,
        defaults=defaults,
        model_catalog=model_catalog,
        routing_policies=routing_policies,
        claude_model_catalog=claude_model_catalog,
        routes=MappingProxyType(routes),
        slots=MappingProxyType(slots),
        slot_prepare=tuple(slot_prepare),
    )


def _agy_launch_mode(control: Mapping[str, Any]) -> str:
    value = _string_value(control.get("agy_launch_mode", "unmanaged"))
    if value not in {"managed", "unmanaged"}:
        raise ValueError("[control].agy_launch_mode must be managed or unmanaged")
    return value


def _claude_mcp_servers(control: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = control.get("claude_mcp_servers", {})
    if not isinstance(raw, dict):
        raise ValueError("[control.claude_mcp_servers] must be a table")
    result: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(definition, dict):
            raise ValueError(f"[control.claude_mcp_servers.{name}] must be a table")
        result[str(name)] = dict(definition)
    return result


def _claude_config_path_value(control: Mapping[str, Any]) -> Path | None:
    raw = control.get("claude_config_path")
    if raw is None:
        return None
    return Path(_string_value(raw)).expanduser()


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
    newness_window_days = _positive_float(
        raw.get("newness_window_days", 7.0),
        "control.model_catalog.newness_window_days",
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
    configured_rules = raw.get("model_rules", [])
    if not isinstance(configured_rules, list):
        raise ValueError("control.model_catalog.model_rules must be an array of tables")
    model_rules = [_model_rule_config(value) for value in configured_rules]
    rule_matches = [rule.match.lower() for rule in model_rules]
    if len(rule_matches) != len(set(rule_matches)):
        raise ValueError("control.model_catalog.model_rules contains duplicate match patterns")
    configured_model_ids = {model.model.lower() for model in models}
    for legacy_model in legacy_secondary_models:
        if legacy_model.lower() not in configured_model_ids:
            models.append(
                CodexModelMetadataConfig(
                    model=legacy_model,
                    premium=False,
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
    for rule in model_rules:
        if rule.quota_domain is not None and rule.quota_domain not in configured_domains_by_name:
            raise ValueError(
                f"Model catalog rule references unknown quota domain: {rule.quota_domain}"
            )
    unknown_model_policy = raw.get("unknown_model_policy", "warn")
    if not isinstance(unknown_model_policy, str) or unknown_model_policy not in (
        "allow",
        "warn",
        "require_override",
    ):
        raise ValueError(
            "control.model_catalog.unknown_model_policy must be one of: allow, warn, require_override"
        )
    return CodexModelCatalogConfig(
        cache_path=cache_path,
        max_cache_age_sec=max_cache_age_sec,
        models=tuple(models),
        model_rules=tuple(model_rules),
        quota_domains=quota_domains,
        newness_window_days=newness_window_days,
        unknown_model_policy=unknown_model_policy,
    )


def _claude_model_catalog_config(raw: Any) -> ClaudeModelCatalogConfig:
    if not isinstance(raw, dict):
        raise ValueError("[control.claude_model_catalog] must be a table")
    configured_models = raw.get("models", [])
    if not isinstance(configured_models, list):
        raise ValueError("control.claude_model_catalog.models must be an array of tables")
    models = tuple(_model_metadata_config(value) for value in configured_models)
    model_ids = [model.model.lower() for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise ValueError("control.claude_model_catalog.models contains duplicate model IDs")
    configured_rules = raw.get("model_rules", [])
    if not isinstance(configured_rules, list):
        raise ValueError("control.claude_model_catalog.model_rules must be an array of tables")
    model_rules = tuple(_model_rule_config(value) for value in configured_rules)
    rule_matches = [rule.match.lower() for rule in model_rules]
    if len(rule_matches) != len(set(rule_matches)):
        raise ValueError(
            "control.claude_model_catalog.model_rules contains duplicate match patterns"
        )
    configured_inventory = raw.get("inventory", [])
    if not isinstance(configured_inventory, list):
        raise ValueError("control.claude_model_catalog.inventory must be an array of tables")
    inventory = tuple(_claude_inventory_config(value) for value in configured_inventory)
    inventory_ids = [entry.model.lower() for entry in inventory]
    if len(inventory_ids) != len(set(inventory_ids)):
        raise ValueError("control.claude_model_catalog.inventory contains duplicate model IDs")
    unknown_model_policy = raw.get("unknown_model_policy", "warn")
    if not isinstance(unknown_model_policy, str) or unknown_model_policy not in (
        "allow",
        "warn",
        "require_override",
    ):
        raise ValueError(
            "control.claude_model_catalog.unknown_model_policy must be one of: allow, warn, require_override"
        )
    return ClaudeModelCatalogConfig(
        models=models,
        model_rules=model_rules,
        inventory=inventory,
        unknown_model_policy=unknown_model_policy,
    )


def _claude_inventory_config(raw: Any) -> ClaudeModelInventoryConfig:
    if not isinstance(raw, Mapping):
        raise ValueError("control.claude_model_catalog.inventory entries must be tables")
    model = _string_value(_required(raw, "model")).strip()
    if not model:
        raise ValueError("control.claude_model_catalog.inventory model must not be empty")
    efforts = raw.get("supported_reasoning_efforts")
    if not isinstance(efforts, list) or not efforts:
        raise ValueError(
            "control.claude_model_catalog.inventory supported_reasoning_efforts "
            f"must be a non-empty array: {model}"
        )
    priority = raw.get("priority")
    if priority is not None and not isinstance(priority, int):
        raise ValueError(f"control.claude_model_catalog.inventory priority must be int: {model}")
    return ClaudeModelInventoryConfig(
        model=model,
        visible=bool(raw.get("visible", True)),
        priority=priority,
        default_reasoning_effort=_optional_string_value(raw.get("default_reasoning_effort")),
        supported_reasoning_efforts=_string_tuple(efforts),
    )


def _claude_permission_mode_value(value: Any) -> str:
    mode = _string_value(value).strip()
    allowed = ("default", "acceptEdits", "plan", "dontAsk")
    if mode not in allowed:
        raise ValueError(
            f"Unsupported claude_permission_mode {mode!r}. Expected one of: {', '.join(allowed)}"
        )
    return mode


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
        premium=_boolean_value(raw.get("premium", False), f"Model catalog premium: {model}"),
        quota_domain=quota_domain,
        capacity_units=capacity_units,
        credit_rate=credit_rate,
        api_usd_rate=api_usd_rate,
        rate_card_version=rate_card_version,
        rate_card_source=rate_card_source,
    )


_ALLOWED_RULE_KEYS = {
    "match",
    "quota_domain",
    "capacity_units",
    "credit_rate",
    "api_usd_rate",
    "rate_card_version",
    "rate_card_source",
    "premium",
}


def _model_rule_config(raw: Any) -> CodexModelRuleConfig:
    if not isinstance(raw, dict):
        raise ValueError("Each model catalog rule entry must be a table")
    unknown_keys = set(raw.keys()) - _ALLOWED_RULE_KEYS
    if unknown_keys:
        raise ValueError(
            f"Model catalog rule contains unknown field(s): {', '.join(sorted(unknown_keys))}"
        )
    match_val = _optional_string_value(_required(raw, "match"))
    if match_val is None:
        raise ValueError("Model catalog rule match pattern must not be empty")
    match = match_val.strip()
    quota_domain = _optional_string_value(raw.get("quota_domain"))
    if quota_domain is not None:
        quota_domain = quota_domain.lower()
    capacity_raw = raw.get("capacity_units", {})
    if not isinstance(capacity_raw, dict):
        raise ValueError(f"Model catalog capacity_units must be a table: match {match}")
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
        raise ValueError(f"Model catalog capacity_units contains duplicate efforts: match {match}")
    credit_rate = _catalog_rate(raw, match, "credit")
    api_usd_rate = _catalog_rate(raw, match, "api_usd")
    rate_card_version = _optional_string_value(raw.get("rate_card_version"))
    rate_card_source = _optional_string_value(raw.get("rate_card_source"))
    if (credit_rate is not None or api_usd_rate is not None) and (
        rate_card_version is None or rate_card_source is None
    ):
        raise ValueError(f"Model catalog rate metadata needs version and source: match {match}")
    return CodexModelRuleConfig(
        match=match,
        premium=_boolean_value(raw.get("premium", False), f"Model catalog premium: match {match}"),
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


def _aliased_default(
    defaults_raw: Mapping[str, Any], new_key: str, old_key: str, fallback: Any
) -> Any:
    """Read a `[control.defaults]` key that has a deprecated `codex_`-prefixed alias.

    The new key wins when both are present; the old key keeps working as a deprecated
    alias so existing configs are not broken by the rename.
    """
    if new_key in defaults_raw:
        return defaults_raw[new_key]
    if old_key in defaults_raw:
        return defaults_raw[old_key]
    return fallback


def _optional_path(raw: Mapping[str, Any], key: str, base: Path) -> Path | None:
    if key not in raw:
        return None
    return _coerce_path(raw[key], base)


def _optional_unresolved_path(raw: Mapping[str, Any], key: str, base: Path) -> Path | None:
    """Keep a configured managed-adapter spelling intact until symlink checks complete."""
    if key not in raw:
        return None
    path = Path(os.path.expandvars(str(raw[key]))).expanduser()
    return path if path.is_absolute() else base / path


def _validate_managed_adapter_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "[control].agy_proxy_launcher must be an absolute path without parent traversal"
        )
    for candidate in reversed((path, *path.parents)):
        try:
            status = candidate.lstat()
        except OSError as exc:
            raise ValueError(
                f"[control].agy_proxy_launcher parent is unavailable: {candidate}"
            ) from exc
        if stat.S_ISLNK(status.st_mode):
            raise ValueError(
                f"[control].agy_proxy_launcher must not traverse a symlink: {candidate}"
            )
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("[control].agy_proxy_launcher must be an executable regular file")


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


def _optional_non_negative_int(
    value: Any,
    key: str = "control.defaults.auto_archive_days",
) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{key} must be non-negative")
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


def _boolean_value(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


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


def _route_root_ignore_globs(route_name: str, value: Any) -> tuple[str, ...]:
    globs = _string_tuple(value)
    for pattern in globs:
        if glob_matches_whole_tree(pattern):
            raise ValueError(
                f"routes.{route_name}.route_root_ignore_globs entry {pattern!r} matches the "
                "whole tree; the route root guard would be silenced entirely. Use a narrower "
                "pattern that only covers operator-owned paths."
            )
    return globs


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
