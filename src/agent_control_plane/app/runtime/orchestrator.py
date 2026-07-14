from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from agent_control_plane.entities.job import JobRecord, JobStore, format_events, new_job_id
from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.entities.workspace import (
    ForbiddenStatusEntry,
    StartRequest,
    WorkspacePolicy,
    find_forbidden_status_entries,
    find_new_forbidden_status_entries,
)
from agent_control_plane.features.agent_runner import (
    AGY_BACKEND,
    CODEX_BACKEND,
    SUPPORTED_BACKENDS,
    AgentRunner,
    AgentRunSpec,
    CodexExecRunner,
    CodexRateLimitReader,
    GlobalQuotaBroker,
    ModelProfile,
    ModelRoutingPolicy,
    PtyAgyRunner,
    QuotaDecision,
    build_task_prompt,
    inspect_result,
    normalize_backend,
)
from agent_control_plane.features.antigravity_accounts import (
    AntigravityManagerAdapter,
    AntigravityManagerError,
    is_agy_quota_failure,
)
from agent_control_plane.features.slot_lifecycle import (
    SlotError,
    SlotManager,
    bootstrap_slot_config,
)
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.config import ControlConfig, load_config
from agent_control_plane.shared.git_tools import GitError, diff_patch, workspace_state


class PolicyError(RuntimeError):
    pass


TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "partial",
        "blocked",
        "failed",
        "cancelled",
        "guardrail_violation",
        "worker_error",
        "stopped_dirty_after_failure",
    }
)
CODEX_DIRTY_DIFF_MAX_CHANGED_LINES = 500


def _compact_status_preview(porcelain: str, *, limit: int = 8) -> str:
    lines = [line.strip() for line in porcelain.splitlines() if line.strip()]
    if not lines:
        return "none"
    if len(lines) <= limit:
        return "; ".join(lines)
    return "; ".join(lines[:limit]) + f"; ... ({len(lines) - limit} more)"


def _status_paths(porcelain: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", maxsplit=1)[1].strip()
        if len(path) >= 2 and path[0] == path[-1] == '"':
            path = path[1:-1]
        if path:
            paths.append(path)
    return tuple(paths)


@dataclass(frozen=True)
class StartOptions:
    task_id: str
    route: str
    backend: str | None = None
    agy_model: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    codex_quality_tier: str | None = None
    codex_tool_call_budget: int | None = None
    slot: str | None = None
    workspace_path: Path | None = None
    expected_branch: str | None = None
    timeout_sec: int | None = None
    idle_timeout_sec: int | None = None
    print_timeout: str | None = None
    max_restarts: int | None = None
    yolo: bool | None = None
    allow_dirty: bool | None = None
    read_only: bool = False


@dataclass(frozen=True)
class GuardrailBaseline:
    entries: tuple[ForbiddenStatusEntry, ...]
    fingerprints: dict[tuple[str, str, str], str]
    diff_changed_lines: int = 0


@dataclass(frozen=True)
class WorkspaceDirtyBaseline:
    path: Path
    fingerprints: dict[str, str]


def _process_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class AgentControlPlane:
    def __init__(self, config: ControlConfig) -> None:
        self.config = config
        self.store = JobStore(config.database_path)
        self.slot_store = SlotStore(config.database_path)
        self.slots = SlotManager(config, self.slot_store)
        self.policy = WorkspacePolicy(config)
        defaults = config.defaults
        self.model_routing = ModelRoutingPolicy(
            mechanical=ModelProfile(
                defaults.codex_mechanical_model,
                defaults.codex_mechanical_reasoning_effort,
            ),
            balanced=ModelProfile(
                defaults.codex_balanced_model,
                defaults.codex_balanced_reasoning_effort,
            ),
            deep=ModelProfile(
                defaults.codex_deep_model,
                defaults.codex_deep_reasoning_effort,
            ),
        )
        self.quota_broker: GlobalQuotaBroker | None = None
        if defaults.codex_global_quota_database is not None:
            rate_limit_reader = (
                CodexRateLimitReader(defaults.codex_sessions_root).latest
                if defaults.codex_sessions_root is not None
                else None
            )
            self.quota_broker = GlobalQuotaBroker(
                defaults.codex_global_quota_database,
                max_concurrent_jobs=defaults.codex_global_max_concurrent_jobs,
                soft_limit_percent=defaults.codex_five_hour_soft_limit_percent,
                rate_limit_reader=rate_limit_reader,
            )

    @staticmethod
    def _runner_for_backend(backend: str) -> AgentRunner:
        if backend == AGY_BACKEND:
            return PtyAgyRunner()
        if normalize_backend(backend) == CODEX_BACKEND:
            return CodexExecRunner()
        allowed = ", ".join(SUPPORTED_BACKENDS)
        raise PolicyError(f"Unsupported backend {backend!r}. Expected one of: {allowed}")

    @classmethod
    def from_config_path(
        cls,
        config_path: str | os.PathLike[str] | None = None,
    ) -> AgentControlPlane:
        return cls(load_config(config_path))

    def smoke(self) -> dict[str, Any]:
        self.store.initialize()
        return {
            "config": str(self.config.config_path),
            "database": str(self.config.database_path),
            "runs_root": str(self.config.runs_root),
            "agy_on_path": shutil.which(self.config.agy_command),
            "codex_on_path": shutil.which(self.config.codex_command),
            "default_backend": self.config.defaults.backend,
            "agy_model": self.config.defaults.agy_model,
            "codex_model": self.config.defaults.codex_model,
            "codex_reasoning_effort": self.config.defaults.codex_reasoning_effort,
            "codex_quality_tier": self.config.defaults.codex_quality_tier,
            "codex_tool_call_budgets": {
                "mechanical": self.config.defaults.codex_mechanical_tool_call_budget,
                "balanced": self.config.defaults.codex_balanced_tool_call_budget,
                "deep": self.config.defaults.codex_deep_tool_call_budget,
            },
            "codex_quality_profiles": {
                tier: [
                    {"model": profile.model, "reasoning_effort": profile.reasoning_effort}
                    for profile in self.model_routing.ladder_for_tier(tier)
                ]
                for tier in ("mechanical", "balanced", "deep")
            },
            "codex_global_quota": {
                "enabled": self.quota_broker is not None,
                "database": (
                    str(self.config.defaults.codex_global_quota_database)
                    if self.config.defaults.codex_global_quota_database
                    else None
                ),
                "max_concurrent_jobs": self.config.defaults.codex_global_max_concurrent_jobs,
                "five_hour_soft_limit_percent": (
                    self.config.defaults.codex_five_hour_soft_limit_percent
                ),
                "poll_sec": self.config.defaults.codex_quota_poll_sec,
            },
            "runs_layout": self.config.defaults.runs_layout,
            "auto_archive_days": self.config.defaults.auto_archive_days,
            "auto_archive_limit": self.config.defaults.auto_archive_limit,
            "guardrails": {
                "poll_sec": self.config.defaults.guardrail_poll_sec,
                "forbidden_status_globs": self.config.defaults.forbidden_status_globs,
                "codex_dirty_diff_max_changed_lines": CODEX_DIRTY_DIFF_MAX_CHANGED_LINES,
                "codex_no_progress_timeout_sec": (
                    self.config.defaults.codex_no_progress_timeout_sec
                ),
            },
            "antigravity_manager": {
                "auto_switch_agy_on_quota": self.config.defaults.auto_switch_agy_on_quota,
                "auto_switch_agy_strategy": self.config.defaults.auto_switch_agy_strategy,
                "auto_switch_agy_electron_command": (
                    self.config.defaults.auto_switch_agy_electron_command
                ),
            },
            "routes": {
                name: {
                    "path": str(route.path),
                    "exists": route.path.exists(),
                    "required_branch": route.required_branch,
                    "backend": route.backend or self.config.defaults.backend,
                    "agy_model": route.agy_model or self.config.defaults.agy_model,
                    "codex_model": route.codex_model or self.config.defaults.codex_model,
                    "codex_reasoning_effort": (
                        route.codex_reasoning_effort or self.config.defaults.codex_reasoning_effort
                    ),
                    "agy_mcp_server": route.agy_mcp_server or "idea",
                    "worktree_root": str(route.worktree_root) if route.worktree_root else None,
                    "worktree_base": str(route.worktree_base),
                    "source_roots": [str(path) for path in route.source_roots],
                    "test_roots": [str(path) for path in route.test_roots],
                    "exclude_dirs": [str(path) for path in route.exclude_dirs],
                }
                for name, route in self.config.routes.items()
            },
            "slots": {status.name: status.as_dict() for status in self.slots.list_slots()},
            "slot_root": str(self.config.slot_root),
            "worktree_base": str(self.config.worktree_base),
            "slot_prepare": [
                {
                    "name": command.name,
                    "working_dir": str(command.working_dir),
                    "marker": str(command.marker) if command.marker is not None else None,
                    "command": list(command.command),
                    "timeout_sec": command.timeout_sec,
                    "routes": list(command.routes),
                }
                for command in self.config.slot_prepare
            ],
        }

    @staticmethod
    def _initialize_task_artifacts(
        *,
        task_id: str,
        job_id: str,
        workspace_path: Path,
        expected_branch: str,
        result_path: Path,
    ) -> None:
        task_dir = result_path.parent
        progress_path = task_dir / "agent-progress.md"
        task_dir.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(
            "Current phase: queued by control-plane\n"
            "Confirmed facts:\n"
            f"- Task ID: {task_id}\n"
            f"- Job ID: {job_id}\n"
            f"- Workspace: {workspace_path}\n"
            f"- Expected branch: {expected_branch}\n"
            "- Control-plane initialized this progress file before runner start.\n"
            "Target files:\n"
            "- none yet\n"
            "Next action:\n"
            "- Agent runner must update this file through the IDEA MCP selected "
            "in the generated prompt before repository exploration or edits.\n"
            "Changed files:\n"
            "- none\n"
            "Open risks:\n"
            "- Runner has not started yet.\n",
            encoding="utf-8",
        )
        result_path.write_text(
            "Status: blocked\n\n"
            "Not reviewed yet. Awaiting agent execution.\n\n"
            "Changed files: none\n\n"
            "What changed: nothing yet\n\n"
            "Verification performed: none\n\n"
            "Not verified / remaining risks: runner has not started yet\n",
            encoding="utf-8",
        )

    def start_job(self, options: StartOptions) -> JobRecord:
        allow_dirty = _option(options.allow_dirty, self.config.defaults.allow_dirty)
        workspace_path = options.workspace_path
        if options.slot:
            try:
                slot_status = self.slots.inspect_slot(options.slot)
            except SlotError as exc:
                raise PolicyError(str(exc)) from exc
            if slot_status.route != options.route:
                raise PolicyError(
                    f"Slot {options.slot} belongs to route {slot_status.route!r}, "
                    f"not {options.route!r}"
                )
            if workspace_path and workspace_path.resolve(strict=False) != slot_status.path.resolve(
                strict=False
            ):
                raise PolicyError(
                    f"Slot {options.slot} resolves to {slot_status.path}, "
                    f"but workspace_path was {workspace_path}"
                )
            workspace_path = slot_status.path

        check = self.policy.check_start(
            StartRequest(
                task_id=options.task_id,
                route=options.route,
                workspace_path=workspace_path,
                expected_branch=options.expected_branch,
                allow_dirty=allow_dirty,
            )
        )
        if not check.ok:
            raise PolicyError("\n".join(check.reasons))

        if check.workspace_path is None or check.expected_branch is None:
            raise PolicyError("Policy check did not resolve workspace path or expected branch")

        route_config = self.config.routes[options.route]
        backend = _backend_option(
            options.backend,
            route_config.backend,
            self.config.defaults.backend,
        )
        normalized_backend = normalize_backend(backend)
        if options.agy_model is not None and normalized_backend != AGY_BACKEND:
            raise PolicyError("--agy-model can only be used with the agy backend")
        agy_model = (
            options.agy_model or route_config.agy_model or self.config.defaults.agy_model
            if normalized_backend == AGY_BACKEND
            else None
        )
        explicit_codex_profile = any(
            value is not None
            for value in (
                options.codex_model,
                options.codex_reasoning_effort,
                route_config.codex_model,
                route_config.codex_reasoning_effort,
            )
        )
        codex_quality_tier: str | None = None
        codex_model: str | None = None
        codex_reasoning_effort: str | None = None
        if normalized_backend == CODEX_BACKEND:
            if not explicit_codex_profile:
                codex_quality_tier = (
                    options.codex_quality_tier or self.config.defaults.codex_quality_tier
                )
                try:
                    initial_profile = self.model_routing.ladder_for_tier(codex_quality_tier)[0]
                except ValueError as exc:
                    raise PolicyError(str(exc)) from exc
                codex_model = initial_profile.model
                codex_reasoning_effort = initial_profile.reasoning_effort
            else:
                codex_model = _option(
                    options.codex_model or route_config.codex_model,
                    self.config.defaults.codex_model,
                )
                codex_reasoning_effort = _option(
                    options.codex_reasoning_effort or route_config.codex_reasoning_effort,
                    self.config.defaults.codex_reasoning_effort,
                )

        codex_tool_call_budget: int | None = None
        if normalized_backend == CODEX_BACKEND:
            budget_tier = (
                options.codex_quality_tier
                or codex_quality_tier
                or self.config.defaults.codex_quality_tier
            )
            codex_tool_call_budget = (
                options.codex_tool_call_budget
                if options.codex_tool_call_budget is not None
                else _tool_call_budget_for_tier(self.config, budget_tier)
            )
            if codex_tool_call_budget <= 0:
                raise PolicyError("Codex tool-call budget must be positive")

        job_id = new_job_id(options.task_id)
        run_dir = self._run_dir_for_job(job_id)
        prompt_path = run_dir / "prompt.md"
        try:
            prompt = build_task_prompt(
                config=self.config,
                task_id=options.task_id,
                route=options.route,
                workspace_path=check.workspace_path,
                expected_branch=check.expected_branch,
                result_path=check.result_path,
                backend=backend,
                read_only=options.read_only,
                codex_tool_call_budget=codex_tool_call_budget or 0,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise PolicyError(str(exc)) from exc

        try:
            job = self.store.create_job(
                job_id=job_id,
                task_id=options.task_id,
                route=options.route,
                workspace_path=check.workspace_path,
                expected_branch=check.expected_branch,
                config_path=self.config.config_path,
                run_dir=run_dir,
                prompt_path=prompt_path,
                result_path=check.result_path,
                timeout_sec=_option(options.timeout_sec, self.config.defaults.timeout_sec),
                idle_timeout_sec=_option(
                    options.idle_timeout_sec,
                    self.config.defaults.idle_timeout_sec,
                ),
                print_timeout=_option(options.print_timeout, self.config.defaults.print_timeout),
                max_restarts=_option(options.max_restarts, self.config.defaults.max_restarts),
                yolo=_option(options.yolo, self.config.defaults.yolo),
                allow_dirty=allow_dirty,
                read_only=options.read_only,
                backend=backend,
                agy_model=agy_model,
                codex_model=codex_model,
                codex_reasoning_effort=codex_reasoning_effort,
                codex_quality_tier=codex_quality_tier,
                codex_tool_call_budget=codex_tool_call_budget,
                slot_name=options.slot,
            )
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc

        try:
            self._initialize_task_artifacts(
                task_id=options.task_id,
                job_id=job_id,
                workspace_path=check.workspace_path,
                expected_branch=check.expected_branch,
                result_path=check.result_path,
            )
            run_dir.mkdir(parents=True, exist_ok=False)
            prompt_path.write_text(prompt, encoding="utf-8")
        except Exception as exc:
            message = f"Could not initialize job artifacts: {exc}"
            self.store.add_event(job.job_id, "error", message)
            self._finish_job(job.job_id, "blocked", message)
            raise PolicyError(message) from exc

        self.store.add_event(job.job_id, "info", "Job created")
        if options.slot:
            try:
                self.slots.acquire_for_job(
                    options.slot,
                    job_id=job.job_id,
                    route=options.route,
                    allow_dirty=allow_dirty,
                )
            except SlotError as exc:
                self.store.add_event(job.job_id, "error", str(exc))
                return self._finish_job(job.job_id, "blocked", str(exc))

            if not options.read_only:
                try:
                    self.slots.ensure_ide_root_module()
                    if self.config.defaults.prepare_slots:
                        self.slots.prepare_slot(options.slot)
                except SlotError as exc:
                    message = f"Could not prepare slot {options.slot}: {exc}"
                    self.slots.release_for_job(options.slot, job_id=job.job_id)
                    self.store.add_event(job.job_id, "error", message)
                    return self._finish_job(job.job_id, "blocked", message)

        try:
            worker_pid = self._launch_worker(job.job_id)
        except Exception as exc:
            if options.slot:
                self.slots.release_for_job(options.slot, job_id=job.job_id)
            self._finish_job(job.job_id, "worker_error", str(exc))
            raise
        return self.store.update_job(job.job_id, status="queued", worker_pid=worker_pid)

    def run_job(self, job_id: str) -> JobRecord:
        job = self.store.update_job(
            job_id,
            status="running",
            worker_pid=os.getpid(),
            started_at=utc_now(),
        )
        self.store.add_event(job_id, "info", f"Worker started with PID {os.getpid()}")

        if normalize_backend(job.backend) == CODEX_BACKEND and self.quota_broker is not None:
            if not self._wait_for_codex_quota(job):
                message = "Cancel requested while waiting for global Codex quota"
                self._write_blocked_result_if_missing(job, message)
                return self._finish_job(job_id, "cancelled", message)
            job = self.store.get_job(job_id)

        check = self.policy.check_start(
            StartRequest(
                task_id=job.task_id,
                route=job.route,
                workspace_path=job.workspace_path,
                expected_branch=job.expected_branch,
                allow_dirty=job.allow_dirty,
            )
        )
        if not check.ok:
            message = "\n".join(check.reasons)
            self._write_blocked_result_if_missing(job, message)
            self.store.add_event(job_id, "error", message)
            return self._finish_job(job_id, "blocked", message)

        prompt = job.prompt_path.read_text(encoding="utf-8")
        attempt_prompt = prompt
        runner = self._runner_for_backend(job.backend)
        model_ladder = self._model_ladder_for_job(job)
        model_index = 0
        resume_thread_id: str | None = None
        attempts = job.max_restarts + (
            len(model_ladder) if normalize_backend(job.backend) == CODEX_BACKEND else 1
        )
        guardrail_baseline = self._guardrail_baseline(job)
        route_config = self.config.routes.get(job.route)
        route_root_baseline = self._route_root_dirty_baseline(job, route_config)
        last_result_message = f"{job.backend} did not run"
        quota_recovery_used = False
        codex_forbidden_tool_markers = (
            route_config.codex_forbidden_tool_markers
            if route_config and route_config.codex_forbidden_tool_markers is not None
            else self.config.defaults.codex_forbidden_tool_markers
        )

        attempt_no = 1
        while attempt_no <= attempts:
            if self.store.cancel_requested(job_id):
                message = "Cancel requested before attempt"
                self._write_blocked_result_if_missing(job, message)
                return self._finish_job(job_id, "cancelled", message)

            active_profile = model_ladder[model_index]
            log_path = job.run_dir / f"attempt-{attempt_no:03d}.log"
            self.store.start_attempt(job_id, attempt_no, log_path)
            self.store.update_job(
                job_id,
                status="running",
                log_path=log_path,
                runner_pid=None,
                agy_pid=None,
            )
            attempt_profile = (
                job.agy_model or "agy-default"
                if normalize_backend(job.backend) == AGY_BACKEND
                else f"{active_profile.model}/{active_profile.reasoning_effort}"
            )
            self.store.add_event(
                job_id,
                "info",
                f"Attempt {attempt_no} started with {attempt_profile}",
            )
            guardrail_message: str | None = None
            last_guardrail_check = 0.0

            def should_stop() -> bool:
                nonlocal guardrail_message, last_guardrail_check
                if self.store.cancel_requested(job_id):
                    return True
                if guardrail_message:
                    return True
                now = time.monotonic()
                if now - last_guardrail_check < self.config.defaults.guardrail_poll_sec:
                    return False
                last_guardrail_check = now
                guardrail_message = self._guardrail_violation_message(job, guardrail_baseline)
                if guardrail_message is None:
                    guardrail_message = self._route_root_guardrail_message(
                        job,
                        route_root_baseline,
                    )
                if guardrail_message is None:
                    guardrail_message = self._codex_dirty_diff_guardrail_message(
                        job, guardrail_baseline
                    )
                if guardrail_message:
                    self._preserve_dirty_state_if_needed(job, prefix="guardrail")
                    self.store.add_event(job_id, "error", guardrail_message)
                    self.store.update_job(
                        job_id,
                        status="guardrail_violation",
                        last_error=guardrail_message,
                    )
                    return True
                return False

            def record_runner_pid(pid: int | None) -> None:
                updates: dict[str, int | None] = {"runner_pid": pid}
                if job.backend == AGY_BACKEND:
                    updates["agy_pid"] = pid
                self.store.update_job(job_id, **updates)
                return None

            result = runner.run(
                AgentRunSpec(
                    backend=job.backend,
                    agy_command=self.config.agy_command,
                    agy_model=job.agy_model,
                    codex_command=self.config.codex_command,
                    codex_model=active_profile.model,
                    codex_reasoning_effort=active_profile.reasoning_effort,
                    codex_sandbox_mode=self.config.defaults.codex_sandbox_mode,
                    codex_disabled_mcp_servers=self.config.defaults.codex_disabled_mcp_servers,
                    prompt=attempt_prompt,
                    workspace_path=job.workspace_path,
                    result_path=job.result_path,
                    log_path=log_path,
                    print_timeout=job.print_timeout,
                    timeout_sec=job.timeout_sec,
                    idle_timeout_sec=job.idle_timeout_sec,
                    yolo=job.yolo,
                    read_only=job.read_only,
                    codex_no_progress_timeout_sec=(
                        self.config.defaults.codex_no_progress_timeout_sec
                    ),
                    codex_tool_call_budget=job.codex_tool_call_budget or 0,
                    codex_terminal_tab_name=job.task_id,
                    codex_forbidden_tool_markers=codex_forbidden_tool_markers,
                    codex_resume_thread_id=resume_thread_id,
                    codex_sessions_root=self.config.defaults.codex_sessions_root,
                ),
                cancel_requested=should_stop,
                pid_observed=record_runner_pid,
            )

            self.store.finish_attempt(
                job_id,
                attempt_no,
                result.status,
                result_status=result.result_status,
                exit_code=result.exit_code,
                message=result.message,
            )
            if result.metrics is not None:
                self.store.record_attempt_metrics(
                    job_id,
                    attempt_no,
                    backend=job.backend,
                    model=active_profile.model,
                    reasoning_effort=active_profile.reasoning_effort,
                    metrics=result.metrics,
                )
            self.store.update_job(job_id, runner_pid=None, agy_pid=None)
            self.store.add_event(job_id, "info", f"Attempt {attempt_no} ended: {result.status}")
            last_result_message = result.message

            if guardrail_message:
                self._write_blocked_result_if_missing(job, guardrail_message)
                return self._finish_job(job_id, "guardrail_violation", guardrail_message)

            has_next_model = model_index + 1 < len(model_ladder)
            if normalize_backend(
                job.backend
            ) == CODEX_BACKEND and self.model_routing.should_escalate(
                runner_status=result.status,
                result_status=result.result_status,
                has_next=has_next_model,
            ):
                model_index += 1
                if result.metrics is not None and result.metrics.thread_id:
                    resume_thread_id = result.metrics.thread_id
                next_profile = model_ladder[model_index]
                self.store.update_job(
                    job_id,
                    codex_model=next_profile.model,
                    codex_reasoning_effort=next_profile.reasoning_effort,
                )
                continuation = (
                    "Continue the same assigned task from the existing workspace state. "
                    f"The prior attempt ended as {result.status}/"
                    f"{result.result_status or 'no-result'}: {result.message}. "
                    "Review the current changes, finish the implementation, run the required "
                    "checks, and write the required result.md with a final Status marker."
                )
                attempt_prompt = continuation if resume_thread_id else f"{prompt}\n\n{continuation}"
                self.store.add_event(
                    job_id,
                    "warning",
                    f"Escalating to {next_profile.model}/{next_profile.reasoning_effort}; "
                    f"resume_thread={resume_thread_id or 'unavailable'}",
                )
                attempt_no += 1
                continue

            if (
                normalize_backend(job.backend) == CODEX_BACKEND
                and result.result_status == "partial"
                and attempt_no < attempts
            ):
                if result.metrics is not None and result.metrics.thread_id:
                    resume_thread_id = result.metrics.thread_id
                continuation = (
                    "Continue the same assigned task from the existing workspace and progress "
                    "state. The prior attempt wrote Status: partial. Do not repeat completed "
                    "discovery or revert useful changes. Finish the remaining acceptance "
                    "criteria, run the required checks, commit when requested, and overwrite "
                    "result.md with the final Status marker. A soft tool-call or changed-line "
                    "checkpoint is not a blocker while scoped progress remains possible."
                )
                attempt_prompt = continuation if resume_thread_id else f"{prompt}\n\n{continuation}"
                self.store.add_event(
                    job_id,
                    "warning",
                    "Continuing partial Codex result with the same model; "
                    f"resume_thread={resume_thread_id or 'unavailable'}",
                )
                attempt_no += 1
                continue

            if result.completed:
                final_status = result.result_status or "completed"
                return self._finish_job(job_id, final_status, result.message)

            if result.status == "cancelled":
                self._write_blocked_result_if_missing(job, result.message)
                return self._finish_job(job_id, "cancelled", result.message)

            if result.status == "blocked":
                self._write_blocked_result_if_missing(job, result.message)
                self.store.add_event(job_id, "error", result.message)
                return self._finish_job(job_id, "blocked", result.message)

            if result.status == "exited_without_result":
                diagnostic_message = self._missing_result_message(job, result, log_path)
                quota_recovery_message = None
                if job.backend == AGY_BACKEND:
                    quota_recovery_message = self._auto_switch_agy_after_quota_failure(
                        job,
                        log_path,
                        diagnostic_message,
                        already_used=quota_recovery_used,
                    )
                if quota_recovery_message is not None:
                    quota_recovery_used = True
                    attempts += 1
                    self.store.add_event(job_id, "warning", quota_recovery_message)
                    self.store.add_event(
                        job_id,
                        "warning",
                        "Retrying after agy account auto-switch",
                    )
                    last_result_message = quota_recovery_message
                    attempt_no += 1
                    continue
                self._write_blocked_result_if_missing(job, diagnostic_message)
                self.store.add_event(job_id, "error", diagnostic_message)
                last_result_message = diagnostic_message

            dirty_message = self._preserve_dirty_state_if_needed(job, prefix="dirty-after-failure")
            if dirty_message and not job.allow_dirty:
                self._write_blocked_result_if_missing(job, dirty_message)
                self.store.add_event(job_id, "error", dirty_message)
                return self._finish_job(job_id, "stopped_dirty_after_failure", dirty_message)

            if attempt_no < attempts:
                self.store.add_event(job_id, "warning", "Restarting after failed attempt")
            attempt_no += 1

        self._write_blocked_result_if_missing(job, last_result_message)
        return self._finish_job(job_id, "failed", last_result_message)

    def _model_ladder_for_job(self, job: JobRecord) -> tuple[ModelProfile, ...]:
        model = job.codex_model or self.config.defaults.codex_model
        effort = job.codex_reasoning_effort or self.config.defaults.codex_reasoning_effort
        if normalize_backend(job.backend) != CODEX_BACKEND or job.codex_quality_tier is None:
            return self.model_routing.ladder_for_explicit_model(model, effort)
        return self.model_routing.ladder_for_tier(job.codex_quality_tier)

    def _wait_for_codex_quota(self, job: JobRecord) -> bool:
        broker = self.quota_broker
        if broker is None:
            return True
        last_reason: str | None = None
        while not self.store.cancel_requested(job.job_id):
            decision = broker.try_acquire(job.job_id, worker_pid=os.getpid())
            if decision.acquired:
                self.store.update_job(job.job_id, status="running")
                self.store.add_event(
                    job.job_id,
                    "info",
                    f"Global Codex quota acquired; active_jobs={decision.active_jobs}",
                )
                return True
            self.store.update_job(job.job_id, status="waiting_quota")
            if decision.reason != last_reason:
                self.store.add_event(job.job_id, "warning", self._quota_wait_message(decision))
                last_reason = decision.reason
            sleep_for = self.config.defaults.codex_quota_poll_sec
            if decision.retry_after_sec is not None and decision.retry_after_sec > 0:
                sleep_for = min(sleep_for, decision.retry_after_sec)
            time.sleep(sleep_for)
        return False

    @staticmethod
    def _quota_wait_message(decision: QuotaDecision) -> str:
        detail = f"active_jobs={decision.active_jobs}"
        if decision.retry_after_sec is not None:
            detail += f", retry_after_sec={round(decision.retry_after_sec, 1)}"
        return f"Waiting for global Codex quota: {decision.reason or 'unavailable'} ({detail})"

    def _refresh_stale_worker_if_needed(self, job_id: str) -> JobRecord:
        job = self.store.get_job(job_id)
        if self._is_terminal(job) or job.status not in {"running", "waiting_quota"}:
            return job
        if job.worker_pid is None:
            return job
        if _process_is_alive(job.worker_pid):
            return job

        message = (
            f"Worker process PID {job.worker_pid or '-'} is no longer alive "
            f"while job status is {job.status}; marking job as worker_error."
        )
        if job.runner_pid:
            message += f" Last observed runner PID was {job.runner_pid}."
        if job.agy_pid and job.agy_pid != job.runner_pid:
            message += f" Last observed agy PID was {job.agy_pid}."
        worker_log_path = job.run_dir / "worker.log"
        if worker_log_path.exists():
            message += f" Worker log: {worker_log_path}"

        self.store.add_event(job_id, "error", message)
        self.store.finish_running_attempts(job_id, "worker_lost", message=message)
        return self._finish_job(job_id, "worker_error", message)

    def status_job(self, job_id: str) -> dict[str, Any]:
        job = self._refresh_stale_worker_if_needed(job_id)
        metrics = self.store.attempt_metrics(job_id, limit=1)
        return {
            "job_id": job.job_id,
            "task_id": job.task_id,
            "status": job.status,
            "route": job.route,
            "workspace_path": str(job.workspace_path),
            "expected_branch": job.expected_branch,
            "backend": job.backend,
            "agy_model": job.agy_model,
            "codex_model": job.codex_model,
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "codex_quality_tier": job.codex_quality_tier,
            "codex_tool_call_budget": job.codex_tool_call_budget,
            "worker_pid": job.worker_pid,
            "runner_pid": job.runner_pid,
            "agy_pid": job.agy_pid,
            "log_path": str(job.log_path) if job.log_path else None,
            "result_path": str(job.result_path),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "last_error": job.last_error,
            "cancel_requested": job.cancel_requested,
            "read_only": job.read_only,
            "slot_name": job.slot_name,
            "latest_attempt_metrics": metrics[0] if metrics else None,
            "events": format_events(self.store.recent_events(job_id)),
        }

    def summary_job(self, job_id: str, log_lines: int = 20) -> dict[str, Any]:
        job = self._refresh_stale_worker_if_needed(job_id)
        metrics = self.store.attempt_metrics(job_id, limit=1)
        status = ""
        forbidden: list[str] = []
        try:
            state = workspace_state(job.workspace_path)
            status = state.porcelain
            forbidden = [
                f"{entry.status} {entry.path} [{entry.matched_glob}]"
                for entry in find_forbidden_status_entries(
                    state.porcelain,
                    self.config.defaults.forbidden_status_globs,
                )
            ]
        except GitError as exc:
            status = f"<git status failed: {exc}>"

        result_state = inspect_result(job.result_path, _job_start_timestamp(job))
        return {
            "job_id": job.job_id,
            "task_id": job.task_id,
            "status": job.status,
            "terminal": self._is_terminal(job),
            "last_error": job.last_error,
            "backend": job.backend,
            "agy_model": job.agy_model,
            "codex_model": job.codex_model,
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "codex_quality_tier": job.codex_quality_tier,
            "codex_tool_call_budget": job.codex_tool_call_budget,
            "worker_pid": job.worker_pid,
            "runner_pid": job.runner_pid,
            "agy_pid": job.agy_pid,
            "read_only": job.read_only,
            "slot_name": job.slot_name,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "result_done": result_state.done,
            "result_status": result_state.status,
            "forbidden_changes": forbidden,
            "dirty_status": status,
            "log_tail": self.tail_job(job_id, log_lines) if job.log_path else "",
            "result_path": str(job.result_path),
            "run_dir": str(job.run_dir),
            "latest_attempt_metrics": metrics[0] if metrics else None,
        }

    def analytics(
        self,
        *,
        limit: int = 100,
        model: str | None = None,
        reasoning_effort: str | None = None,
        valid_only: bool = False,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        report = self.store.metrics_report(
            limit=limit,
            model=model,
            reasoning_effort=reasoning_effort,
            valid_only=valid_only,
        )
        report["filters"] = {
            "limit": limit,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "valid_only": valid_only,
        }
        return report

    def tail_job(self, job_id: str, lines: int = 80) -> str:
        job = self.store.get_job(job_id)
        if job.log_path is None:
            return "No log file has been assigned yet."
        if not job.log_path.exists():
            return f"Log file does not exist yet: {job.log_path}"
        return _tail(job.log_path, lines)

    def result_job(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        if not job.result_path.exists():
            return f"Result file does not exist yet: {job.result_path}"
        return job.result_path.read_text(encoding="utf-8", errors="replace")

    def watch_job(
        self,
        job_id: str,
        *,
        poll_interval_sec: float = 30.0,
        timeout_sec: float | None = None,
        log_lines: int = 80,
        include_details: bool = False,
        log_cursor: int | None = None,
        log_byte_limit: int = 2048,
    ) -> dict[str, Any]:
        """Poll a job with a compact payload and optional bounded log delta."""
        if poll_interval_sec < 0:
            raise ValueError("poll_interval_sec must be non-negative")
        if timeout_sec is not None and timeout_sec < 0:
            raise ValueError("timeout_sec must be non-negative")
        if poll_interval_sec == 0 and timeout_sec is None:
            raise ValueError("poll_interval_sec=0 requires a timeout_sec")
        if log_cursor is not None and log_cursor < 0:
            raise ValueError("log_cursor must be non-negative")
        if not 0 < log_byte_limit <= 16_384:
            raise ValueError("log_byte_limit must be in [1, 16384]")

        started = time.monotonic()
        while True:
            summary = (
                self.summary_job(job_id, log_lines)
                if include_details
                else self._compact_watch_snapshot(
                    job_id,
                    log_cursor=log_cursor,
                    log_byte_limit=log_byte_limit,
                )
            )
            if summary["terminal"]:
                summary["timed_out"] = False
                summary["watch_elapsed_sec"] = round(time.monotonic() - started, 3)
                return summary

            elapsed = time.monotonic() - started
            if timeout_sec is not None and elapsed >= timeout_sec:
                summary["timed_out"] = True
                summary["watch_elapsed_sec"] = round(elapsed, 3)
                return summary

            sleep_for = poll_interval_sec
            if timeout_sec is not None:
                sleep_for = min(sleep_for, max(0.0, timeout_sec - elapsed))
            if sleep_for <= 0:
                # One immediate re-check is enough when timeout_sec=0.
                if timeout_sec == 0:
                    summary["timed_out"] = True
                    summary["watch_elapsed_sec"] = round(time.monotonic() - started, 3)
                    return summary
                continue
            time.sleep(sleep_for)

    def _compact_watch_snapshot(
        self,
        job_id: str,
        *,
        log_cursor: int | None,
        log_byte_limit: int,
    ) -> dict[str, Any]:
        job = self._refresh_stale_worker_if_needed(job_id)
        result_state = inspect_result(job.result_path, _job_start_timestamp(job))
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "task_id": job.task_id,
            "status": job.status,
            "terminal": self._is_terminal(job),
            "last_error": job.last_error,
            "backend": job.backend,
            "agy_model": job.agy_model,
            "codex_model": job.codex_model,
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "codex_quality_tier": job.codex_quality_tier,
            "codex_tool_call_budget": job.codex_tool_call_budget,
            "worker_pid": job.worker_pid,
            "runner_pid": job.runner_pid,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "result_done": result_state.done,
            "result_status": result_state.status,
            "result_path": str(job.result_path),
        }
        if log_cursor is not None:
            delta, next_cursor, truncated = _read_log_delta(
                job.log_path,
                cursor=log_cursor,
                byte_limit=log_byte_limit,
            )
            payload.update(
                {
                    "log_delta": delta,
                    "next_log_cursor": next_cursor,
                    "log_delta_truncated": truncated,
                }
            )
        return payload

    def cancel_job(self, job_id: str) -> JobRecord:
        self.store.add_event(job_id, "warning", "Cancel requested")
        return self.store.request_cancel(job_id)

    def finish_job(self, job_id: str, status: str, last_error: str | None = None) -> JobRecord:
        return self._finish_job(job_id, status, last_error)

    def _run_dir_for_job(self, job_id: str) -> Path:
        if self.config.defaults.runs_layout == "flat":
            return self.config.runs_root / job_id
        return self.config.runs_root / _date_bucket_from_timestamp(time.time()) / job_id

    def archive_jobs(
        self,
        *,
        older_than_days: int = 14,
        limit: int = 50,
        apply: bool = False,
    ) -> list[dict[str, Any]]:
        if older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")

        cutoff = time.time() - older_than_days * 24 * 60 * 60
        decisions: list[dict[str, Any]] = []
        for job in self.store.list_jobs(limit):
            decision = self._archive_decision(job, cutoff, apply=apply)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def sync_slots(self) -> list[dict[str, Any]]:
        return [status.as_dict() for status in self.slots.sync_configured_slots()]

    def manager_accounts(self) -> dict[str, Any]:
        return (
            AntigravityManagerAdapter(
                electron_command=self.config.defaults.auto_switch_agy_electron_command,
            )
            .load_state()
            .as_dict()
        )

    def switch_agy_account(
        self,
        *,
        account_id: str | None = None,
        email: str | None = None,
        strategy: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        result = AntigravityManagerAdapter(
            electron_command=self.config.defaults.auto_switch_agy_electron_command,
        ).switch_agy(
            account_id=account_id,
            email=email,
            strategy=strategy or self.config.defaults.auto_switch_agy_strategy,
            dry_run=dry_run,
        )
        return result.as_dict()

    def list_slots(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        return [
            status.as_dict() for status in self.slots.list_slots(include_deleted=include_deleted)
        ]

    def create_slot(
        self,
        name: str,
        *,
        route: str | None = None,
        branch: str | None = None,
        start_point: str | None = None,
    ) -> dict[str, Any]:
        return self.slots.create_slot(
            name,
            route=route,
            branch=branch,
            start_point=start_point,
        ).as_dict()

    def bootstrap_slot(
        self,
        name: str,
        *,
        route: str,
        repo_path: Path | None = None,
        required_branch: str | None = None,
        slot_path: Path | None = None,
        branch: str | None = None,
        start_point: str | None = None,
        create: bool = True,
        ensure_ide: bool = True,
        remove_slot_modules: bool = True,
    ) -> dict[str, Any]:
        config_result = bootstrap_slot_config(
            self.config,
            slot_name=name,
            route_name=route,
            repo_path=repo_path,
            required_branch=required_branch,
            slot_path=slot_path,
        )
        refreshed = AgentControlPlane(load_config(self.config.config_path))
        payload: dict[str, Any] = {"config": config_result.as_dict()}
        if create:
            payload["slot"] = refreshed.create_slot(
                name,
                route=route,
                branch=branch,
                start_point=start_point,
            )
        if ensure_ide:
            payload["ide"] = refreshed.ensure_slot_root_ide_module(
                remove_slot_modules=remove_slot_modules
            )
        return payload

    def delete_slot(self, name: str, *, force: bool = False) -> dict[str, Any]:
        return self.slots.delete_slot(name, force=force).as_dict()

    def checkout_slot(
        self,
        name: str,
        *,
        branch: str,
        start_point: str | None = None,
    ) -> dict[str, Any]:
        return self.slots.checkout_slot(name, branch=branch, start_point=start_point).as_dict()

    def ensure_slot_ide_module(self, name: str) -> dict[str, object]:
        return self.slots.ensure_ide_module(name)

    def ensure_slot_root_ide_module(
        self,
        *,
        remove_slot_modules: bool = False,
    ) -> dict[str, object]:
        return self.slots.ensure_ide_root_module(remove_configured_slot_modules=remove_slot_modules)

    def unload_slot_root_ide_module(self) -> dict[str, object]:
        return self.slots.unload_ide_root_module()

    def unload_slot_ide_module(self, name: str) -> dict[str, object]:
        return self.slots.unload_ide_module(name)

    def remove_slot_ide_module(self, name: str) -> dict[str, object]:
        return self.slots.remove_ide_module(name)

    def prepare_slot(self, name: str) -> list[dict[str, Any]]:
        return self.slots.prepare_slot(name)

    def cleanup_slots(
        self,
        *,
        max_per_route: int,
        apply: bool = False,
        force: bool = False,
    ) -> list[dict[str, str]]:
        return [
            decision.as_dict()
            for decision in self.slots.cleanup(
                max_per_route=max_per_route,
                apply=apply,
                force=force,
            )
        ]

    def _archive_decision(
        self,
        job: JobRecord,
        cutoff: float,
        *,
        apply: bool,
    ) -> dict[str, Any] | None:
        if not self._is_terminal(job) or job.archived_at is not None:
            return None
        archived_from_timestamp = _job_archive_timestamp(job)
        if archived_from_timestamp > cutoff:
            return None

        archive_dir = (
            self.config.runs_root
            / "_archive"
            / _date_bucket_from_timestamp(archived_from_timestamp)
            / job.job_id
        )
        decision: dict[str, Any] = {
            "job_id": job.job_id,
            "task_id": job.task_id,
            "status": job.status,
            "backend": job.backend,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
            "run_dir": str(job.run_dir),
            "archive_dir": str(archive_dir),
            "apply": apply,
            "action": "would_archive",
        }
        runs_root = self.config.runs_root.resolve(strict=False)
        run_dir = job.run_dir.resolve(strict=False)
        if run_dir == runs_root or not run_dir.is_relative_to(runs_root):
            decision["action"] = "blocked"
            decision["reason"] = (
                f"Run directory is outside configured runs root {runs_root}: {run_dir}"
            )
            return decision
        if not apply:
            return decision
        if archive_dir.exists():
            decision["action"] = "blocked"
            decision["reason"] = f"Archive path already exists: {archive_dir}"
            return decision

        updates: dict[str, Any] = {"archived_at": utc_now()}
        if job.run_dir.exists():
            archive_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(job.run_dir), str(archive_dir))
            except OSError as exc:
                decision["action"] = "failed"
                decision["reason"] = str(exc)
                return decision
            updates["run_dir"] = archive_dir
            prompt_relative = _path_relative_to(job.prompt_path, job.run_dir)
            if prompt_relative is not None:
                updates["prompt_path"] = archive_dir / prompt_relative
            if job.log_path is not None:
                log_relative = _path_relative_to(job.log_path, job.run_dir)
                if log_relative is not None:
                    updates["log_path"] = archive_dir / log_relative
        else:
            decision["warning"] = f"Run directory does not exist: {job.run_dir}"

        archived = self.store.update_job(job.job_id, **updates)
        decision["action"] = "archived"
        decision["run_dir"] = str(archived.run_dir)
        decision["archived_at"] = archived.archived_at
        return decision

    def _launch_worker(self, job_id: str) -> int:
        job = self.store.get_job(job_id)
        worker_log_path = job.run_dir / "worker.log"
        worker_log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        src_path = str(self.config.project_root / "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        command = [
            sys.executable,
            "-m",
            "agent_control_plane.app.runtime.cli",
            "run-job",
            "--config",
            str(self.config.config_path),
            "--job-id",
            job_id,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            )
        with worker_log_path.open("ab") as worker_log:
            proc = subprocess.Popen(  # nosec B603
                command,
                cwd=str(self.config.project_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        self.store.add_event(
            job_id,
            "info",
            f"Background worker launched with PID {proc.pid}; worker log: {worker_log_path}",
        )
        return proc.pid

    def _finish_job(self, job_id: str, status: str, last_error: str | None = None) -> JobRecord:
        job = self.store.mark_finished(job_id, status, last_error)
        if self.quota_broker is not None:
            try:
                self.quota_broker.release(job_id)
            except (OSError, sqlite3.Error) as exc:
                self.store.add_event(job_id, "warning", f"Could not release quota lease: {exc}")
        if job.slot_name:
            slot_status, slot_note = self._slot_release_status(job, status)
            self.slots.release_for_job(
                job.slot_name,
                job_id=job.job_id,
                status=slot_status,
                note=slot_note,
            )
        return job

    @staticmethod
    def _slot_release_status(job: JobRecord, job_status: str) -> tuple[str, str | None]:
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return (
                "inspection_failed",
                f"job {job.job_id} finished {job_status}; could not inspect slot: {exc}",
            )
        if not state.porcelain:
            return "available", None

        dirty_preview = _compact_status_preview(state.porcelain)
        if job_status == "stopped_dirty_after_failure":
            return (
                "dirty_after_failure",
                f"job {job.job_id} stopped with dirty workspace: {dirty_preview}",
            )
        return (
            "dirty_after_job",
            f"job {job.job_id} finished {job_status} with dirty workspace: {dirty_preview}",
        )

    @staticmethod
    def _is_terminal(job: JobRecord) -> bool:
        return job.finished_at is not None or job.status in TERMINAL_STATUSES

    def _guardrail_baseline(self, job: JobRecord) -> GuardrailBaseline:
        try:
            state = workspace_state(job.workspace_path)
        except GitError:
            return GuardrailBaseline(entries=(), fingerprints={})
        entries = tuple(
            find_forbidden_status_entries(
                state.porcelain,
                self.config.defaults.forbidden_status_globs,
            )
        )
        try:
            baseline_patch = diff_patch(job.workspace_path) if state.dirty else ""
        except GitError:
            baseline_patch = ""
        return GuardrailBaseline(
            entries=entries,
            fingerprints={
                self._forbidden_entry_key(entry): self._status_path_fingerprint(
                    job.workspace_path,
                    entry.path,
                )
                for entry in entries
            },
            diff_changed_lines=self._diff_changed_line_count(baseline_patch),
        )

    def _route_root_dirty_baseline(
        self,
        job: JobRecord,
        route_config: Any,
    ) -> WorkspaceDirtyBaseline | None:
        if not job.slot_name or route_config is None:
            return None
        if not getattr(route_config, "monitor_route_root", True):
            return None
        route_root = route_config.path.resolve(strict=False)
        if route_root == job.workspace_path.resolve(strict=False):
            return None
        try:
            state = workspace_state(route_root)
        except GitError:
            return WorkspaceDirtyBaseline(path=route_root, fingerprints={})
        return WorkspaceDirtyBaseline(
            path=route_root,
            fingerprints={
                path: self._status_path_fingerprint(route_root, path)
                for path in _status_paths(state.porcelain)
            },
        )

    def _route_root_guardrail_message(
        self,
        job: JobRecord,
        baseline: WorkspaceDirtyBaseline | None,
    ) -> str | None:
        if baseline is None:
            return None
        try:
            state = workspace_state(baseline.path)
        except GitError as exc:
            return f"Route root guardrail could not inspect git status: {exc}"

        changed: list[str] = []
        for path in _status_paths(state.porcelain):
            fingerprint = self._status_path_fingerprint(baseline.path, path)
            previous = baseline.fingerprints.get(path)
            if previous is None or previous != fingerprint:
                changed.append(path)

        if not changed:
            return None

        status_path = job.run_dir / "route-root-guardrail-status.txt"
        status_path.write_text(state.porcelain, encoding="utf-8")
        try:
            patch = diff_patch(baseline.path)
        except GitError as exc:
            patch = f"Could not capture route root git diff: {exc}\n"
        (job.run_dir / "route-root-guardrail.patch").write_text(patch, encoding="utf-8")

        preview = "; ".join(changed[:8])
        if len(changed) > 8:
            preview += f"; ... ({len(changed) - 8} more)"
        return (
            "Slot job modified route root outside assigned workspace. "
            f"Assigned workspace: {job.workspace_path}; route root: {baseline.path}; "
            f"changed route-root paths: {preview}. "
            f"Preserved status in {status_path}"
        )

    def _guardrail_violation_message(
        self,
        job: JobRecord,
        baseline: GuardrailBaseline,
    ) -> str | None:
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return f"Guardrail could not inspect git status: {exc}"
        if job.read_only and state.dirty:
            return f"Read-only job modified workspace: {state.porcelain}"
        current_entries = find_forbidden_status_entries(
            state.porcelain,
            self.config.defaults.forbidden_status_globs,
        )
        entries = find_new_forbidden_status_entries(
            state.porcelain,
            self.config.defaults.forbidden_status_globs,
            list(baseline.entries),
        )
        entries.extend(self._changed_baseline_forbidden_entries(job, baseline, current_entries))
        entries = self._dedupe_forbidden_entries(entries)
        if not entries:
            return None
        details = "; ".join(
            f"{entry.status} {entry.path} matched {entry.matched_glob}" for entry in entries
        )
        return f"Forbidden workspace change detected: {details}"

    def _codex_dirty_diff_guardrail_message(
        self,
        job: JobRecord,
        baseline: GuardrailBaseline,
    ) -> str | None:
        """Stops Codex when it expands a dirty diff without a valid result."""
        if normalize_backend(job.backend) != CODEX_BACKEND:
            return None

        result_state = inspect_result(job.result_path, 0.0)
        if result_state.done:
            return None

        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return f"Codex dirty diff guardrail could not inspect git status: {exc}"
        if not state.dirty:
            return None

        try:
            patch = diff_patch(job.workspace_path)
        except GitError as exc:
            return f"Codex dirty diff guardrail could not inspect git diff: {exc}"

        changed_lines = self._diff_changed_line_count(patch)
        growth = max(0, changed_lines - baseline.diff_changed_lines)
        if growth <= CODEX_DIRTY_DIFF_MAX_CHANGED_LINES:
            return None

        return (
            "Codex dirty diff exceeded "
            f"{CODEX_DIRTY_DIFF_MAX_CHANGED_LINES} changed-line growth "
            f"without a valid result (baseline {baseline.diff_changed_lines}, "
            f"current {changed_lines}, growth {growth}). "
            f"Dirty status: {_compact_status_preview(state.porcelain)}"
        )

    @staticmethod
    def _diff_changed_line_count(patch: str) -> int:
        return sum(
            1
            for line in patch.splitlines()
            if (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
        )

    def _changed_baseline_forbidden_entries(
        self,
        job: JobRecord,
        baseline: GuardrailBaseline,
        current_entries: list[ForbiddenStatusEntry],
    ) -> list[ForbiddenStatusEntry]:
        changed: list[ForbiddenStatusEntry] = []
        for entry in current_entries:
            key = self._forbidden_entry_key(entry)
            baseline_fingerprint = baseline.fingerprints.get(key)
            if baseline_fingerprint is None:
                continue
            current_fingerprint = self._status_path_fingerprint(job.workspace_path, entry.path)
            if current_fingerprint != baseline_fingerprint:
                changed.append(entry)
        return changed

    @classmethod
    def _dedupe_forbidden_entries(
        cls,
        entries: list[ForbiddenStatusEntry],
    ) -> list[ForbiddenStatusEntry]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[ForbiddenStatusEntry] = []
        for entry in entries:
            key = cls._forbidden_entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return deduped

    @staticmethod
    def _forbidden_entry_key(entry: ForbiddenStatusEntry) -> tuple[str, str, str]:
        return (
            entry.status,
            _normalize_status_path(entry.path),
            _normalize_status_path(entry.matched_glob),
        )

    @staticmethod
    def _status_path_fingerprint(workspace_path: Path, status_path: str) -> str:
        path = workspace_path / Path(status_path)
        try:
            if not path.exists():
                return "missing"
            if path.is_dir():
                return "directory"
            if not path.is_file():
                return "other"
            digest = hashlib.sha256()
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
            return f"file:{digest.hexdigest()}"
        except OSError as exc:
            return f"error:{type(exc).__name__}:{exc}"

    def _preserve_dirty_state_if_needed(self, job: JobRecord, *, prefix: str) -> str | None:
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return f"Could not inspect workspace after failure: {exc}"

        if not state.dirty:
            return None

        dirty_status = job.run_dir / f"{prefix}-status.txt"
        dirty_status.write_text(state.porcelain, encoding="utf-8")
        try:
            patch = diff_patch(job.workspace_path)
        except GitError as exc:
            patch = f"Could not capture git diff: {exc}\n"
        (job.run_dir / f"{prefix}.patch").write_text(patch, encoding="utf-8")
        return f"Workspace is dirty. Preserved status in {dirty_status}"

    @staticmethod
    def _missing_result_message(job: JobRecord, result: object, log_path: Path) -> str:
        status = getattr(result, "status", "unknown")
        message = getattr(result, "message", "no runner message")
        exit_code = getattr(result, "exit_code", None)
        return (
            f"{job.backend} exited without writing a valid result file. "
            f"job_id={job.job_id}; task_id={job.task_id}; runner_status={status}; "
            f"exit_code={exit_code}; reason={message}; log_path={log_path}"
        )

    def _auto_switch_agy_after_quota_failure(
        self,
        job: JobRecord,
        log_path: Path,
        diagnostic_message: str,
        *,
        already_used: bool,
    ) -> str | None:
        if already_used or not self.config.defaults.auto_switch_agy_on_quota:
            return None
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
        if not is_agy_quota_failure(f"{diagnostic_message}\n{log_text}"):
            return None
        try:
            result = AntigravityManagerAdapter(
                electron_command=self.config.defaults.auto_switch_agy_electron_command,
            ).switch_agy(
                strategy=self.config.defaults.auto_switch_agy_strategy,
                dry_run=False,
                avoid_current=True,
            )
        except AntigravityManagerError as exc:
            self.store.add_event(
                job.job_id,
                "error",
                f"agy quota auto-switch failed: {exc}",
            )
            return None
        return (
            "agy quota failure detected; switched Antigravity Manager agy account "
            f"from {result.previous_email or result.previous_account_id or '<none>'} "
            f"to {result.email} using strategy {result.strategy}"
        )

    @staticmethod
    def _write_blocked_result_if_missing(job: JobRecord, message: str) -> None:
        if job.result_path.exists():
            text = job.result_path.read_text(encoding="utf-8", errors="replace")
            is_placeholder = (
                "Awaiting `agy`" in text
                or "Awaiting execution" in text
                or "Awaiting agent execution" in text
            )
            is_placeholder = is_placeholder or (
                "Not reviewed yet" in text and "Status: blocked" in text
            )
            if not is_placeholder:
                return

        changed_files, artifact_note = AgentControlPlane._dirty_result_context(job)
        risk_message = message if not artifact_note else f"{message}; {artifact_note}"
        next_action = "inspect the attempt log and rerun after fixing the blocker"
        if artifact_note:
            next_action = (
                "inspect the preserved dirty status/patch and rerun after fixing the blocker"
            )

        job.result_path.parent.mkdir(parents=True, exist_ok=True)
        job.result_path.write_text(
            "Status: blocked\n\n"
            f"Changed files: {changed_files}\n\n"
            "What changed: nothing landed into the target branch\n\n"
            "Verification performed: none\n\n"
            f"Not verified / remaining risks: {risk_message}\n\n"
            f"Next action: {next_action}.\n",
            encoding="utf-8",
        )

    @staticmethod
    def _dirty_result_context(job: JobRecord) -> tuple[str, str]:
        status_files = sorted(
            job.run_dir.glob("*-status.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not status_files:
            return "none", ""

        status_file = status_files[0]
        status_text = status_file.read_text(encoding="utf-8", errors="replace")
        prefix = status_file.name.removesuffix("-status.txt")
        patch_file = job.run_dir / f"{prefix}.patch"
        artifact_note = f"preserved dirty status: {status_file}"
        if patch_file.exists():
            artifact_note = f"{artifact_note}; patch: {patch_file}"
        return _compact_status_preview(status_text), artifact_note


T = TypeVar("T")


def _normalize_status_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _option(value: T | None, default: T) -> T:
    return default if value is None else value


def _backend_option(*values: str | None) -> str:
    for value in values:
        if not value:
            continue
        backend = normalize_backend(value)
        if backend not in SUPPORTED_BACKENDS:
            allowed = ", ".join(SUPPORTED_BACKENDS)
            raise PolicyError(f"Unsupported backend {value!r}. Expected one of: {allowed}")
        return backend
    raise PolicyError("No backend configured")


def _tool_call_budget_for_tier(config: ControlConfig, tier: str) -> int:
    budgets = {
        "mechanical": config.defaults.codex_mechanical_tool_call_budget,
        "balanced": config.defaults.codex_balanced_tool_call_budget,
        "deep": config.defaults.codex_deep_tool_call_budget,
    }
    try:
        return budgets[tier]
    except KeyError as exc:
        raise PolicyError(f"Unsupported Codex quality tier for tool budget: {tier}") from exc


def _date_bucket_from_timestamp(timestamp: float) -> Path:
    moment = datetime.fromtimestamp(timestamp, UTC)
    return Path(f"{moment:%Y}") / f"{moment:%m}" / f"{moment:%d}"


def _job_start_timestamp(job: JobRecord) -> float:
    timestamp = job.started_at or job.created_at
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return 0.0


def _job_archive_timestamp(job: JobRecord) -> float:
    timestamp = job.finished_at or job.updated_at or job.created_at
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return time.time()


def _path_relative_to(path: Path, parent: Path) -> Path | None:
    try:
        return path.relative_to(parent)
    except ValueError:
        return None


def _read_log_delta(
    path: Path | None,
    *,
    cursor: int,
    byte_limit: int,
) -> tuple[str, int, bool]:
    if path is None or not path.exists():
        return "", cursor, False
    size = path.stat().st_size
    safe_cursor = min(cursor, size)
    with path.open("rb") as handle:
        handle.seek(safe_cursor)
        data = handle.read(byte_limit)
    next_cursor = safe_cursor + len(data)
    return data.decode("utf-8", errors="replace"), next_cursor, next_cursor < size


def _tail(path: Path, lines: int) -> str:
    if lines <= 0:
        raise ValueError("lines must be positive")
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])
