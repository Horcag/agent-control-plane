from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from agent_control_plane.entities.job import JobRecord, JobStore, new_job_id
from agent_control_plane.entities.plan import PlanStore
from agent_control_plane.entities.workspace import StartRequest, WorkspacePolicy
from agent_control_plane.features.agent_runner.lib.claude_model_catalog import (
    claude_ladder_for_explicit_model,
)
from agent_control_plane.features.agent_runner.lib.model_catalog import ModelCatalog
from agent_control_plane.features.agent_runner.lib.model_routing import (
    ModelRoutingPolicy,
    RoutingDecision,
    parse_routing_history_records,
)
from agent_control_plane.features.agent_runner.lib.prompt_builder import build_task_prompt
from agent_control_plane.features.agent_runner.lib.runner import (
    AGY_BACKEND,
    CLAUDE_BACKEND,
    CODEX_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.config import ControlConfig
from agent_control_plane.shared.native_quality import (
    resolve_native_quality_contract,
    write_native_quality_contract,
)


class JobLaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobLaunchOptions:
    task_id: str
    route: str
    backend: str | None = None
    agy_model: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    claude_model: str | None = None
    claude_reasoning_effort: str | None = None
    codex_quality_tier: str | None = None
    codex_premium_override_reason: str | None = None
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
    plan_id: str | None = None
    plan_task_id: str | None = None
    plan_dispatch_token: str | None = None
    workspace_access: str | None = None


class SlotStatusView(Protocol):
    @property
    def route(self) -> str: ...

    @property
    def path(self) -> Path: ...

    @property
    def branch(self) -> str | None: ...


class SlotLifecycleGateway(Protocol):
    def inspect_slot(self, name: str, *, scope: str | None = None) -> SlotStatusView: ...

    def acquire_for_job(
        self,
        name: str,
        *,
        job_id: str,
        route: str,
        allow_dirty: bool,
    ) -> Any: ...

    def release_for_job(
        self,
        name: str,
        *,
        job_id: str,
        status: str = "available",
        note: str | None = None,
    ) -> Any: ...

    def ensure_ide_root_module(self) -> Any: ...

    def prepare_slot(self, name: str) -> Any: ...


class JobLauncher:
    """Validate one launch request, persist it, acquire its slot, and spawn its worker."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        store: JobStore,
        plan_store: PlanStore,
        slots: SlotLifecycleGateway,
        policy: WorkspacePolicy,
        model_routing: ModelRoutingPolicy,
        claude_catalog: ModelCatalog,
        reconcile_jobs: Callable[[], object],
        finish_job: Callable[[str, str, str | None], JobRecord],
        launch_worker: Callable[[str, str], int],
        run_dir_for_job: Callable[[str], Path],
        slot_error_type: type[Exception],
    ) -> None:
        self.config = config
        self.store = store
        self.plan_store = plan_store
        self.slots = slots
        self.policy = policy
        self.model_routing = model_routing
        self.claude_catalog = claude_catalog
        self.reconcile_jobs = reconcile_jobs
        self.finish_job = finish_job
        self.launch_worker = launch_worker
        self.run_dir_for_job = run_dir_for_job
        self.slot_error_type = slot_error_type

    def start(self, options: JobLaunchOptions) -> JobRecord:
        override_reason = (
            options.codex_premium_override_reason.strip()
            if options.codex_premium_override_reason is not None
            else None
        )
        if options.plan_task_id and not options.plan_id:
            raise JobLaunchError("plan_task_id requires plan_id")
        if options.plan_dispatch_token and not options.plan_id:
            raise JobLaunchError("plan_dispatch_token requires plan_id")
        plan_task_id = options.plan_task_id or options.task_id
        if options.plan_id:
            try:
                if options.plan_dispatch_token:
                    self.plan_store.assert_dispatch_claim(
                        options.plan_id,
                        plan_task_id,
                        dispatch_token=options.plan_dispatch_token,
                        dispatch_task_id=options.task_id,
                    )
                else:
                    self.plan_store.assert_task_can_start(options.plan_id, plan_task_id)
            except (KeyError, ValueError) as exc:
                raise JobLaunchError(str(exc)) from exc

        allow_dirty = _option(options.allow_dirty, self.config.defaults.allow_dirty)
        workspace_path = options.workspace_path
        expected_branch = options.expected_branch
        if options.slot:
            self.reconcile_jobs()
            try:
                slot_status = self.slots.inspect_slot(options.slot)
            except self.slot_error_type as exc:
                raise JobLaunchError(str(exc)) from exc
            if slot_status.route != options.route:
                raise JobLaunchError(
                    f"Slot {options.slot} belongs to route {slot_status.route!r}, "
                    f"not {options.route!r}"
                )
            if workspace_path and workspace_path.resolve(strict=False) != slot_status.path.resolve(
                strict=False
            ):
                raise JobLaunchError(
                    f"Slot {options.slot} resolves to {slot_status.path}, "
                    f"but workspace_path was {workspace_path}"
                )
            workspace_path = slot_status.path
            if expected_branch is None:
                if slot_status.branch is None:
                    raise JobLaunchError(
                        f"Slot {options.slot} has no current branch; pass --expected-branch"
                    )
                expected_branch = slot_status.branch

        check = self.policy.check_start(
            StartRequest(
                task_id=options.task_id,
                route=options.route,
                workspace_path=workspace_path,
                expected_branch=expected_branch,
                allow_dirty=allow_dirty,
            )
        )
        if not check.ok:
            raise JobLaunchError("\n".join(check.reasons))
        if check.workspace_path is None or check.expected_branch is None:
            raise JobLaunchError("Policy check did not resolve workspace path or expected branch")

        route_config = self.config.routes[options.route]
        requested_workspace_access = (
            options.workspace_access
            if options.workspace_access is not None
            else route_config.workspace_access
        )
        workspace_access = _option(
            requested_workspace_access,
            self.config.defaults.workspace_access,
        )
        if workspace_access not in {"ide_mcp", "native"}:
            raise JobLaunchError(
                f"workspace_access must be exactly 'ide_mcp' or 'native', got {workspace_access!r}"
            )
        quality_contract = resolve_native_quality_contract(
            self.config,
            options.route,
            workspace_access=workspace_access,
            read_only=options.read_only,
        )
        if quality_contract.policy == "controller" and (
            options.slot is None or self.config.defaults.terminal_slot_policy != "checkpoint"
        ):
            raise JobLaunchError(
                "native_quality_policy=controller requires a checkpointed slot "
                "(pass --slot and set terminal_slot_policy='checkpoint')"
            )

        backend = _backend_option(
            options.backend,
            route_config.backend,
            self.config.defaults.backend,
        )
        normalized_backend = normalize_backend(backend)
        if normalized_backend == AGY_BACKEND and workspace_access == "native":
            raise JobLaunchError("workspace_access=native is not supported with the agy backend")
        if normalized_backend == CLAUDE_BACKEND and workspace_access != "native":
            raise JobLaunchError(
                "The claude backend requires workspace_access=native; "
                "ide_mcp workers are Codex/agy only"
            )
        if options.agy_model is not None and normalized_backend != AGY_BACKEND:
            raise JobLaunchError("--agy-model can only be used with the agy backend")
        if options.claude_model is not None and normalized_backend != CLAUDE_BACKEND:
            raise JobLaunchError("--claude-model can only be used with the claude backend")
        if options.claude_reasoning_effort is not None and normalized_backend != CLAUDE_BACKEND:
            raise JobLaunchError(
                "--claude-reasoning-effort can only be used with the claude backend"
            )
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
        if explicit_codex_profile and options.codex_quality_tier is not None:
            raise JobLaunchError(
                "Callers must choose either automatic policy routing or one fixed explicit profile; "
                "codex_quality_tier cannot be combined with codex_model or codex_reasoning_effort"
            )
        if (
            options.codex_premium_override_reason is not None
            and normalized_backend == CODEX_BACKEND
            and not explicit_codex_profile
        ):
            raise JobLaunchError(
                "codex_premium_override_reason requires an explicit Codex model profile"
            )
        if (
            explicit_codex_profile
            and options.codex_premium_override_reason is not None
            and not override_reason
        ):
            raise JobLaunchError(
                "Explicit premium Codex launches require a nonblank codex_premium_override_reason"
            )
        codex_quality_tier: str | None = None
        codex_policy_name: str | None = None
        routing_decision: RoutingDecision | None = None
        explicit_premium_launch = False
        codex_model: str | None = None
        codex_reasoning_effort: str | None = None
        if normalized_backend == CODEX_BACKEND:
            codex_policy_name = (
                options.codex_quality_tier or self.config.defaults.codex_quality_tier
            )
            if not explicit_codex_profile:
                try:
                    configured_policy = self.model_routing.policy(codex_policy_name)
                    routing_decision = self.model_routing.decision_for_policy(
                        configured_policy.name,
                        history=parse_routing_history_records(self.store.routing_history()),
                        route=options.route,
                    )
                    initial_profile = routing_decision.chosen_profile
                except ValueError as exc:
                    raise JobLaunchError(str(exc)) from exc
                codex_quality_tier = configured_policy.name
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
                try:
                    self.model_routing.policy(codex_policy_name)
                    explicit_profile = self.model_routing.ladder_for_explicit_model(
                        codex_model,
                        codex_reasoning_effort,
                    )[0]
                except ValueError as exc:
                    raise JobLaunchError(str(exc)) from exc
                codex_model = explicit_profile.model
                codex_reasoning_effort = explicit_profile.reasoning_effort
                metadata = self.model_routing.catalog.rate_metadata_for(codex_model)
                if metadata is not None and metadata.premium and not override_reason:
                    raise JobLaunchError(
                        "Explicit launch of premium Codex model requires a nonblank "
                        "codex_premium_override_reason"
                    )
                explicit_premium_launch = metadata is not None and metadata.premium

        if normalized_backend == CLAUDE_BACKEND:
            claude_model = _option(
                options.claude_model or route_config.claude_model,
                self.config.defaults.claude_model,
            )
            claude_reasoning_effort = _option(
                options.claude_reasoning_effort or route_config.claude_reasoning_effort,
                self.config.defaults.claude_reasoning_effort,
            )
            try:
                claude_profile = claude_ladder_for_explicit_model(
                    self.claude_catalog,
                    claude_model,
                    claude_reasoning_effort,
                )[0]
            except ValueError as exc:
                raise JobLaunchError(str(exc)) from exc
            # The resolved Claude profile rides the shared profile columns so the
            # existing escalation, metrics, and status plumbing stay schema-stable.
            codex_model = claude_profile.model
            codex_reasoning_effort = claude_profile.reasoning_effort
            metadata = self.claude_catalog.rate_metadata_for(claude_profile.model)
            if metadata is not None and metadata.premium and not override_reason:
                raise JobLaunchError(
                    "Explicit launch of premium Claude model requires a nonblank "
                    "codex_premium_override_reason"
                )
            explicit_premium_launch = metadata is not None and metadata.premium

        codex_tool_call_budget: int | None = None
        if normalized_backend == CLAUDE_BACKEND and options.codex_tool_call_budget is not None:
            codex_tool_call_budget = options.codex_tool_call_budget
            if codex_tool_call_budget <= 0:
                raise JobLaunchError("Claude tool-call budget must be positive")
        if normalized_backend == CODEX_BACKEND:
            if codex_policy_name is None:
                raise JobLaunchError("Codex routing policy was not selected")
            codex_tool_call_budget = (
                options.codex_tool_call_budget
                if options.codex_tool_call_budget is not None
                else (
                    routing_decision.tool_call_budget
                    if routing_decision is not None
                    else self.model_routing.tool_call_budget_for_policy(codex_policy_name)
                )
            )
            if codex_tool_call_budget <= 0:
                raise JobLaunchError("Codex tool-call budget must be positive")

        job_id = new_job_id(options.task_id)
        run_dir = self.run_dir_for_job(job_id)
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
                workspace_access=workspace_access,
                native_quality_contract=quality_contract,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise JobLaunchError(str(exc)) from exc

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
                codex_premium_override_reason=override_reason,
                codex_tool_call_budget=codex_tool_call_budget,
                workspace_access=workspace_access,
                slot_name=options.slot,
            )
        except ValueError as exc:
            raise JobLaunchError(str(exc)) from exc

        if routing_decision is not None:
            try:
                self.store.record_routing_decision(
                    job.job_id,
                    {
                        "event": "routing_decision",
                        "route": options.route,
                        **routing_decision.as_dict(),
                    },
                )
            except Exception as exc:
                message = f"Could not persist routing decision: {exc}"
                self.store.add_event(job.job_id, "error", message)
                self.finish_job(job.job_id, "blocked", message)
                raise JobLaunchError(message) from exc
        if explicit_premium_launch:
            try:
                self.store.record_explicit_premium_launch(
                    job.job_id,
                    {
                        "event": "explicit_premium_launch",
                        "route": options.route,
                        "codex_model": codex_model,
                        "codex_reasoning_effort": codex_reasoning_effort,
                        "codex_premium_override_reason": override_reason,
                    },
                )
            except Exception as exc:
                message = f"Could not persist explicit premium launch audit: {exc}"
                self.store.add_event(job.job_id, "error", message)
                self.finish_job(job.job_id, "blocked", message)
                raise JobLaunchError(message) from exc

        if options.plan_id:
            try:
                if options.plan_dispatch_token:
                    self.plan_store.bind_dispatched_job(
                        options.plan_id,
                        plan_task_id,
                        dispatch_token=options.plan_dispatch_token,
                        job_id=job.job_id,
                    )
                else:
                    self.plan_store.bind_job(options.plan_id, plan_task_id, job.job_id)
            except (KeyError, ValueError) as exc:
                message = f"Could not bind job to plan task {options.plan_id}/{plan_task_id}: {exc}"
                self.store.add_event(job.job_id, "error", message)
                self.finish_job(job.job_id, "blocked", message)
                raise JobLaunchError(message) from exc
            if self.store.cancel_requested(job.job_id):
                message = "Plan was cancelled while the dispatch claim was being materialized"
                self.store.add_event(job.job_id, "warning", message)
                return self.finish_job(job.job_id, "cancelled", message)

        try:
            _initialize_task_artifacts(
                task_id=options.task_id,
                job_id=job_id,
                workspace_path=check.workspace_path,
                expected_branch=check.expected_branch,
                result_path=check.result_path,
                workspace_access=workspace_access,
                read_only=options.read_only,
            )
            run_dir.mkdir(parents=True, exist_ok=False)
            write_native_quality_contract(run_dir, quality_contract)
            prompt_path.write_text(prompt, encoding="utf-8")
        except Exception as exc:
            message = f"Could not initialize job artifacts: {exc}"
            self.store.add_event(job.job_id, "error", message)
            self.finish_job(job.job_id, "blocked", message)
            raise JobLaunchError(message) from exc

        self.store.add_event(job.job_id, "info", "Job created")
        if options.slot:
            try:
                self.slots.acquire_for_job(
                    options.slot,
                    job_id=job.job_id,
                    route=options.route,
                    allow_dirty=allow_dirty,
                )
            except self.slot_error_type as exc:
                self.store.add_event(job.job_id, "error", str(exc))
                return self.finish_job(job.job_id, "blocked", str(exc))

            if not options.read_only:
                try:
                    if workspace_access != "native":
                        self.slots.ensure_ide_root_module()
                    if self.config.defaults.prepare_slots:
                        self.slots.prepare_slot(options.slot)
                except self.slot_error_type as exc:
                    message = f"Could not prepare slot {options.slot}: {exc}"
                    self.slots.release_for_job(options.slot, job_id=job.job_id)
                    self.store.add_event(job.job_id, "error", message)
                    return self.finish_job(job.job_id, "blocked", message)

        worker_instance_id = uuid.uuid4().hex
        try:
            self.store.assign_worker(job.job_id, worker_instance_id)
            worker_pid = self.launch_worker(job.job_id, worker_instance_id)
        except Exception as exc:
            self.finish_job(job.job_id, "worker_error", str(exc))
            raise
        self.store.update_for_worker(
            job.job_id,
            worker_instance_id,
            worker_pid=worker_pid,
            worker_heartbeat_at=utc_now(),
        )
        return self.store.get_job(job.job_id)


T = TypeVar("T")


def _option(value: T | None, default: T) -> T:
    return default if value is None else value


def _backend_option(*values: str | None) -> str:
    for value in values:
        if not value:
            continue
        backend = normalize_backend(value)
        if backend not in SUPPORTED_BACKENDS:
            allowed = ", ".join(SUPPORTED_BACKENDS)
            raise JobLaunchError(f"Unsupported backend {value!r}. Expected one of: {allowed}")
        return backend
    raise JobLaunchError("No backend configured")


def _initialize_task_artifacts(
    *,
    task_id: str,
    job_id: str,
    workspace_path: Path,
    expected_branch: str,
    result_path: Path,
    workspace_access: str = "ide_mcp",
    read_only: bool = False,
) -> None:
    task_dir = result_path.parent
    progress_path = task_dir / "agent-progress.md"
    task_dir.mkdir(parents=True, exist_ok=True)
    if workspace_access == "native" and read_only:
        next_action_rules = (
            "- Agent runner must inspect the workspace with native read-only tools and "
            "return the structured final response for control-plane recovery; it must "
            "not update this progress file.\n"
        )
    elif workspace_access == "native":
        next_action_rules = (
            "- Agent runner must update this file through native shell/file editing "
            "before repository exploration or edits.\n"
        )
    else:
        next_action_rules = (
            "- Agent runner must update this file through the IDEA MCP selected "
            "in the generated prompt before repository exploration or edits.\n"
        )
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
        f"{next_action_rules}"
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
