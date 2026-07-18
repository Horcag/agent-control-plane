from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_control_plane.app.runtime.finalization_service import FinalizationService
from agent_control_plane.app.runtime.job_execution_service import JobExecutionService
from agent_control_plane.app.runtime.job_guardrails import (
    CODEX_DIRTY_DIFF_MAX_CHANGED_LINES,
    JobGuardrails,
)
from agent_control_plane.entities.job import (
    JobRecord,
    JobStore,
    ReviewMetricsStore,
    format_events,
)
from agent_control_plane.entities.plan import (
    PlanDispatchClaim,
    PlanExecutionSpec,
    PlanStore,
    PlanTaskDefinition,
)
from agent_control_plane.entities.review_inbox import (
    ReviewInboxDraft,
    ReviewInboxItem,
    ReviewInboxStore,
)
from agent_control_plane.entities.slot import SlotStore
from agent_control_plane.entities.workspace import WorkspacePolicy, find_forbidden_status_entries
from agent_control_plane.features.agent_runner import (
    AGY_BACKEND,
    CODEX_BACKEND,
    SUPPORTED_BACKENDS,
    AdaptiveRoutingSettings,
    AgentRunner,
    CodexExecRunner,
    CodexRateLimitReader,
    GlobalQuotaBroker,
    JobLauncher,
    JobLaunchError,
    JobLaunchOptions,
    JobReconciler,
    ModelCatalog,
    ModelProfile,
    ModelRoutingPolicy,
    PtyAgyRunner,
    QuotaDomain,
    RoutingPolicy,
    codex_quota_domain,
    inspect_result,
    normalize_backend,
    parse_routing_history_records,
    process_is_alive,
    terminate_verified_process,
)
from agent_control_plane.features.antigravity_accounts import AntigravityManagerAdapter
from agent_control_plane.features.lifecycle_cleanup import ArchiveService, RetentionService
from agent_control_plane.features.plan_supervision import PlanService
from agent_control_plane.features.result_handoff import (
    HandoffAcceptanceService,
    NativeQualityGateRunner,
    scan_codex_subagent_completions,
)
from agent_control_plane.features.slot_lifecycle import (
    SlotError,
    SlotManager,
    bootstrap_slot_config,
)
from agent_control_plane.shared.config import ControlConfig, load_config
from agent_control_plane.shared.git_tools import (
    GitError,
    workspace_state,
)
from agent_control_plane.shared.native_quality import (
    inspect_native_quality_contract,
    resolve_native_quality_contract,
)


class PolicyError(RuntimeError):
    pass


StartOptions = JobLaunchOptions


def _configured_routing_policies(config: ControlConfig) -> tuple[RoutingPolicy, ...] | None:
    if not config.routing_policies:
        return None
    policies: list[RoutingPolicy] = []
    for configured in config.routing_policies:
        adaptive = configured.adaptive
        policies.append(
            RoutingPolicy(
                name=configured.name,
                task_class=configured.task_class,
                tool_call_budget=configured.tool_call_budget,
                candidates=tuple(
                    ModelProfile(candidate.model, candidate.reasoning_effort)
                    for candidate in configured.candidates
                ),
                adaptive=(
                    AdaptiveRoutingSettings(
                        minimum_samples_per_candidate=adaptive.minimum_samples_per_candidate,
                        history_window=adaptive.history_window,
                        quality_floor=adaptive.quality_floor,
                        prior_quality=adaptive.prior_quality,
                        prior_weight=adaptive.prior_weight,
                        allow_missing_price=adaptive.allow_missing_price,
                    )
                    if adaptive is not None
                    else None
                ),
            )
        )
    return tuple(policies)


_ROUTING_HISTORY_LIMIT = 200
COORDINATOR_SCOPE = (
    "ACP controls delegated worker profiles only; the parent/coordinating Codex thread is "
    "external and cannot be selected, downgraded, or escalated by ACP."
)


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


class AgentControlPlane:
    def __init__(self, config: ControlConfig) -> None:
        self.config = config
        self.store = JobStore(config.database_path)
        self.plan_store = PlanStore(config.database_path)
        self.review_inbox = ReviewInboxStore(config.database_path)
        self.review_metrics = ReviewMetricsStore(config.database_path)
        self.slot_store = SlotStore(config.database_path)
        self.slots = SlotManager(config, self.slot_store)
        self.policy = WorkspacePolicy(config)
        defaults = config.defaults
        self.model_catalog = ModelCatalog.from_config(config.model_catalog)
        self.model_routing = ModelRoutingPolicy(
            policies=_configured_routing_policies(config),
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
            mechanical_tool_call_budget=defaults.codex_mechanical_tool_call_budget,
            balanced_tool_call_budget=defaults.codex_balanced_tool_call_budget,
            deep_tool_call_budget=defaults.codex_deep_tool_call_budget,
            catalog=self.model_catalog,
        )
        self.model_routing.validate_configured_candidates()
        self.model_routing.policy(defaults.codex_quality_tier)
        self._quota_broker: GlobalQuotaBroker | None = None
        if defaults.codex_global_quota_database is not None:
            rate_limit_reader = (
                CodexRateLimitReader(
                    defaults.codex_sessions_root,
                    catalog=self.model_catalog,
                ).latest
                if defaults.codex_sessions_root is not None
                else None
            )
            self._quota_broker = GlobalQuotaBroker(
                defaults.codex_global_quota_database,
                max_concurrent_jobs=defaults.codex_global_max_concurrent_jobs,
                max_burst_jobs=defaults.codex_global_max_burst_jobs,
                soft_limit_percent=defaults.codex_five_hour_soft_limit_percent,
                spark_soft_limit_percent=defaults.codex_spark_soft_limit_percent,
                spark_max_concurrent_jobs=defaults.codex_spark_max_concurrent_jobs,
                rate_limit_reader=rate_limit_reader,
                catalog=self.model_catalog,
                quota_domains=tuple(
                    QuotaDomain(
                        name=domain.name,
                        max_concurrent_jobs=domain.max_concurrent_jobs,
                        max_burst_jobs=domain.max_burst_jobs,
                        soft_limit_percent=domain.soft_limit_percent,
                    )
                    for domain in config.model_catalog.quota_domains
                ),
            )
        self.finalization = FinalizationService(
            config=self.config,
            store=self.store,
            slot_store=self.slot_store,
            slots=self.slots,
            review_inbox=self.review_inbox,
            quota_broker=self._quota_broker,
            native_quality_runner=NativeQualityGateRunner(),
            is_terminal=self._is_terminal,
        )
        self.job_guardrails = JobGuardrails(defaults.forbidden_status_globs)
        self.job_execution = JobExecutionService(
            config=self.config,
            store=self.store,
            policy=self.policy,
            model_routing=self.model_routing,
            guardrails=self.job_guardrails,
            finalizer=self.finalization,
            runner_factory=lambda backend: self._runner_for_backend(backend),
            quota_broker=self._quota_broker,
        )
        self.plan_service = PlanService(
            coordination_root=config.coordination_root,
            job_store=self.store,
            plan_store=self.plan_store,
            review_inbox=self.review_inbox,
            launch=self._launch_plan_claim,
            cancel_job=self.cancel_job,
            accept_handoff=self._accept_plan_handoff,
            reconcile_jobs=self.reconcile_jobs,
            process_is_alive=process_is_alive,
            policy_error=PolicyError,
        )

    @property
    def quota_broker(self) -> GlobalQuotaBroker | None:
        return self._quota_broker

    @quota_broker.setter
    def quota_broker(self, broker: GlobalQuotaBroker | None) -> None:
        self._quota_broker = broker
        if hasattr(self, "finalization"):
            self.finalization.quota_broker = broker
        if hasattr(self, "job_execution"):
            self.job_execution.quota_broker = broker

    def _runner_for_backend(self, backend: str) -> AgentRunner:
        if backend == AGY_BACKEND:
            return PtyAgyRunner()
        if normalize_backend(backend) == CODEX_BACKEND:
            return CodexExecRunner(self.model_catalog)
        allowed = ", ".join(SUPPORTED_BACKENDS)
        raise PolicyError(f"Unsupported backend {backend!r}. Expected one of: {allowed}")

    @classmethod
    def from_config_path(
        cls,
        config_path: str | os.PathLike[str] | None = None,
        *,
        config_contents: bytes | None = None,
    ) -> AgentControlPlane:
        return cls(load_config(config_path, config_contents=config_contents))

    def model_catalog_inspection(self) -> dict[str, Any]:
        return self.model_catalog.inspection_payload()

    def model_routing_explain(self, policy: str, route: str) -> dict[str, Any]:
        if self.config.routes.get(route) is None:
            raise PolicyError(f"Unknown route: {route}")
        try:
            configured_policy = self.model_routing.policy(policy)
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc

        raw_history = self.store.routing_history(limit=_ROUTING_HISTORY_LIMIT)
        history_rows = raw_history if isinstance(raw_history, list) else ()
        history = parse_routing_history_records(history_rows)
        decision = self.model_routing.decision_for_policy(
            configured_policy.name,
            history=history,
            route=route,
        )
        payload = decision.as_dict()
        payload.update(
            {
                "route": route,
                "policy": decision.requested_policy,
                "chosen_model": decision.chosen_profile.model,
                "chosen_reasoning_effort": decision.chosen_profile.reasoning_effort,
                "fallback_reason": (
                    "; ".join(decision.excluded_data_reasons)
                    if decision.configured_fallback
                    else None
                ),
                "catalog_source": decision.catalog_source,
                "catalog_version": decision.catalog_version,
                "model_control_scope": COORDINATOR_SCOPE,
            }
        )
        return payload

    def smoke(self) -> dict[str, Any]:
        self.store.initialize()
        self.plan_store.initialize()
        self.review_inbox.initialize()
        catalog_diagnostics = self._catalog_diagnostics()
        smoke_diagnostics = self._smoke_routing_diagnostics(catalog_diagnostics)
        return {
            "status": "failed" if smoke_diagnostics["failures"] else "passed",
            "failures": smoke_diagnostics["failures"],
            "model_control_scope": COORDINATOR_SCOPE,
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
            "workspace_access": self.config.defaults.workspace_access,
            "native_quality_policy": self.config.defaults.native_quality_policy,
            "terminal_slot_policy": self.config.defaults.terminal_slot_policy,
            "codex_tool_call_budgets": {
                policy.name: policy.tool_call_budget
                for policy in (
                    self.model_routing.policy(name) for name in self.model_routing.policy_names
                )
            },
            "codex_quality_profiles": catalog_diagnostics["profiles"],
            "codex_model_catalog": {
                "status": self.model_catalog.cache_status,
                "profile_resolution_errors": catalog_diagnostics["profile_resolution_errors"],
                "models": self.model_catalog.inspection_payload()["models"],
            },
            "codex_routing_invariants": smoke_diagnostics,
            "codex_global_quota": {
                "enabled": self.quota_broker is not None,
                "database": (
                    str(self.config.defaults.codex_global_quota_database)
                    if self.config.defaults.codex_global_quota_database
                    else None
                ),
                "max_concurrent_jobs": self.config.defaults.codex_global_max_concurrent_jobs,
                "max_burst_jobs": self.config.defaults.codex_global_max_burst_jobs,
                "spark_max_concurrent_jobs": self.config.defaults.codex_spark_max_concurrent_jobs,
                "primary_window_soft_limit_percent": (
                    self.config.defaults.codex_five_hour_soft_limit_percent
                ),
                "spark_window_soft_limit_percent": (
                    self.config.defaults.codex_spark_soft_limit_percent
                ),
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
                    "workspace_access": (
                        route.workspace_access or self.config.defaults.workspace_access
                    ),
                    "native_quality_policy": (
                        route.native_quality_policy or self.config.defaults.native_quality_policy
                    ),
                    "native_quality_max_parallel": route.native_quality_max_parallel,
                    "native_quality_gates": [
                        {
                            "name": gate.name,
                            "command": list(gate.command),
                            "working_dir": gate.working_dir.as_posix(),
                            "timeout_sec": gate.timeout_sec,
                            "include_globs": list(gate.include_globs),
                            "run_on": gate.run_on,
                        }
                        for gate in route.native_quality_gates
                    ],
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

    def _catalog_diagnostics(self) -> dict[str, dict[str, Any]]:
        profiles: dict[str, list[dict[str, Any]]] = {}
        profile_resolution_errors: dict[str, str] = {}
        for policy_name in self.model_routing.policy_names:
            try:
                profiles[policy_name] = [
                    {
                        "model": profile.model,
                        "reasoning_effort": profile.reasoning_effort,
                        "premium": (
                            metadata.premium
                            if (metadata := self.model_catalog.rate_metadata_for(profile.model))
                            is not None
                            else None
                        ),
                    }
                    for profile in self.model_routing.ladder_for_policy(policy_name)
                ]
            except ValueError as exc:
                profile_resolution_errors[policy_name] = str(exc)
        return {
            "profiles": profiles,
            "profile_resolution_errors": profile_resolution_errors,
        }

    def _smoke_routing_diagnostics(
        self, catalog_diagnostics: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        default_policy = self.config.defaults.codex_quality_tier
        effective_default: dict[str, str] | None = None
        try:
            ladder = self.model_routing.ladder_for_policy(default_policy)
            if ladder:
                initial = ladder[0]
                effective_default = {
                    "model": initial.model,
                    "reasoning_effort": initial.reasoning_effort,
                }
        except ValueError:
            ladder = ()
        advertised = {
            "model": self.config.defaults.codex_model,
            "reasoning_effort": self.config.defaults.codex_reasoning_effort,
        }
        if (
            effective_default is not None
            and advertised["model"].strip().lower() != "default"
            and advertised != effective_default
        ):
            failures.append(
                {
                    "code": "advertised_default_mismatch",
                    "message": "Advertised explicit Codex default differs from the configured default policy initial profile.",
                    "advertised_default": advertised,
                    "effective_default_policy": default_policy,
                    "effective_initial_profile": effective_default,
                    "config_keys": [
                        "control.defaults.codex_model",
                        "control.defaults.codex_quality_tier",
                    ],
                }
            )
        premium_initials: dict[str, bool | None] = {}
        initial_models: dict[str, str] = {}
        if not catalog_diagnostics["profile_resolution_errors"]:
            for policy_name in self.model_routing.policy_names:
                policy_ladder = self.model_routing.ladder_for_policy(policy_name)
                if policy_ladder:
                    metadata = self.model_catalog.rate_metadata_for(policy_ladder[0].model)
                    initial_models[policy_name] = policy_ladder[0].model
                    premium_initials[policy_name] = metadata.premium if metadata else None
            if (
                premium_initials
                and len(set(initial_models.values())) == 1
                and all(value is True for value in premium_initials.values())
            ):
                failures.append(
                    {
                        "code": "all_policy_initial_profiles_premium",
                        "message": "Every configured policy starts on a premium catalog model; configure an intentional cheap-first policy.",
                        "premium_initial_profiles": premium_initials,
                        "initial_models": initial_models,
                        "config_keys": [
                            "control.model_routing.policies",
                            "control.model_catalog.models",
                        ],
                    }
                )
        return {
            "advertised_default": advertised,
            "effective_default_policy": default_policy,
            "effective_default_initial_profile": effective_default,
            "premium_initial_profiles": premium_initials,
            "initial_models": initial_models,
            "coordinator_scope": COORDINATOR_SCOPE,
            "failures": failures,
        }

    def reconcile_jobs(
        self,
        job_id: str | None = None,
        *,
        terminate_verified_runners: bool = False,
    ) -> dict[str, Any]:
        reconciler = JobReconciler(
            store=self.store,
            slot_store=self.slot_store,
            is_terminal=self._is_terminal,
            finalize=self._replay_finalization,
            write_orphan_result=self.job_execution.write_blocked_result_if_missing,
            process_is_alive=process_is_alive,
            terminate_verified_process=terminate_verified_process,
        )
        return reconciler.reconcile(
            job_id,
            terminate_verified_runners=terminate_verified_runners,
        )

    def create_plan(
        self,
        *,
        plan_id: str,
        title: str,
        objective: str = "",
        tasks: tuple[PlanTaskDefinition, ...] = (),
    ) -> dict[str, Any]:
        return self.plan_service.create_plan(
            plan_id=plan_id, title=title, objective=objective, tasks=tasks
        )

    def add_plan_task(
        self,
        plan_id: str,
        *,
        task_id: str,
        title: str,
        depends_on: tuple[str, ...] = (),
        execution: PlanExecutionSpec | None = None,
    ) -> dict[str, Any]:
        return self.plan_service.add_plan_task(
            plan_id,
            task_id=task_id,
            title=title,
            depends_on=depends_on,
            execution=execution,
        )

    def bind_plan_job(self, plan_id: str, task_id: str, job_id: str) -> dict[str, Any]:
        return self.plan_service.bind_plan_job(plan_id, task_id, job_id)

    def accept_plan_task(
        self,
        plan_id: str,
        task_id: str,
        *,
        accepted_sha: str | None = None,
    ) -> dict[str, Any]:
        return self.plan_service.accept_plan_task(plan_id, task_id, accepted_sha=accepted_sha)

    def accept_handoff(
        self,
        plan_id: str,
        task_id: str,
        *,
        review_span_id: str,
        accepted_sha: str | None = None,
        attempt_no: int | None = None,
        defects_found: int = 0,
        false_positives: int = 0,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return self.plan_service.accept_handoff(
            plan_id,
            task_id,
            review_span_id=review_span_id,
            accepted_sha=accepted_sha,
            attempt_no=attempt_no,
            defects_found=defects_found,
            false_positives=false_positives,
            notes=notes,
        )

    def reject_plan_task(self, plan_id: str, task_id: str) -> dict[str, Any]:
        return self.plan_service.reject_plan_task(plan_id, task_id)

    def dispatch_plan(self, plan_id: str, *, max_jobs: int = 1) -> dict[str, Any]:
        return self.plan_service.dispatch_plan(plan_id, max_jobs=max_jobs)

    def _launch_plan_claim(self, claim: PlanDispatchClaim) -> JobRecord:
        return self.start_job(
            StartOptions(
                task_id=claim.dispatch_task_id,
                route=claim.execution.route,
                backend=claim.execution.backend,
                codex_quality_tier=claim.execution.codex_quality_tier,
                codex_model=claim.execution.codex_model,
                codex_reasoning_effort=claim.execution.codex_reasoning_effort,
                slot=claim.execution.slot,
                read_only=claim.execution.read_only,
                plan_id=claim.plan_id,
                plan_task_id=claim.task_id,
                plan_dispatch_token=claim.dispatch_token,
                workspace_access=claim.execution.workspace_access,
            )
        )

    def _accept_plan_handoff(self, plan_id: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
        return HandoffAcceptanceService(
            self.config.database_path,
            plan_store=self.plan_store,
            review_inbox=self.review_inbox,
            review_metrics=self.review_metrics,
        ).accept(plan_id, task_id, **kwargs)

    def retry_plan_task(
        self,
        plan_id: str,
        task_id: str,
        *,
        brief_override: str | None = None,
    ) -> dict[str, Any]:
        return self.plan_service.retry_plan_task(plan_id, task_id, brief_override=brief_override)

    def cancel_plan(self, plan_id: str) -> dict[str, Any]:
        return self.plan_service.cancel_plan(plan_id)

    def archive_plan(self, plan_id: str) -> dict[str, Any]:
        return self.plan_service.archive_plan(plan_id)

    def plan_snapshot(
        self,
        plan_id: str,
        *,
        since: int | None = None,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        return self.plan_service.plan_snapshot(
            plan_id, since=since, event_limit=event_limit, item_limit=item_limit
        )

    def watch_plan(
        self,
        plan_id: str,
        *,
        since: int,
        poll_interval_sec: float = 5.0,
        timeout_sec: float | None = 25.0,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        return self.plan_service.watch_plan(
            plan_id,
            since=since,
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
            event_limit=event_limit,
            item_limit=item_limit,
        )

    def run_plan_until_review(
        self,
        plan_id: str,
        *,
        max_jobs: int = 1,
        poll_interval_sec: float = 5.0,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        return self.plan_service.run_plan_until_review(
            plan_id,
            max_jobs=max_jobs,
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
        )

    def list_plans(
        self,
        limit: int = 20,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self.plan_service.list_plans(limit, include_archived=include_archived)

    def start_job(self, options: StartOptions) -> JobRecord:
        try:
            return JobLauncher(
                config=self.config,
                store=self.store,
                plan_store=self.plan_store,
                slots=self.slots,
                policy=self.policy,
                model_routing=self.model_routing,
                reconcile_jobs=self.reconcile_jobs,
                finish_job=self._finish_job,
                launch_worker=self._launch_worker,
                run_dir_for_job=self._run_dir_for_job,
                slot_error_type=SlotError,
            ).start(options)
        except JobLaunchError as exc:
            raise PolicyError(str(exc)) from exc

    def run_job(
        self,
        job_id: str,
        worker_instance_id: str | None = None,
    ) -> JobRecord:
        return self.job_execution.run_job(
            job_id,
            worker_instance_id=worker_instance_id,
        )

    def _refresh_stale_worker_if_needed(self, job_id: str) -> JobRecord:
        self.reconcile_jobs(job_id)
        return self.store.get_job(job_id)

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
            "codex_quota_domain": self._codex_quota_domain(job),
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "codex_quality_tier": job.codex_quality_tier,
            "codex_tool_call_budget": job.codex_tool_call_budget,
            "workspace_access": job.workspace_access,
            "native_quality": self._native_quality_summary(job),
            "worker_pid": job.worker_pid,
            "worker_instance_id": job.worker_instance_id,
            "worker_heartbeat_at": job.worker_heartbeat_at,
            "runner_pid": job.runner_pid,
            "agy_pid": job.agy_pid,
            "log_path": str(job.log_path) if job.log_path else None,
            "result_path": str(job.result_path),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "finalization_status": job.finalization_status,
            "finalization_error": job.finalization_error,
            "finalized_at": job.finalized_at,
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
            "codex_quota_domain": self._codex_quota_domain(job),
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "codex_quality_tier": job.codex_quality_tier,
            "codex_tool_call_budget": job.codex_tool_call_budget,
            "workspace_access": job.workspace_access,
            "native_quality": self._native_quality_summary(job),
            "worker_pid": job.worker_pid,
            "worker_instance_id": job.worker_instance_id,
            "worker_heartbeat_at": job.worker_heartbeat_at,
            "runner_pid": job.runner_pid,
            "agy_pid": job.agy_pid,
            "read_only": job.read_only,
            "slot_name": job.slot_name,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "finalization_status": job.finalization_status,
            "finalization_error": job.finalization_error,
            "finalized_at": job.finalized_at,
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
            "codex_quota_domain": self._codex_quota_domain(job),
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
            "workspace_access": job.workspace_access,
            "native_quality": self._native_quality_summary(job),
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

    def _codex_quota_domain(self, job: JobRecord) -> str:
        if normalize_backend(job.backend) != CODEX_BACKEND:
            return "primary"
        return codex_quota_domain(
            job.codex_model or self.config.defaults.codex_model,
            self.model_catalog,
        )

    def _native_quality_summary(self, job: JobRecord) -> dict[str, Any]:
        expected = resolve_native_quality_contract(
            self.config,
            job.route,
            workspace_access=job.workspace_access,
            read_only=job.read_only,
        )
        inspection = inspect_native_quality_contract(job.run_dir, expected)
        return {
            "policy": expected.policy,
            "gates": [gate.name for gate in expected.gates],
            "worker_gates": [
                gate.name for gate in expected.gates if gate.run_on in {"worker", "both"}
            ],
            "controller_gates": [
                gate.name for gate in expected.gates if gate.run_on in {"controller", "both"}
            ],
            "max_parallel": expected.max_parallel,
            "expected_sha256": expected.sha256,
            "persisted_sha256": inspection.persisted_sha256,
            "contract_state": inspection.state,
            "contract_path": str(inspection.path),
            "error": inspection.error,
        }

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
        return ArchiveService(
            store=self.store,
            runs_root=self.config.runs_root,
            is_terminal=self._is_terminal,
            clock=time.time,
        ).archive(
            older_than_days=older_than_days,
            limit=limit,
            apply=apply,
        )

    def collect_garbage(
        self,
        *,
        older_than_days: int = 30,
        limit: int = 500,
        apply: bool = False,
    ) -> dict[str, Any]:
        return RetentionService(
            self.config.database_path,
            plan_store=self.plan_store,
            job_store=self.store,
            review_inbox=self.review_inbox,
            clock=time.time,
        ).collect(
            older_than_days=older_than_days,
            limit=limit,
            apply=apply,
        )

    def sync_slots(self) -> list[dict[str, Any]]:
        return [status.as_dict() for status in self.slots.sync_configured_slots()]

    def list_review_inbox(
        self,
        *,
        review_status: str | None = "pending",
        parent_thread_id: str | None = None,
        limit: int = 50,
        sync_subagents: bool = False,
        since_hours: float | None = 72.0,
        max_files: int = 500,
    ) -> list[dict[str, Any]]:
        if sync_subagents:
            self.sync_subagent_results(
                since_hours=since_hours,
                max_files=max_files,
                parent_thread_id=parent_thread_id,
            )
        return [
            _compact_review_item(item, excerpt_limit=600)
            for item in self.review_inbox.list_items(
                review_status=review_status,
                parent_thread_id=parent_thread_id,
                limit=limit,
            )
        ]

    def get_review_inbox_item(self, item_id: str) -> dict[str, Any]:
        return self.review_inbox.get(item_id).as_dict()

    def resolve_review_inbox_item(self, item_id: str, decision: str) -> dict[str, Any]:
        return self.review_inbox.resolve(item_id, decision).as_dict()

    def sync_subagent_results(
        self,
        *,
        since_hours: float | None = 72.0,
        max_files: int = 500,
        parent_thread_id: str | None = None,
    ) -> dict[str, Any]:
        sessions_root = self.config.defaults.codex_sessions_root
        if sessions_root is None:
            raise PolicyError(
                "codex_sessions_root is not configured; cannot import Codex subagent results"
            )
        scope_roots: dict[str, Path] = {}
        for route_name, route_config in self.config.routes.items():
            scope_roots[f"route:{route_name}"] = route_config.path
            if route_config.worktree_base.resolve(strict=False) != route_config.path.resolve(
                strict=False
            ):
                scope_roots[f"route-base:{route_name}"] = route_config.worktree_base
        for configured_slot_name, slot in self.config.slots.items():
            scope_roots[f"slot:{configured_slot_name}"] = slot.path

        imported: list[ReviewInboxItem] = []
        for completion in scan_codex_subagent_completions(
            sessions_root,
            workspace_roots=scope_roots,
            parent_thread_id=parent_thread_id,
            since_hours=since_hours,
            max_files=max_files,
        ):
            matched_route, matched_slot_name = self._route_and_slot_for_scope(completion.route)
            imported.append(
                self.review_inbox.upsert(
                    ReviewInboxDraft(
                        source_kind="codex_subagent",
                        source_id=completion.thread_id,
                        source_status="completed",
                        source_completed_at=_source_completion_time(completion.completed_at),
                        delivery_status="ready",
                        route=matched_route,
                        workspace_path=completion.cwd,
                        slot_name=matched_slot_name,
                        parent_thread_id=completion.parent_thread_id,
                        agent_path=completion.agent_path,
                        rollout_path=completion.rollout_path,
                        result_excerpt=completion.result,
                        result_text=completion.result,
                    )
                )
            )
        return {
            "imported": len(imported),
            "items": [
                _sync_review_item(item)
                for item in sorted(
                    imported,
                    key=lambda candidate: (
                        candidate.source_completed_at or candidate.updated_at,
                        candidate.item_id,
                    ),
                    reverse=True,
                )[:5]
            ],
            "items_truncated": len(imported) > 5,
        }

    def checkpoint_slot(self, name: str, *, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not self._is_terminal(job):
            raise PolicyError(f"Job {job_id} is not terminal")
        if job.slot_name != name:
            raise PolicyError(
                f"Job {job_id} belongs to slot {job.slot_name!r}, not requested slot {name!r}"
            )
        status = self.slots.inspect_slot(name)
        if status.path.resolve(strict=False) != job.workspace_path.resolve(strict=False):
            raise PolicyError(f"Job {job_id} workspace does not match slot {name}")
        if status.active_job_id not in {None, job_id}:
            raise PolicyError(f"Slot {name} is active for another job: {status.active_job_id}")
        item = self.finalization.finish_slot_lifecycle(
            job,
            job.status,
            force_checkpoint=True,
            allow_inactive=True,
        )
        if item is None:
            raise PolicyError(f"Could not persist review inbox delivery for job {job_id}")
        return {
            "slot": self.slots.inspect_slot(name).as_dict(),
            "inbox": item.as_dict(),
        }

    def _route_and_slot_for_scope(self, scope: str) -> tuple[str, str | None]:
        if scope.startswith("slot:"):
            slot_name = scope.removeprefix("slot:")
            slot = self.config.slots.get(slot_name)
            if slot is None:
                raise PolicyError(f"Unknown configured slot scope: {scope}")
            return slot.route, slot_name
        if scope.startswith("route-base:"):
            return scope.removeprefix("route-base:"), None
        if scope.startswith("route:"):
            return scope.removeprefix("route:"), None
        return scope, None

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

    def list_slots(
        self,
        *,
        route: str | None = None,
        all_routes: bool = False,
        include_deleted: bool = False,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        if route is not None and all_routes:
            raise PolicyError("route and all_routes are mutually exclusive")
        if route is None and not all_routes:
            raise PolicyError("slot inventory scope is required; pass route or all_routes")
        return [
            status.as_dict()
            for status in self.slots.list_slots(
                route=route,
                include_deleted=include_deleted,
                include_stale=include_stale,
            )
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
        route: str | None = None,
        all_routes: bool = False,
    ) -> list[dict[str, str]]:
        return [
            decision.as_dict()
            for decision in self.slots.cleanup(
                max_per_route=max_per_route,
                apply=apply,
                force=force,
                route=route,
                all_routes=all_routes,
            )
        ]

    def _launch_worker(self, job_id: str, worker_instance_id: str) -> int:
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
            "--worker-instance-id",
            worker_instance_id,
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
        self.finalization.quota_broker = self.quota_broker
        return self.finalization.finish(
            job_id,
            status,
            last_error,
            worker_instance_id=self.job_execution.active_worker_instance_id,
        )

    def _replay_finalization(self, job_id: str, allow_inactive: bool) -> JobRecord:
        self.finalization.quota_broker = self.quota_broker
        return self.finalization.replay(job_id, allow_inactive=allow_inactive)

    @staticmethod
    def _is_terminal(job: JobRecord) -> bool:
        return job.finished_at is not None or job.status in TERMINAL_STATUSES


def _date_bucket_from_timestamp(timestamp: float) -> Path:
    moment = datetime.fromtimestamp(timestamp, UTC)
    return Path(f"{moment:%Y}") / f"{moment:%m}" / f"{moment:%d}"


def _job_start_timestamp(job: JobRecord) -> float:
    timestamp = job.started_at or job.created_at
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return 0.0


def _compact_review_item(
    item: ReviewInboxItem,
    *,
    excerpt_limit: int,
) -> dict[str, Any]:
    payload = item.as_dict()
    payload.pop("result_text", None)
    payload.pop("verification_json", None)
    excerpt = item.result_excerpt
    truncated = excerpt is not None and len(excerpt) > excerpt_limit
    if truncated and excerpt is not None:
        payload["result_excerpt"] = excerpt[: excerpt_limit - 3] + "..."
    payload["result_excerpt_truncated"] = truncated
    bundle = payload.pop("verification_bundle", None)
    if isinstance(bundle, dict):
        raw_result = bundle.get("result")
        raw_artifact = bundle.get("artifact")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        artifact: dict[str, Any] = raw_artifact if isinstance(raw_artifact, dict) else {}
        payload["verification_summary"] = {
            "review_ready": bundle.get("review_ready"),
            "format_valid": result.get("format_valid"),
            "status": result.get("status"),
            "verification_claim_count": len(result.get("verification_claims", [])),
            "actual_changed_file_count": len(bundle.get("changed_files_actual", [])),
            "artifact_kind": artifact.get("kind"),
            "checkpoint_verified": artifact.get("checkpoint_verified"),
            "artifact_error": artifact.get("error"),
        }
    return payload


def _sync_review_item(item: ReviewInboxItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "source_completed_at": item.source_completed_at,
        "parent_thread_id": item.parent_thread_id,
        "agent_path": item.agent_path,
    }


def _source_completion_time(value: str | int | float | None) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return text
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


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
