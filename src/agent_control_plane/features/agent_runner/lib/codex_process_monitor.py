from __future__ import annotations

import subprocess  # nosec B404
import time
from collections.abc import Callable
from typing import Protocol, TextIO

from agent_control_plane.features.agent_runner.lib.codex_telemetry import codex_turn_completed
from agent_control_plane.features.agent_runner.lib.codex_watchdog import (
    CODEX_TOOL_TIMEOUT_MARKER,
    productive_log_activity_if_needed,
    progress_signature,
    refresh_log_activity,
    scan_forbidden_tool,
    scan_tool_timeouts,
)
from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec

CODEX_COMPLETION_GRACE_SEC = 60.0


class TerminableProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class CodexProcessMonitor:
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
        current_progress_signature, workspace_dirty = progress_signature(spec)

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
                    last_productive_mono = now

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
                return terminal_result

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
        completed = self._completed_result_if_ready(proc, spec, started_wall, terminate=True)
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
                f"{timeout_count} occurrences instead of continuing without a result",
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
    ) -> AgentRunResult | None:
        result_state = inspect_result(spec.result_path, started_wall)
        if not result_state.done:
            return None
        if terminate:
            self._await_completed_process(proc, spec)
        return self._completed_result(proc, result_state.status)

    @staticmethod
    def _exited_result_if_dead(
        proc: subprocess.Popen[str],
        spec: AgentRunSpec,
        started_wall: float,
    ) -> AgentRunResult | None:
        exit_code = proc.poll()
        if exit_code is None:
            return None
        result_state = inspect_result(spec.result_path, started_wall)
        if result_state.done:
            return CodexProcessMonitor._completed_result(proc, result_state.status)
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
    def _completed_result(
        proc: TerminableProcess,
        result_status: str | None,
    ) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            completed=True,
            exit_code=proc.poll(),
            result_status=result_status,
            message=f"Result file completed with status {result_status}",
        )

    @staticmethod
    def _stopped_result(
        proc: TerminableProcess,
        status: str,
        message: str,
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            completed=False,
            exit_code=proc.poll(),
            result_status=None,
            message=message,
        )

    @staticmethod
    def _await_completed_process(
        proc: TerminableProcess,
        spec: AgentRunSpec,
    ) -> None:
        deadline = time.monotonic() + CODEX_COMPLETION_GRACE_SEC
        event_log_path = spec.log_path.with_suffix(".events.jsonl")
        while proc.poll() is None:
            if codex_turn_completed(event_log_path):
                try:
                    proc.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    terminate_spawned_process(proc)
                return
            if time.monotonic() >= deadline:
                terminate_spawned_process(proc)
                return
            time.sleep(0.1)


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
