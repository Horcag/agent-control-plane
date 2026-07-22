from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_control_plane.app.runtime.job_guardrails import (
    GuardrailBaseline,
    JobGuardrails,
    WorkspaceDirtyBaseline,
)
from agent_control_plane.entities.job import JobRecord, JobStore
from agent_control_plane.entities.workspace import StartRequest, WorkspacePolicy
from agent_control_plane.features.agent_runner import (
    AGY_BACKEND,
    CLAUDE_BACKEND,
    CODEX_BACKEND,
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
    GlobalQuotaBroker,
    ModelCatalog,
    ModelProfile,
    ModelRoutingPolicy,
    QuotaDecision,
    WorkerLease,
    WorkerLeaseError,
    assess_result_contract,
    capture_process_identity,
    claude_ladder_for_explicit_model,
    codex_job_capacity_units,
    normalize_backend,
    resolve_claude_mcp_server_definition,
    select_ide_mcp_server,
    write_claude_mcp_config,
)
from agent_control_plane.features.antigravity_accounts import (
    AntigravityManagerAdapter,
    AntigravityManagerError,
    is_agy_quota_failure,
)
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.config import ControlConfig
from agent_control_plane.shared.git_tools import compact_status_preview

# Write-capable builtin Claude tools dropped from a read-only worker's allowlist so the
# headless worker (which cannot answer a permission prompt) is denied file mutations.
_CLAUDE_WRITE_TOOLS = frozenset({"Edit", "Write"})


class JobFinalizer(Protocol):
    quota_broker: GlobalQuotaBroker | None

    def finish(
        self,
        job_id: str,
        status: str,
        last_error: str | None = None,
        *,
        worker_instance_id: str | None = None,
    ) -> JobRecord: ...


@dataclass
class ExecutionState:
    prompt: str
    attempt_prompt: str
    runner: AgentRunner
    model_ladder: tuple[ModelProfile, ...]
    guardrail_baseline: GuardrailBaseline
    route_root_baseline: WorkspaceDirtyBaseline | None
    forbidden_tool_markers: tuple[str, ...]
    dirty_diff_max_changed_lines: int
    max_attempts: int
    model_index: int = 0
    resume_thread_id: str | None = None
    last_result_message: str = "Agent did not run"
    quota_recovery_used: bool = False


@dataclass(frozen=True)
class AttemptOutcome:
    result: AgentRunResult
    log_path: Path
    profile: ModelProfile
    guardrail_message: str | None


class AttemptGuard:
    def __init__(
        self,
        execution: JobExecutionService,
        job: JobRecord,
        state: ExecutionState,
    ) -> None:
        self._execution = execution
        self._job = job
        self._state = state
        self._last_check = 0.0
        self.message: str | None = None

    def should_stop(self) -> bool:
        if self._execution.worker_ownership_lost:
            return True
        if self._execution.store.cancel_requested(self._job.job_id):
            return True
        if self.message:
            return True
        if not self._poll_is_due():
            return False
        self.message = self._find_violation()
        if self.message is None:
            return False
        self._record_violation()
        return True

    def check_workspace_now(self) -> bool:
        if self.message:
            return True
        self.message = self._find_violation()
        if self.message is None:
            return False
        self._record_violation()
        return True

    def _poll_is_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < self._execution.config.defaults.guardrail_poll_sec:
            return False
        self._last_check = now
        return True

    def _find_violation(self) -> str | None:
        violation = self._execution.guardrails.workspace_violation(
            self._job,
            self._state.guardrail_baseline,
        )
        if violation is None:
            violation = self._execution.guardrails.route_root_violation(
                self._job,
                self._state.route_root_baseline,
            )
        if violation is None:
            violation = self._execution.guardrails.dirty_diff_violation(
                self._job,
                self._state.guardrail_baseline,
                max_changed_lines=self._state.dirty_diff_max_changed_lines,
            )
        return violation

    def _record_violation(self) -> None:
        message = self.message
        if message is None:
            return
        self._execution.guardrails.preserve_dirty_state(self._job, prefix="guardrail")
        self._execution.store.add_event(self._job.job_id, "error", message)
        self._execution.update_active_worker_job(
            self._job.job_id,
            status="guardrail_violation",
            last_error=message,
        )


class JobExecutionService:
    """Own one worker lease and execute bounded agent attempts to a terminal result."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        store: JobStore,
        policy: WorkspacePolicy,
        model_routing: ModelRoutingPolicy,
        guardrails: JobGuardrails,
        finalizer: JobFinalizer,
        runner_factory: Callable[[str], AgentRunner],
        quota_broker: GlobalQuotaBroker | None,
        claude_catalog: ModelCatalog | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.policy = policy
        self.model_routing = model_routing
        self.guardrails = guardrails
        self.finalizer = finalizer
        self.runner_factory = runner_factory
        self.quota_broker = quota_broker
        self.claude_catalog = claude_catalog
        self._active_worker_instance_id: str | None = None
        self._worker_ownership_lost: threading.Event | None = None

    @property
    def active_worker_instance_id(self) -> str | None:
        return self._active_worker_instance_id

    @property
    def worker_ownership_lost(self) -> bool:
        return self._worker_ownership_lost is not None and self._worker_ownership_lost.is_set()

    def run_job(
        self,
        job_id: str,
        worker_instance_id: str | None = None,
    ) -> JobRecord:
        job = self.store.get_job(job_id)
        instance_id = worker_instance_id or job.worker_instance_id or uuid.uuid4().hex
        self._claim_worker(job, instance_id)
        lease = WorkerLease(job.run_dir, instance_id)
        with lease:
            self._start_worker(job, instance_id)
            heartbeat_stop, heartbeat = self._start_heartbeat(job_id, instance_id)
            try:
                return self.execute(job_id)
            except Exception as exc:
                self._record_worker_crash(job_id, instance_id, exc)
                raise
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=2.0)
                self._worker_ownership_lost = None
                self._active_worker_instance_id = None

    def execute(self, job_id: str) -> JobRecord:
        job = self.store.get_job(job_id)
        quota_terminal = self._acquire_initial_quota(job)
        if quota_terminal is not None:
            return quota_terminal
        job = self.store.get_job(job_id)
        policy_terminal = self._enforce_start_policy(job)
        if policy_terminal is not None:
            return policy_terminal
        state = self._prepare_execution(job)
        attempt_no = 1
        while attempt_no <= state.max_attempts:
            cancellation = self._cancel_before_attempt(job)
            if cancellation is not None:
                return cancellation
            outcome = self._run_attempt(job, state, attempt_no)
            terminal = self._handle_attempt_result(job, state, attempt_no, outcome)
            if terminal is not None:
                return terminal
            attempt_no += 1
        self.write_blocked_result_if_missing(job, state.last_result_message)
        return self._finish(job_id, "failed", state.last_result_message)

    def wait_for_codex_quota(
        self,
        job: JobRecord,
        profile: ModelProfile | None = None,
    ) -> bool:
        broker = self.quota_broker
        if broker is None:
            return True
        active_profile = profile or self._model_ladder_for_job(job)[0]
        capacity_units = codex_job_capacity_units(
            active_profile.model,
            active_profile.reasoning_effort,
            self.model_routing.catalog,
        )
        last_reason: str | None = None
        while not self.store.cancel_requested(job.job_id):
            self.assert_active_worker(job.job_id)
            decision = broker.try_acquire(
                job.job_id,
                worker_pid=os.getpid(),
                capacity_units=capacity_units,
                model=active_profile.model,
            )
            if decision.acquired:
                self.update_active_worker_job(job.job_id, status="running")
                self._record_quota_acquired(job, active_profile, decision)
                return True
            self.update_active_worker_job(job.job_id, status="waiting_quota")
            if decision.reason != last_reason:
                self.store.add_event(job.job_id, "warning", self._quota_wait_message(decision))
                last_reason = decision.reason
            time.sleep(self._quota_sleep_seconds(decision))
        return False

    def assert_active_worker(self, job_id: str) -> None:
        instance_id = self._active_worker_instance_id
        if instance_id is None:
            return
        if self.worker_ownership_lost:
            raise WorkerLeaseError(f"Worker ownership was lost for job {job_id}")
        if self.store.heartbeat_worker(job_id, instance_id, worker_pid=os.getpid()):
            return
        self._mark_worker_ownership_lost()
        raise WorkerLeaseError(f"Worker ownership was lost for job {job_id}")

    def update_active_worker_job(self, job_id: str, **values: Any) -> JobRecord:
        instance_id = self._active_worker_instance_id
        if instance_id is None:
            return self.store.update_job(job_id, **values)
        if self.store.update_for_worker(job_id, instance_id, **values):
            return self.store.get_job(job_id)
        self._mark_worker_ownership_lost()
        raise WorkerLeaseError(f"Worker ownership was lost for job {job_id}")

    def _claim_worker(self, job: JobRecord, instance_id: str) -> None:
        if job.worker_instance_id is None:
            self.store.assign_worker(job.job_id, instance_id, worker_pid=os.getpid())
            return
        if job.worker_instance_id != instance_id:
            raise WorkerLeaseError(
                f"Worker identity mismatch for {job.job_id}: expected {job.worker_instance_id}"
            )

    def _start_worker(self, job: JobRecord, instance_id: str) -> None:
        if not self.store.update_for_worker(
            job.job_id,
            instance_id,
            status="running",
            worker_pid=os.getpid(),
            worker_heartbeat_at=utc_now(),
            started_at=job.started_at or utc_now(),
        ):
            raise WorkerLeaseError(f"Worker no longer owns job {job.job_id}")
        self._active_worker_instance_id = instance_id
        self._worker_ownership_lost = threading.Event()
        self.store.add_event(job.job_id, "info", f"Worker started with PID {os.getpid()}")

    def _start_heartbeat(
        self,
        job_id: str,
        instance_id: str,
    ) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._worker_heartbeat_loop,
            args=(job_id, instance_id, stop),
            name=f"acp-heartbeat-{job_id[:32]}",
            daemon=True,
        )
        heartbeat.start()
        return stop, heartbeat

    def _worker_heartbeat_loop(
        self,
        job_id: str,
        worker_instance_id: str,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(5.0):
            try:
                owned = self.store.heartbeat_worker(
                    job_id,
                    worker_instance_id,
                    worker_pid=os.getpid(),
                )
            except (OSError, sqlite3.Error):
                owned = False
            if owned:
                continue
            self._mark_worker_ownership_lost()
            return

    def _mark_worker_ownership_lost(self) -> None:
        if self._worker_ownership_lost is not None:
            self._worker_ownership_lost.set()

    def _record_worker_crash(self, job_id: str, instance_id: str, exc: Exception) -> None:
        current = self.store.get_job(job_id)
        if current.finished_at is not None or current.worker_instance_id != instance_id:
            return
        self.store.finish_running_attempts(job_id, "worker_error", message=str(exc))
        self.store.add_event(job_id, "error", f"Worker crashed: {exc}")
        self._finish(job_id, "worker_error", str(exc))

    def _acquire_initial_quota(self, job: JobRecord) -> JobRecord | None:
        if normalize_backend(job.backend) != CODEX_BACKEND or self.quota_broker is None:
            return None
        if self.wait_for_codex_quota(job):
            return None
        message = "Cancel requested while waiting for global Codex quota"
        self.write_blocked_result_if_missing(job, message)
        return self._finish(job.job_id, "cancelled", message)

    def _enforce_start_policy(self, job: JobRecord) -> JobRecord | None:
        check = self.policy.check_start(
            StartRequest(
                task_id=job.task_id,
                route=job.route,
                workspace_path=job.workspace_path,
                expected_branch=job.expected_branch,
                allow_dirty=job.allow_dirty,
            )
        )
        if check.ok:
            return None
        message = "\n".join(check.reasons)
        self.write_blocked_result_if_missing(job, message)
        self.store.add_event(job.job_id, "error", message)
        return self._finish(job.job_id, "blocked", message)

    def _prepare_execution(self, job: JobRecord) -> ExecutionState:
        prompt = job.prompt_path.read_text(encoding="utf-8")
        model_ladder = self._model_ladder_for_job(job)
        route_config = self.config.routes.get(job.route)
        forbidden_tool_markers = (
            route_config.codex_forbidden_tool_markers
            if route_config and route_config.codex_forbidden_tool_markers is not None
            else self.config.defaults.codex_forbidden_tool_markers
        )
        dirty_diff_max_changed_lines = (
            route_config.dirty_diff_max_changed_lines
            if route_config and route_config.dirty_diff_max_changed_lines is not None
            else self.config.defaults.dirty_diff_max_changed_lines
        )
        max_attempts = job.max_restarts + (
            len(model_ladder) if normalize_backend(job.backend) == CODEX_BACKEND else 1
        )
        return ExecutionState(
            prompt=prompt,
            attempt_prompt=prompt,
            runner=self.runner_factory(job.backend),
            model_ladder=model_ladder,
            guardrail_baseline=self.guardrails.workspace_baseline(job),
            route_root_baseline=self.guardrails.route_root_baseline(job, route_config),
            forbidden_tool_markers=forbidden_tool_markers,
            dirty_diff_max_changed_lines=dirty_diff_max_changed_lines,
            max_attempts=max_attempts,
            last_result_message=f"{job.backend} did not run",
        )

    def _cancel_before_attempt(self, job: JobRecord) -> JobRecord | None:
        if not self.store.cancel_requested(job.job_id):
            return None
        message = "Cancel requested before attempt"
        self.write_blocked_result_if_missing(job, message)
        return self._finish(job.job_id, "cancelled", message)

    def _run_attempt(
        self,
        job: JobRecord,
        state: ExecutionState,
        attempt_no: int,
    ) -> AttemptOutcome:
        profile = state.model_ladder[state.model_index]
        log_path = job.run_dir / f"attempt-{attempt_no:03d}.log"
        self._begin_attempt(job, attempt_no, log_path, profile)
        guard = AttemptGuard(self, job, state)
        result = state.runner.run(
            self._agent_run_spec(job, state, profile, log_path),
            cancel_requested=guard.should_stop,
            pid_observed=lambda pid: self._record_runner_pid(job, pid),
        )
        guard.check_workspace_now()
        self._complete_attempt(job, attempt_no, profile, result)
        return AttemptOutcome(
            result=result,
            log_path=log_path,
            profile=profile,
            guardrail_message=guard.message,
        )

    def _begin_attempt(
        self,
        job: JobRecord,
        attempt_no: int,
        log_path: Path,
        profile: ModelProfile,
    ) -> None:
        self.assert_active_worker(job.job_id)
        self.store.start_attempt(job.job_id, attempt_no, log_path)
        self.update_active_worker_job(
            job.job_id,
            status="running",
            log_path=log_path,
            runner_pid=None,
            agy_pid=None,
        )
        attempt_profile = (
            job.agy_model or "agy-default"
            if normalize_backend(job.backend) == AGY_BACKEND
            else f"{profile.model}/{profile.reasoning_effort}"
        )
        self.store.add_event(
            job.job_id,
            "info",
            f"Attempt {attempt_no} started with {attempt_profile}",
        )

    def _complete_attempt(
        self,
        job: JobRecord,
        attempt_no: int,
        profile: ModelProfile,
        result: AgentRunResult,
    ) -> None:
        self.assert_active_worker(job.job_id)
        self.store.finish_attempt(
            job.job_id,
            attempt_no,
            result.status,
            result_status=result.result_status,
            exit_code=result.exit_code,
            message=result.message,
        )
        for event in result.lifecycle_events:
            level = "warning" if event.kind == "budget_warning" else "info"
            self.store.add_event(job.job_id, level, event.message)
        if result.metrics is not None:
            self.store.record_attempt_metrics(
                job.job_id,
                attempt_no,
                backend=job.backend,
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,
                metrics=result.metrics,
            )
        self.update_active_worker_job(
            job.job_id,
            runner_pid=None,
            runner_process_identity=None,
            agy_pid=None,
        )
        self.store.add_event(
            job.job_id,
            "info",
            f"Attempt {attempt_no} ended: {result.status}",
        )

    def _agent_run_spec(
        self,
        job: JobRecord,
        state: ExecutionState,
        profile: ModelProfile,
        log_path: Path,
    ) -> AgentRunSpec:
        claude_mcp_config_path, claude_allowed_tools = self._claude_binding(job)
        return AgentRunSpec(
            backend=job.backend,
            agy_command=self.config.agy_command,
            agy_model=job.agy_model,
            codex_command=self.config.codex_command,
            codex_model=profile.model,
            codex_reasoning_effort=profile.reasoning_effort,
            codex_sandbox_mode=self.config.defaults.codex_sandbox_mode,
            codex_disabled_mcp_servers=self._disabled_mcp_servers(job),
            prompt=state.attempt_prompt,
            workspace_path=job.workspace_path,
            result_path=job.result_path,
            log_path=log_path,
            print_timeout=job.print_timeout,
            timeout_sec=job.timeout_sec,
            idle_timeout_sec=job.idle_timeout_sec,
            yolo=job.yolo,
            read_only=job.read_only,
            codex_no_progress_timeout_sec=self.config.defaults.codex_no_progress_timeout_sec,
            codex_tool_timeout_limit=self.config.defaults.codex_tool_timeout_limit,
            codex_tool_call_budget=job.codex_tool_call_budget or 0,
            codex_tool_call_budget_grace_sec=self.config.defaults.codex_tool_call_budget_grace_sec,
            codex_invalid_verification_grace_sec=(
                self.config.defaults.codex_invalid_verification_grace_sec
            ),
            codex_terminal_tab_name=None if job.workspace_access == "native" else job.task_id,
            codex_forbidden_tool_markers=self._effective_forbidden_markers(job, state),
            codex_resume_thread_id=state.resume_thread_id,
            codex_sessions_root=self.config.defaults.codex_sessions_root,
            workspace_access=job.workspace_access,
            claude_command=self.config.claude_command,
            claude_model=(
                profile.model if normalize_backend(job.backend) == CLAUDE_BACKEND else None
            ),
            claude_reasoning_effort=(
                profile.reasoning_effort
                if normalize_backend(job.backend) == CLAUDE_BACKEND
                else None
            ),
            claude_permission_mode=self.config.defaults.claude_permission_mode,
            claude_allowed_tools=claude_allowed_tools,
            claude_sessions_root=self.config.defaults.claude_sessions_root,
            claude_max_turns=self.config.defaults.claude_max_turns,
            claude_bare=self.config.defaults.claude_bare,
            claude_mcp_config_path=claude_mcp_config_path,
        )

    def _claude_binding(self, job: JobRecord) -> tuple[Path | None, tuple[str, ...]]:
        """Resolve the IDE MCP config and tool allowlist for a claude job.

        Two concerns, both expressed through the worker's ``--allowedTools``:
        - Read-only: drop the write-capable builtin tools so, under ``default`` prompting,
          the headless worker is denied any file mutation (it cannot answer a prompt).
        - ide_mcp: the worker is launched bare, so ACP hands it exactly the route's IDE MCP
          server via a per-job ``--mcp-config`` file and allowlists that server's
          ``mcp__<server>__*`` tools so it may call them without an approval it can never get.
        Native and non-claude jobs get no MCP config.
        """

        allowed_tools = tuple(self.config.defaults.claude_allowed_tools)
        if job.read_only:
            allowed_tools = tuple(t for t in allowed_tools if t not in _CLAUDE_WRITE_TOOLS)
        if normalize_backend(job.backend) != CLAUDE_BACKEND or job.workspace_access == "native":
            return None, allowed_tools
        server_name = select_ide_mcp_server(self.config, job.route)
        definition = resolve_claude_mcp_server_definition(self.config, server_name)
        config_path = write_claude_mcp_config(job.run_dir, server_name, definition)
        server_tools = f"mcp__{server_name.replace('-', '_')}"
        if server_tools not in allowed_tools:
            allowed_tools = (*allowed_tools, server_tools)
        return config_path, allowed_tools

    def _disabled_mcp_servers(self, job: JobRecord) -> tuple[str, ...]:
        disabled = list(dict.fromkeys(self.config.defaults.codex_disabled_mcp_servers))
        if job.workspace_access != "native":
            return tuple(disabled)
        configured_ide_servers = tuple(
            route.ide_mcp_server for route in self.config.routes.values() if route.ide_mcp_server
        )
        native_disabled = (
            *configured_ide_servers,
            "agentbridge_dataspell_8643",
            "agentbridge_idea_64343",
            "agentbridge_idea_8644",
        )
        return tuple(dict.fromkeys((*disabled, *native_disabled)))

    @staticmethod
    def _effective_forbidden_markers(
        job: JobRecord,
        state: ExecutionState,
    ) -> tuple[str, ...]:
        if job.workspace_access != "native":
            return state.forbidden_tool_markers
        return tuple(marker for marker in state.forbidden_tool_markers if marker != "raw_exec")

    def _record_runner_pid(self, job: JobRecord, pid: int | None) -> None:
        identity = capture_process_identity(pid) if pid is not None else None
        updates: dict[str, int | str | None] = {
            "runner_pid": pid,
            "runner_process_identity": identity.to_json() if identity else None,
        }
        if job.backend == AGY_BACKEND:
            updates["agy_pid"] = pid
        if pid is not None and identity is None:
            self.store.add_event(
                job.job_id,
                "warning",
                f"Could not record durable process identity for runner PID {pid}",
            )
        self.update_active_worker_job(job.job_id, **updates)

    def _handle_attempt_result(
        self,
        job: JobRecord,
        state: ExecutionState,
        attempt_no: int,
        outcome: AttemptOutcome,
    ) -> JobRecord | None:
        result = outcome.result
        state.last_result_message = result.message
        if outcome.guardrail_message:
            self.write_blocked_result_if_missing(job, outcome.guardrail_message)
            return self._finish(job.job_id, "guardrail_violation", outcome.guardrail_message)
        if result.completed:
            assessment = assess_result_contract(
                job.expected_result_status,
                result.result_status or "completed",
            )
            if assessment.matches:
                return self._finish(
                    job.job_id,
                    assessment.effective_terminal_status,
                    result.message,
                )
            if (
                assessment.expected_status == "completed"
                and assessment.reported_status == "partial"
                and attempt_no < state.max_attempts
            ):
                if self._should_escalate_model(job, state, result):
                    return self._prepare_model_escalation(job, state, result)
                self._prepare_partial_continuation(job, state, result)
                return None
            message = (
                "result_contract_mismatch "
                f"expected={assessment.expected_status} reported={assessment.reported_status}"
            )
            self.store.add_event(job.job_id, "error", message)
            return self._finish(job.job_id, assessment.effective_terminal_status, message)
        runner_failure = result.status
        self.store.set_runner_failure(job.job_id, runner_failure)
        if result.status == "cancelled":
            self.write_blocked_result_if_missing(job, result.message)
            return self._finish(job.job_id, "cancelled", result.message)
        if result.status == "blocked":
            self.write_blocked_result_if_missing(job, result.message)
            self.store.add_event(job.job_id, "error", result.message)
            return self._finish(job.job_id, "blocked", result.message)
        if result.status == "exited_without_result" and self._handle_missing_result(
            job,
            state,
            outcome,
        ):
            return None
        dirty_message = self.guardrails.preserve_dirty_state(
            job,
            prefix="dirty-after-failure",
        )
        if dirty_message and not job.allow_dirty:
            self.store.set_workspace_disposition(job.job_id, "dirty_after_failure")
            cause = runner_failure or result.status
            message = f"runner_failure={cause}; {dirty_message}"
            self.write_blocked_result_if_missing(job, message)
            self.store.add_event(job.job_id, "error", message)
            return self._finish(job.job_id, "stopped_dirty_after_failure", message)
        if runner_failure is not None:
            self.store.set_workspace_disposition(job.job_id, "clean")
        if result.status == "inefficient_tool_usage":
            self.store.add_event(job.job_id, "error", result.message)
            return self._finish(job.job_id, result.status, result.message)
        if attempt_no < state.max_attempts:
            self.store.add_event(job.job_id, "warning", "Restarting after failed attempt")
        return None

    def _should_escalate_model(
        self,
        job: JobRecord,
        state: ExecutionState,
        result: AgentRunResult,
    ) -> bool:
        classification = result.escalation_classification
        accepted = normalize_backend(
            job.backend
        ) == CODEX_BACKEND and self.model_routing.should_escalate(
            runner_status=result.status,
            result_status=result.result_status,
            has_next=state.model_index + 1 < len(state.model_ladder),
            escalation_classification=classification,
        )
        self.store.add_event(
            job.job_id,
            "warning" if accepted else "info",
            f"Model escalation {'accepted' if accepted else 'refused'}; classification={classification or 'unclassified'}",
        )
        return accepted

    def _prepare_model_escalation(
        self,
        job: JobRecord,
        state: ExecutionState,
        result: AgentRunResult,
    ) -> JobRecord | None:
        state.model_index += 1
        self._resume_from_metrics(state, result)
        profile = state.model_ladder[state.model_index]
        self.update_active_worker_job(
            job.job_id,
            codex_model=profile.model,
            codex_reasoning_effort=profile.reasoning_effort,
        )
        continuation = (
            "Continue the same assigned task from the existing workspace state. "
            f"The prior attempt ended as {result.status}/"
            f"{result.result_status or 'no-result'}: {result.message}. "
            "Review the current changes, finish the implementation, run the required "
            "checks, and write the required result.md with a final Status marker."
        )
        state.attempt_prompt = (
            continuation if state.resume_thread_id else f"{state.prompt}\n\n{continuation}"
        )
        self.store.add_event(
            job.job_id,
            "warning",
            f"Escalating to {profile.model}/{profile.reasoning_effort}; "
            f"resume_thread={state.resume_thread_id or 'unavailable'}",
        )
        if self.quota_broker is None or self.wait_for_codex_quota(job, profile):
            return None
        message = "Cancel requested while waiting to resize global Codex quota"
        self.write_blocked_result_if_missing(job, message)
        return self._finish(job.job_id, "cancelled", message)

    def _prepare_partial_continuation(
        self,
        job: JobRecord,
        state: ExecutionState,
        result: AgentRunResult,
    ) -> None:
        self._resume_from_metrics(state, result)
        continuation = (
            "Continue the same assigned task from the existing workspace and progress state. "
            "The prior attempt wrote Status: partial. Do not repeat completed discovery or "
            "revert useful changes. Finish the remaining acceptance criteria, run the required "
            "checks, commit when requested, and overwrite result.md with the final Status "
            "marker. A soft tool-call or changed-line checkpoint is not a blocker while scoped "
            "progress remains possible."
        )
        state.attempt_prompt = (
            continuation if state.resume_thread_id else f"{state.prompt}\n\n{continuation}"
        )
        self.store.add_event(
            job.job_id,
            "warning",
            f"Continuing partial {normalize_backend(job.backend)} result with the same model; "
            f"resume_thread={state.resume_thread_id or 'unavailable'}",
        )

    @staticmethod
    def _resume_from_metrics(state: ExecutionState, result: AgentRunResult) -> None:
        if result.metrics is not None and result.metrics.thread_id:
            state.resume_thread_id = result.metrics.thread_id

    def _handle_missing_result(
        self,
        job: JobRecord,
        state: ExecutionState,
        outcome: AttemptOutcome,
    ) -> bool:
        diagnostic = self.missing_result_message(job, outcome.result, outcome.log_path)
        recovery = None
        if job.backend == AGY_BACKEND:
            recovery = self._auto_switch_agy_after_quota_failure(
                job,
                outcome.log_path,
                diagnostic,
                already_used=state.quota_recovery_used,
            )
        if recovery is not None:
            state.quota_recovery_used = True
            state.max_attempts += 1
            state.last_result_message = recovery
            self.store.add_event(job.job_id, "warning", recovery)
            self.store.add_event(job.job_id, "warning", "Retrying after agy account auto-switch")
            return True
        self.write_blocked_result_if_missing(job, diagnostic)
        self.store.add_event(job.job_id, "error", diagnostic)
        state.last_result_message = diagnostic
        return False

    def _model_ladder_for_job(self, job: JobRecord) -> tuple[ModelProfile, ...]:
        if normalize_backend(job.backend) == CLAUDE_BACKEND:
            if self.claude_catalog is None:
                raise ValueError(f"Claude job {job.job_id} requires a Claude model catalog")
            model = job.codex_model or self.config.defaults.claude_model
            effort = job.codex_reasoning_effort or self.config.defaults.claude_reasoning_effort
            return claude_ladder_for_explicit_model(self.claude_catalog, model, effort)
        if normalize_backend(job.backend) != CODEX_BACKEND or job.codex_quality_tier is None:
            model = job.codex_model or self.config.defaults.codex_model
            effort = job.codex_reasoning_effort or self.config.defaults.codex_reasoning_effort
            return self.model_routing.ladder_for_explicit_model(model, effort)
        stored_profile = _stored_codex_profile(job)
        if stored_profile is None:
            raise ValueError(f"Automatic Codex job {job.job_id} is missing its stored profile")
        try:
            policy = self.model_routing.policy(job.codex_quality_tier)
        except ValueError:
            return (stored_profile,)
        if job.codex_tool_call_budget != policy.tool_call_budget:
            return (stored_profile,)
        persisted_ladder = _persisted_routing_ladder(
            self.store.routing_decision(job.job_id),
            route=job.route,
            first_profile=stored_profile,
            quality_tier=job.codex_quality_tier,
            tool_call_budget=policy.tool_call_budget,
            task_class=policy.task_class,
            catalog_source=self.model_routing.catalog.source,
            catalog_version=self.model_routing.catalog.version,
        )
        return persisted_ladder or (stored_profile,)

    def _record_quota_acquired(
        self,
        job: JobRecord,
        profile: ModelProfile,
        decision: QuotaDecision,
    ) -> None:
        self.store.add_event(
            job.job_id,
            "info",
            "Global Codex quota acquired for "
            f"{profile.model}/{profile.reasoning_effort}; "
            f"domain={decision.quota_domain}; "
            f"active_jobs={decision.active_jobs}, "
            f"capacity={decision.active_capacity_units}/{decision.max_capacity_units}",
        )

    def _quota_sleep_seconds(self, decision: QuotaDecision) -> float:
        sleep_for = self.config.defaults.codex_quota_poll_sec
        if decision.retry_after_sec is not None and decision.retry_after_sec > 0:
            return min(sleep_for, decision.retry_after_sec)
        return sleep_for

    @staticmethod
    def _quota_wait_message(decision: QuotaDecision) -> str:
        detail = (
            f"active_jobs={decision.active_jobs}, "
            f"capacity={decision.active_capacity_units}/{decision.max_capacity_units}"
        )
        if decision.retry_after_sec is not None:
            detail += f", retry_after_sec={round(decision.retry_after_sec, 1)}"
        return (
            f"Waiting for global Codex quota: {decision.reason or 'unavailable'} "
            f"(domain={decision.quota_domain}; {detail})"
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
            self.store.add_event(job.job_id, "error", f"agy quota auto-switch failed: {exc}")
            return None
        return (
            "agy quota failure detected; switched Antigravity Manager agy account "
            f"from {result.previous_email or result.previous_account_id or '<none>'} "
            f"to {result.email} using strategy {result.strategy}"
        )

    def _finish(self, job_id: str, status: str, last_error: str | None = None) -> JobRecord:
        self.finalizer.quota_broker = self.quota_broker
        return self.finalizer.finish(
            job_id,
            status,
            last_error,
            worker_instance_id=self._active_worker_instance_id,
        )

    @staticmethod
    def missing_result_message(job: JobRecord, result: object, log_path: Path) -> str:
        status = getattr(result, "status", "unknown")
        message = getattr(result, "message", "no runner message")
        exit_code = getattr(result, "exit_code", None)
        if job.backend == AGY_BACKEND:
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
            if is_agy_quota_failure(log_text):
                message = f"agy quota exhausted before result creation; detector_reason={message}"
        return (
            f"{job.backend} exited without writing a valid result file. "
            f"job_id={job.job_id}; task_id={job.task_id}; runner_status={status}; "
            f"exit_code={exit_code}; reason={message}; log_path={log_path}"
        )

    @staticmethod
    def write_blocked_result_if_missing(job: JobRecord, message: str) -> None:
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
        changed_files, artifact_note = JobExecutionService._dirty_result_context(job)
        risk_message = message if not artifact_note else f"{message}; {artifact_note}"
        next_action = "inspect the attempt log and rerun after fixing the blocker"
        if artifact_note:
            next_action = (
                "inspect the preserved dirty status/patch and rerun after fixing the blocker"
            )
        job.result_path.parent.mkdir(parents=True, exist_ok=True)
        what_changed = (
            "worker left uncommitted workspace changes and controller preserved review evidence"
            if artifact_note
            else "no durable workspace changes were observed"
        )
        job.result_path.write_text(
            "Status: blocked\n\n"
            f"Changed files: {changed_files}\n\n"
            f"What changed: {what_changed}\n\n"
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
        return compact_status_preview(status_text), artifact_note


def _stored_codex_profile(job: JobRecord) -> ModelProfile | None:
    if not isinstance(job.codex_model, str) or not job.codex_model.strip():
        return None
    if not isinstance(job.codex_reasoning_effort, str) or not job.codex_reasoning_effort.strip():
        return None
    return ModelProfile(job.codex_model.strip(), job.codex_reasoning_effort.strip().lower())


def _persisted_routing_ladder(
    payload: Any,
    *,
    route: str,
    first_profile: ModelProfile,
    quality_tier: str | None,
    tool_call_budget: int | None,
    task_class: str,
    catalog_source: str,
    catalog_version: str | None,
) -> tuple[ModelProfile, ...] | None:
    if not isinstance(payload, Mapping):
        return None
    if payload.get("event") != "routing_decision" or payload.get("route") != route:
        return None
    if not isinstance(quality_tier, str) or not quality_tier.strip():
        return None
    if (
        not isinstance(payload.get("requested_policy"), str)
        or payload["requested_policy"].strip().casefold() != quality_tier.strip().casefold()
    ):
        return None
    if (
        not isinstance(tool_call_budget, int)
        or isinstance(tool_call_budget, bool)
        or tool_call_budget <= 0
    ):
        return None
    if (
        not isinstance(payload.get("tool_call_budget"), int)
        or isinstance(payload["tool_call_budget"], bool)
        or payload["tool_call_budget"] <= 0
        or payload["tool_call_budget"] != tool_call_budget
    ):
        return None
    if (
        not isinstance(task_class, str)
        or not task_class.strip()
        or not isinstance(payload.get("task_class"), str)
        or payload["task_class"].strip() != task_class.strip()
    ):
        return None
    catalog = payload.get("catalog")
    if not isinstance(catalog, Mapping):
        return None
    if (
        not isinstance(catalog_source, str)
        or not catalog_source.strip()
        or not isinstance(catalog.get("source"), str)
        or catalog["source"].strip() != catalog_source.strip()
    ):
        return None
    payload_catalog_version = catalog.get("version")
    if payload_catalog_version is not None and (
        not isinstance(payload_catalog_version, str) or not payload_catalog_version.strip()
    ):
        return None
    if payload_catalog_version != catalog_version:
        return None
    selection_source = payload.get("selection_source")
    if selection_source not in {"configured_fallback", "history"}:
        return None
    configured_fallback = payload.get("configured_fallback")
    if not isinstance(configured_fallback, bool) or configured_fallback != (
        selection_source == "configured_fallback"
    ):
        return None
    raw_ladder = payload.get("ladder")
    if not isinstance(raw_ladder, list) or not raw_ladder:
        return None
    chosen_profile = _profile_from_payload(payload.get("chosen_profile"))
    if chosen_profile is None:
        return None
    ladder: list[ModelProfile] = []
    seen: set[tuple[str, str]] = set()
    for raw_profile in raw_ladder:
        profile = _profile_from_payload(raw_profile)
        if profile is None:
            return None
        key = (profile.model.casefold(), profile.reasoning_effort)
        if key in seen:
            return None
        seen.add(key)
        ladder.append(profile)
    if ladder[0] != first_profile or chosen_profile != ladder[0]:
        return None
    return tuple(ladder)


def _profile_from_payload(value: Any) -> ModelProfile | None:
    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    effort = value.get("reasoning_effort")
    if not isinstance(model, str) or not model.strip():
        return None
    if not isinstance(effort, str) or not effort.strip():
        return None
    return ModelProfile(model.strip(), effort.strip().lower())
