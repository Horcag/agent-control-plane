from __future__ import annotations

import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol, TextIO

from agent_control_plane.features.agent_runner.lib.codex_telemetry import codex_turn_completed
from agent_control_plane.features.agent_runner.lib.codex_watchdog import (
    CODEX_INEFFICIENT_TOOL_USAGE_LIMIT,
    CODEX_TOOL_TIMEOUT_MARKER,
    productive_log_activity_if_needed,
    progress_signature,
    refresh_log_activity,
    scan_budget_lifecycle,
    scan_codex_tool_constraints,
    scan_forbidden_tool,
    scan_tool_timeouts,
    tool_budget_policy,
    tool_call_budget_runaway_cap,
    updated_calls_without_durable_progress,
)
from agent_control_plane.features.agent_runner.lib.result_detector import (
    ResultState,
    contains_capacity_marker,
    inspect_result,
    recover_result_from_last_message,
)
from agent_control_plane.features.agent_runner.lib.runner import (
    AgentRunResult,
    AgentRunSpec,
    BudgetLifecycleEvent,
)
from agent_control_plane.shared.verification_report import verification_path_for_result

CODEX_COMPLETION_GRACE_SEC = 60.0


class TerminableProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class CodexProcessMonitor:
    def _turn_completed(self, event_log_path: Path) -> bool:
        return codex_turn_completed(event_log_path)

    def _scan_tool_constraints(
        self,
        spec: AgentRunSpec,
        scan_size: int,
        tool_call_count: int,
    ) -> tuple[str | None, int, int]:
        return scan_codex_tool_constraints(
            spec.log_path.with_suffix(".events.jsonl"),
            scan_size,
            tool_call_count,
            tool_call_budget=spec.codex_tool_call_budget,
            terminal_tab_name=spec.codex_terminal_tab_name,
        )

    def monitor(
        self,
        proc: subprocess.Popen[str],
        spec: AgentRunSpec,
        started_wall: float,
        deadline_mono: float,
        last_output_mono: float,
        last_log_size: int,
        log: TextIO,
        cancel_requested: Callable[[], bool],
    ) -> AgentRunResult:
        last_productive_mono = time.monotonic()
        last_progress_check_mono = 0.0
        last_tool_timeout_scan_size = 0
        tool_timeout_count = 0
        last_forbidden_tool_scan_size = 0
        last_productive_log_scan_size = 0
        last_constraint_scan_size = 0
        tool_call_count = 0
        budget_scan_size = 0
        budget_tool_call_count = 0
        budget_events: list[BudgetLifecycleEvent] = []
        budget_policy = tool_budget_policy(spec.codex_tool_call_budget)
        current_progress_signature, workspace_dirty = progress_signature(spec)
        observed_tool_call_count = 0
        calls_without_durable_progress = 0
        budget_breach_mono: float | None = None
        budget_breach_message: str | None = None
        invalid_verification_breach_mono: float | None = None
        invalid_verification_marker: tuple[float | None, float | None, str | None] | None = None

        while True:
            log.flush()
            last_output_mono, last_log_size = refresh_log_activity(
                spec,
                last_output_mono,
                last_log_size,
            )

            now = time.monotonic()
            productive_log_seen, last_productive_log_scan_size = productive_log_activity_if_needed(
                spec,
                last_productive_log_scan_size,
            )
            if productive_log_seen:
                last_productive_mono = now
            if now - last_progress_check_mono >= 2.0:
                last_progress_check_mono = now
                next_signature, workspace_dirty = progress_signature(spec)
                if next_signature != current_progress_signature:
                    current_progress_signature = next_signature
                    calls_without_durable_progress = 0
                    last_productive_mono = now

            constraint_violation, last_constraint_scan_size, tool_call_count = (
                self._scan_tool_constraints(
                    spec,
                    last_constraint_scan_size,
                    tool_call_count,
                )
            )
            if not spec.read_only and tool_call_count > observed_tool_call_count:
                next_signature, workspace_dirty = progress_signature(spec)
                calls_without_durable_progress = updated_calls_without_durable_progress(
                    current_progress_signature,
                    next_signature,
                    calls_without_durable_progress,
                    tool_call_count - observed_tool_call_count,
                )
                current_progress_signature = next_signature
                observed_tool_call_count = tool_call_count
                if calls_without_durable_progress >= CODEX_INEFFICIENT_TOOL_USAGE_LIMIT:
                    completed = self._completed_result_if_ready(
                        proc,
                        spec,
                        started_wall,
                        terminate=True,
                        cancel_requested=cancel_requested,
                    )
                    if completed is not None:
                        return replace(completed, lifecycle_events=tuple(budget_events))
                    terminate_spawned_process(proc)
                    return self._stopped_result(
                        proc,
                        "inefficient_tool_usage",
                        "Codex started 16 tool calls without durable progress",
                        lifecycle_events=tuple(budget_events),
                    )
            budget_scan = scan_budget_lifecycle(
                spec.log_path.with_suffix(".events.jsonl"),
                budget_scan_size,
                budget_tool_call_count,
                policy=budget_policy,
                warning_emitted=any(event.kind == "budget_warning" for event in budget_events),
                handoff_emitted=any(event.kind == "budget_handoff" for event in budget_events),
            )
            budget_scan_size = budget_scan.scan_size
            budget_tool_call_count = budget_scan.tool_call_count
            budget_events.extend(budget_scan.events)
            hard_budget_violation = (
                constraint_violation
                if constraint_violation and constraint_violation.startswith("Codex exceeded")
                else None
            )
            if constraint_violation is not None and hard_budget_violation is None:
                terminate_spawned_process(proc)
                status = (
                    "terminal_scope_violation"
                    if constraint_violation.startswith("Terminal tool")
                    else "tool_call_budget"
                )
                return self._stopped_result(
                    proc,
                    status,
                    constraint_violation,
                    lifecycle_events=tuple(budget_events),
                )
            if hard_budget_violation is not None and budget_breach_mono is None:
                if spec.codex_tool_call_budget_grace_sec <= 0:
                    completed = self._completed_result_if_ready(
                        proc,
                        spec,
                        started_wall,
                        terminate=True,
                        cancel_requested=cancel_requested,
                    )
                    if completed is not None:
                        return replace(completed, lifecycle_events=tuple(budget_events))
                    terminate_spawned_process(proc)
                    return self._stopped_result(
                        proc,
                        "tool_call_budget",
                        hard_budget_violation,
                        lifecycle_events=tuple(budget_events),
                    )
                budget_breach_mono = now
                budget_breach_message = hard_budget_violation
                budget_events.append(
                    BudgetLifecycleEvent(
                        "budget_breach",
                        tool_call_count,
                        f"{hard_budget_violation}; granting a "
                        f"{spec.codex_tool_call_budget_grace_sec}s grace window to finish "
                        "the terminal handoff",
                    )
                )

            if budget_breach_mono is not None and budget_breach_message is not None:
                runaway_cap = tool_call_budget_runaway_cap(spec.codex_tool_call_budget)
                if tool_call_count > runaway_cap:
                    terminate_spawned_process(proc)
                    return self._stopped_result(
                        proc,
                        "tool_call_budget",
                        f"{budget_breach_message}; runaway cap of {runaway_cap} tool calls "
                        "fired during the grace window",
                        lifecycle_events=tuple(budget_events),
                    )
                if now - budget_breach_mono >= spec.codex_tool_call_budget_grace_sec:
                    completed = self._completed_result_if_ready(
                        proc,
                        spec,
                        started_wall,
                        terminate=True,
                        cancel_requested=cancel_requested,
                    )
                    if completed is not None:
                        return replace(completed, lifecycle_events=tuple(budget_events))
                    terminate_spawned_process(proc)
                    return self._stopped_result(
                        proc,
                        "tool_call_budget",
                        f"{budget_breach_message}; grace of "
                        f"{spec.codex_tool_call_budget_grace_sec}s expired without a "
                        "terminal handoff",
                        lifecycle_events=tuple(budget_events),
                    )
            if budget_scan.violation is not None:
                terminate_spawned_process(proc)
                return self._stopped_result(
                    proc,
                    "tool_call_budget",
                    budget_scan.violation,
                    lifecycle_events=tuple(budget_events),
                )

            if not spec.read_only and spec.codex_invalid_verification_grace_sec > 0:
                invalid_result_state = inspect_result(spec.result_path, started_wall)
                if (
                    invalid_result_state.done
                    and invalid_result_state.verification_state == "invalid"
                ):
                    marker = _invalid_verification_marker(spec, invalid_result_state)
                    if (
                        invalid_verification_breach_mono is None
                        or marker != invalid_verification_marker
                    ):
                        invalid_verification_breach_mono = now
                        invalid_verification_marker = marker
                    elif (
                        now - invalid_verification_breach_mono
                        >= spec.codex_invalid_verification_grace_sec
                    ):
                        terminate_spawned_process(proc)
                        return replace(
                            self._stopped_result(
                                proc,
                                "invalid_verification",
                                _invalid_verification_message(
                                    invalid_result_state,
                                    spec.codex_invalid_verification_grace_sec,
                                ),
                            ),
                            lifecycle_events=tuple(budget_events),
                        )
                else:
                    invalid_verification_breach_mono = None
                    invalid_verification_marker = None

            (
                terminal_result,
                last_tool_timeout_scan_size,
                tool_timeout_count,
                last_forbidden_tool_scan_size,
            ) = self._terminal_result_if_ready(
                proc=proc,
                spec=spec,
                started_wall=started_wall,
                now=now,
                deadline_mono=deadline_mono,
                last_output_mono=last_output_mono,
                last_productive_mono=last_productive_mono,
                workspace_dirty=workspace_dirty,
                last_tool_timeout_scan_size=last_tool_timeout_scan_size,
                tool_timeout_count=tool_timeout_count,
                last_forbidden_tool_scan_size=last_forbidden_tool_scan_size,
                cancel_requested=cancel_requested,
            )
            if terminal_result is not None:
                return replace(terminal_result, lifecycle_events=tuple(budget_events))

            time.sleep(0.2)

    def _terminal_result_if_ready(
        self,
        *,
        proc: subprocess.Popen[str],
        spec: AgentRunSpec,
        started_wall: float,
        now: float,
        deadline_mono: float,
        last_output_mono: float,
        last_productive_mono: float,
        workspace_dirty: bool,
        last_tool_timeout_scan_size: int,
        tool_timeout_count: int,
        last_forbidden_tool_scan_size: int,
        cancel_requested: Callable[[], bool],
    ) -> tuple[AgentRunResult | None, int, int, int]:
        completed = self._completed_result_if_ready(
            proc,
            spec,
            started_wall,
            terminate=True,
            cancel_requested=cancel_requested,
        )
        if completed is not None:
            return (
                completed,
                last_tool_timeout_scan_size,
                tool_timeout_count,
                last_forbidden_tool_scan_size,
            )

        tool_timeout, last_tool_timeout_scan_size, tool_timeout_count = (
            self._tool_timeout_result_if_needed(
                proc,
                spec,
                last_tool_timeout_scan_size,
                tool_timeout_count,
            )
        )
        if tool_timeout is not None:
            return (
                tool_timeout,
                last_tool_timeout_scan_size,
                tool_timeout_count,
                last_forbidden_tool_scan_size,
            )

        forbidden_tool, last_forbidden_tool_scan_size = self._forbidden_tool_result_if_needed(
            proc,
            spec,
            last_forbidden_tool_scan_size,
        )
        if forbidden_tool is not None:
            return (
                forbidden_tool,
                last_tool_timeout_scan_size,
                tool_timeout_count,
                last_forbidden_tool_scan_size,
            )

        if cancel_requested():
            terminate_spawned_process(proc)
            return (
                self._stopped_result(proc, "cancelled", "Cancel requested"),
                last_tool_timeout_scan_size,
                tool_timeout_count,
                last_forbidden_tool_scan_size,
            )

        exited = self._exited_result_if_dead(proc, spec, started_wall)
        if exited is not None:
            return (
                exited,
                last_tool_timeout_scan_size,
                tool_timeout_count,
                last_forbidden_tool_scan_size,
            )

        stopped = self._timeout_result_if_needed(
            proc,
            spec,
            now,
            deadline_mono,
            last_output_mono,
            last_productive_mono,
            workspace_dirty,
        )
        return (
            stopped,
            last_tool_timeout_scan_size,
            tool_timeout_count,
            last_forbidden_tool_scan_size,
        )

    def _tool_timeout_result_if_needed(
        self,
        proc: TerminableProcess,
        spec: AgentRunSpec,
        scan_size: int,
        timeout_count: int,
    ) -> tuple[AgentRunResult | None, int, int]:
        triggered, next_scan_size, timeout_count = scan_tool_timeouts(
            spec.log_path,
            scan_size,
            timeout_count,
            spec.codex_tool_timeout_limit,
        )
        if not triggered:
            return None, next_scan_size, timeout_count

        terminate_spawned_process(proc)
        return (
            self._stopped_result(
                proc,
                "tool_timeout",
                "Codex tool calls repeatedly hit "
                f"{CODEX_TOOL_TIMEOUT_MARKER}; stopping after "
                f"{timeout_count} occurrences (limit {spec.codex_tool_timeout_limit}) "
                "instead of continuing without a result",
            ),
            next_scan_size,
            timeout_count,
        )

    def _forbidden_tool_result_if_needed(
        self,
        proc: TerminableProcess,
        spec: AgentRunSpec,
        scan_size: int,
    ) -> tuple[AgentRunResult | None, int]:
        match, next_scan_size = scan_forbidden_tool(
            spec.log_path,
            scan_size,
            spec.codex_forbidden_tool_markers,
        )
        if match is None:
            return None, next_scan_size

        name, marker = match
        terminate_spawned_process(proc)
        return (
            self._stopped_result(
                proc,
                "forbidden_tool_usage",
                f"Codex used forbidden tool marker {name}: {marker!r}",
            ),
            next_scan_size,
        )

    def _completed_result_if_ready(
        self,
        proc: subprocess.Popen[str],
        spec: AgentRunSpec,
        started_wall: float,
        *,
        terminate: bool,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AgentRunResult | None:
        result_state = inspect_result(spec.result_path, started_wall)
        if not result_state.done or not self._valid_terminal_handoff(
            spec, result_state.verification_state
        ):
            return None
        terminal_signature, _ = progress_signature(spec)
        if terminate:
            if self._await_completed_process(proc, spec, cancel_requested):
                return self._stopped_result(proc, "cancelled", "Cancel requested")
            final_signature, _ = progress_signature(spec)
            if final_signature != terminal_signature:
                # A trailing edit (typically a formatter/linter auto-fix that runs after the
                # handoff was written) can change the tree without invalidating the result.
                # Only fail as a late edit when the handoff is no longer a valid completion.
                post_state = inspect_result(spec.result_path, started_wall)
                if not (
                    post_state.done
                    and self._valid_terminal_handoff(spec, post_state.verification_state)
                ):
                    return self._stopped_result(
                        proc,
                        "late_edit",
                        "Terminal handoff changed after it was observed",
                    )
        return self._completed_result(
            proc, result_state.status, result_state.escalation_classification
        )

    @staticmethod
    def _exited_result_if_dead(
        proc: subprocess.Popen[str],
        spec: AgentRunSpec,
        started_wall: float,
    ) -> AgentRunResult | None:
        exit_code = proc.poll()
        if exit_code is None:
            return None
        last_message_path = spec.log_path.with_suffix(".last-message.md")
        # A process can exit a moment before its result/verification files are fully flushed
        # and observable. Re-inspect briefly before concluding there is no valid result.
        result_state = (
            recover_result_from_last_message(spec.result_path, last_message_path, started_wall)
            if spec.read_only
            else inspect_result(spec.result_path, started_wall)
        )
        for _ in range(10):
            if result_state.done and CodexProcessMonitor._valid_terminal_handoff(
                spec, result_state.verification_state
            ):
                return CodexProcessMonitor._completed_result(
                    proc, result_state.status, result_state.escalation_classification
                )
            time.sleep(0.2)
            result_state = (
                recover_result_from_last_message(spec.result_path, last_message_path, started_wall)
                if spec.read_only
                else inspect_result(spec.result_path, started_wall)
            )
        if (
            not spec.read_only
            and spec.codex_invalid_verification_grace_sec > 0
            and result_state.done
            and result_state.verification_state == "invalid"
        ):
            return AgentRunResult(
                status="invalid_verification",
                completed=False,
                exit_code=exit_code,
                result_status=None,
                message=f"verification.json invalid: {_invalid_verification_reason(result_state)}",
            )
        if contains_capacity_marker(spec.log_path, last_message_path):
            return AgentRunResult(
                status="capacity",
                completed=False,
                exit_code=exit_code,
                result_status=None,
                message="Codex capacity or usage limit was reached",
            )
        return AgentRunResult(
            status="exited_without_result",
            completed=False,
            exit_code=exit_code,
            result_status=None,
            message=result_state.reason or "codex exec exited without a valid result file",
        )

    @staticmethod
    def _timeout_result_if_needed(
        proc: TerminableProcess,
        spec: AgentRunSpec,
        now: float,
        deadline_mono: float,
        last_output_mono: float,
        last_productive_mono: float,
        workspace_dirty: bool,
    ) -> AgentRunResult | None:
        if now >= deadline_mono:
            terminate_spawned_process(proc)
            return CodexProcessMonitor._stopped_result(
                proc,
                "timeout",
                f"Timed out after {spec.timeout_sec} seconds",
            )
        if 0 < spec.idle_timeout_sec <= now - last_output_mono:
            terminate_spawned_process(proc)
            return CodexProcessMonitor._stopped_result(
                proc,
                "idle_timeout",
                f"No codex output for {spec.idle_timeout_sec} seconds",
            )
        if 0 < spec.codex_no_progress_timeout_sec <= now - last_productive_mono:
            terminate_spawned_process(proc)
            detail = "workspace is dirty" if workspace_dirty else "workspace is clean"
            return CodexProcessMonitor._stopped_result(
                proc,
                "no_progress_timeout",
                "No result/progress file update or workspace file changes for "
                f"{spec.codex_no_progress_timeout_sec} seconds ({detail})",
            )
        return None

    @staticmethod
    def _valid_terminal_handoff(spec: AgentRunSpec, verification_state: str | None) -> bool:
        return spec.read_only or verification_state == "valid"

    @staticmethod
    def _completed_result(
        proc: TerminableProcess,
        result_status: str | None,
        escalation_classification: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            completed=True,
            exit_code=proc.poll(),
            result_status=result_status,
            message=f"Result file completed with status {result_status}",
            escalation_classification=escalation_classification,
        )

    @staticmethod
    def _stopped_result(
        proc: TerminableProcess,
        status: str,
        message: str,
        *,
        lifecycle_events: tuple[BudgetLifecycleEvent, ...] = (),
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            completed=False,
            exit_code=proc.poll(),
            result_status=None,
            message=message,
            lifecycle_events=lifecycle_events,
        )

    def _await_completed_process(
        self,
        proc: TerminableProcess,
        spec: AgentRunSpec,
        cancel_requested: Callable[[], bool] | None,
    ) -> bool:
        deadline = time.monotonic() + CODEX_COMPLETION_GRACE_SEC
        event_log_path = spec.log_path.with_suffix(".events.jsonl")
        while proc.poll() is None:
            if cancel_requested is not None and cancel_requested():
                terminate_spawned_process(proc)
                return True
            if self._turn_completed(event_log_path):
                try:
                    proc.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    terminate_spawned_process(proc)
                return False
            if time.monotonic() >= deadline:
                terminate_spawned_process(proc)
                return False
            time.sleep(0.1)
        return False


def _optional_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _invalid_verification_marker(
    spec: AgentRunSpec, result_state: ResultState
) -> tuple[float | None, float | None, str | None]:
    return (
        _optional_mtime(verification_path_for_result(spec.result_path)),
        _optional_mtime(spec.result_path),
        result_state.verification_error,
    )


def _invalid_verification_reason(result_state: ResultState) -> str:
    return result_state.verification_error or "verification.json failed validation"


def _invalid_verification_message(result_state: ResultState, grace_sec: int) -> str:
    return (
        f"verification.json invalid: {_invalid_verification_reason(result_state)}; "
        f"grace of {grace_sec}s expired without a valid verification.json"
    )


def terminate_spawned_process(proc: TerminableProcess) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            return
