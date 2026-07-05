from __future__ import annotations

import os
import subprocess  # nosec B404
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec
from agent_control_plane.shared.git_tools import GitError, workspace_state

CODEX_SPARK_DISABLED_FEATURES = ("image_generation",)
CODEX_TOOL_TIMEOUT_LIMIT = 2
CODEX_TOOL_TIMEOUT_MARKER = "Exit code: 124"
CODEX_FORBIDDEN_TOOL_MARKERS_BY_NAME: dict[str, str] = {
    "web_search": "\nweb search:",
    "raw_exec": "\nexec\n",
    "codex_list_mcp_resources": "mcp: codex/list_mcp_resources",
    "codex_list_mcp_resource_templates": "mcp: codex/list_mcp_resource_templates",
}
CODEX_PRODUCTIVE_LOG_MARKERS = ("mcp: agentbridge-ide/",)


class _TerminableProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class CodexExecRunner:
    def run(
        self,
        spec: AgentRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        pid_observed: Callable[[int | None], None],
    ) -> AgentRunResult:
        command = self._build_command(spec)
        started_wall = time.time()
        started_mono = time.monotonic()
        deadline_mono = started_mono + spec.timeout_sec
        last_output_mono = started_mono
        last_log_size = 0
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        with spec.log_path.open("w", encoding="utf-8", errors="replace") as log:
            last_message_path = spec.log_path.with_suffix(".last-message.md")
            log.write("# codex exec run\n")
            log.write(f"workspace: {spec.workspace_path}\n")
            log.write(f"model: {spec.codex_model}\n")
            log.write(f"reasoning_effort: {spec.codex_reasoning_effort}\n")
            log.write(f"last_message: {last_message_path}\n")
            log.write(f"command: {subprocess.list2cmdline(command)}\n\n")
            log.flush()

            try:
                proc = subprocess.Popen(  # nosec B603
                    command,
                    cwd=str(spec.workspace_path),
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                return AgentRunResult(
                    status="blocked",
                    completed=False,
                    exit_code=None,
                    result_status=None,
                    message=f"Failed to spawn codex exec: {exc}",
                )

            pid_observed(proc.pid)
            self._send_prompt(proc, spec.prompt, log)
            return self._monitor_process(
                proc,
                spec,
                started_wall,
                deadline_mono,
                last_output_mono,
                last_log_size,
                log,
                cancel_requested,
            )

    @staticmethod
    def _build_command(spec: AgentRunSpec) -> list[str]:
        last_message_path = spec.log_path.with_suffix(".last-message.md")
        command = [
            spec.codex_command,
            "exec",
            "--model",
            spec.codex_model,
            "-c",
            f'model_reasoning_effort="{spec.codex_reasoning_effort}"',
            "-c",
            'approval_policy="never"',
        ]
        for feature_name in CODEX_SPARK_DISABLED_FEATURES:
            command.extend(["--disable", feature_name])
        for server_name in spec.codex_disabled_mcp_servers:
            command.extend(["-c", f"mcp_servers.{server_name}.enabled=false"])
        command.extend(
            [
                "--cd",
                str(spec.workspace_path),
                "--output-last-message",
                str(last_message_path),
            ]
        )
        if spec.yolo:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(
                [
                    "--sandbox",
                    "read-only" if spec.read_only else spec.codex_sandbox_mode,
                ]
            )
        command.append("-")
        return command

    def _monitor_process(
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
        progress_signature, workspace_dirty = self._progress_signature(spec)

        while True:
            log.flush()
            last_output_mono, last_log_size = self._refresh_log_activity(
                spec,
                last_output_mono,
                last_log_size,
            )

            now = time.monotonic()
            productive_log_seen, last_productive_log_scan_size = (
                self._productive_log_activity_if_needed(
                    spec,
                    last_productive_log_scan_size,
                )
            )
            if productive_log_seen:
                last_productive_mono = now
            if now - last_progress_check_mono >= 2.0:
                last_progress_check_mono = now
                next_signature, workspace_dirty = self._progress_signature(spec)
                if next_signature != progress_signature:
                    progress_signature = next_signature
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

    @staticmethod
    def _send_prompt(proc: subprocess.Popen[str], prompt: str, log: TextIO) -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(prompt)
            if not prompt.endswith("\n"):
                proc.stdin.write("\n")
            proc.stdin.close()
        except OSError as exc:
            log.write(f"\n[failed to send prompt to codex stdin: {exc}]\n")
            log.flush()

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
            self._terminate_spawned(proc)
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

    @staticmethod
    def _refresh_log_activity(
        spec: AgentRunSpec,
        last_output_mono: float,
        last_log_size: int,
    ) -> tuple[float, int]:
        try:
            current_size = spec.log_path.stat().st_size
        except OSError:
            return last_output_mono, last_log_size
        if current_size != last_log_size:
            return time.monotonic(), current_size
        return last_output_mono, last_log_size

    @staticmethod
    def _productive_log_activity_if_needed(
        spec: AgentRunSpec,
        scan_size: int,
    ) -> tuple[bool, int]:
        try:
            with spec.log_path.open("rb") as handle:
                handle.seek(scan_size)
                chunk = handle.read()
                next_scan_size = handle.tell()
        except OSError:
            return False, scan_size

        if not chunk:
            return False, next_scan_size

        text = chunk.decode("utf-8", errors="replace")
        return any(marker in text for marker in CODEX_PRODUCTIVE_LOG_MARKERS), next_scan_size

    def _tool_timeout_result_if_needed(
        self,
        proc: _TerminableProcess,
        spec: AgentRunSpec,
        scan_size: int,
        timeout_count: int,
    ) -> tuple[AgentRunResult | None, int, int]:
        try:
            with spec.log_path.open("rb") as handle:
                handle.seek(scan_size)
                chunk = handle.read()
                next_scan_size = handle.tell()
        except OSError:
            return None, scan_size, timeout_count

        if not chunk:
            return None, next_scan_size, timeout_count

        text = chunk.decode("utf-8", errors="replace")
        timeout_count += text.count(CODEX_TOOL_TIMEOUT_MARKER)
        if timeout_count < CODEX_TOOL_TIMEOUT_LIMIT:
            return None, next_scan_size, timeout_count

        self._terminate_spawned(proc)
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
        proc: _TerminableProcess,
        spec: AgentRunSpec,
        scan_size: int,
    ) -> tuple[AgentRunResult | None, int]:
        if not spec.codex_forbidden_tool_markers:
            return None, scan_size

        try:
            with spec.log_path.open("rb") as handle:
                handle.seek(scan_size)
                chunk = handle.read()
                next_scan_size = handle.tell()
        except OSError:
            return None, scan_size

        if not chunk:
            return None, next_scan_size

        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        for name in spec.codex_forbidden_tool_markers:
            marker = CODEX_FORBIDDEN_TOOL_MARKERS_BY_NAME.get(name, name)
            if marker not in text:
                continue
            self._terminate_spawned(proc)
            return (
                self._stopped_result(
                    proc,
                    "forbidden_tool_usage",
                    f"Codex used forbidden tool marker {name}: {marker!r}",
                ),
                next_scan_size,
            )
        return None, next_scan_size

    @staticmethod
    def _progress_signature(spec: AgentRunSpec) -> tuple[tuple[str, ...] | None, bool]:
        """Return durable progress markers and whether target workspace is dirty."""
        progress_path = spec.result_path.parent / "agent-progress.md"
        markers: list[str] = []
        for path in (progress_path, spec.result_path):
            try:
                stat = path.stat()
            except OSError:
                continue
            markers.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")

        workspace_dirty = False
        try:
            state = workspace_state(spec.workspace_path)
            workspace_dirty = state.dirty
            if state.porcelain.strip():
                markers.append(f"git-status:{state.porcelain}")
                markers.extend(
                    dirty_file_markers_from_porcelain(spec.workspace_path, state.porcelain)
                )
        except GitError as exc:
            markers.append(f"git-error:{type(exc).__name__}:{exc}")

        signature = tuple(markers) if markers else None
        return signature, workspace_dirty

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
            self._terminate_spawned(proc)
        return self._completed_result(proc, result_state.status)

    def _exited_result_if_dead(
        self,
        proc: subprocess.Popen[str],
        spec: AgentRunSpec,
        started_wall: float,
    ) -> AgentRunResult | None:
        exit_code = proc.poll()
        if exit_code is None:
            return None
        result_state = inspect_result(spec.result_path, started_wall)
        if result_state.done:
            return self._completed_result(proc, result_state.status)
        return AgentRunResult(
            status="exited_without_result",
            completed=False,
            exit_code=exit_code,
            result_status=None,
            message=result_state.reason or "codex exec exited without a valid result file",
        )

    def _timeout_result_if_needed(
        self,
        proc: _TerminableProcess,
        spec: AgentRunSpec,
        now: float,
        deadline_mono: float,
        last_output_mono: float,
        last_productive_mono: float,
        workspace_dirty: bool,
    ) -> AgentRunResult | None:
        if now >= deadline_mono:
            self._terminate_spawned(proc)
            return self._stopped_result(
                proc,
                "timeout",
                f"Timed out after {spec.timeout_sec} seconds",
            )
        if 0 < spec.idle_timeout_sec <= now - last_output_mono:
            self._terminate_spawned(proc)
            return self._stopped_result(
                proc,
                "idle_timeout",
                f"No codex output for {spec.idle_timeout_sec} seconds",
            )
        if 0 < spec.codex_no_progress_timeout_sec <= now - last_productive_mono:
            self._terminate_spawned(proc)
            detail = "workspace is dirty" if workspace_dirty else "workspace is clean"
            return self._stopped_result(
                proc,
                "no_progress_timeout",
                "No result/progress file update or workspace file changes for "
                f"{spec.codex_no_progress_timeout_sec} seconds ({detail})",
            )
        return None

    @staticmethod
    def _completed_result(proc: _TerminableProcess, result_status: str | None) -> AgentRunResult:
        return AgentRunResult(
            status="completed",
            completed=True,
            exit_code=proc.poll(),
            result_status=result_status,
            message=f"Result file completed with status {result_status}",
        )

    @staticmethod
    def _stopped_result(proc: _TerminableProcess, status: str, message: str) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            completed=False,
            exit_code=proc.poll(),
            result_status=None,
            message=message,
        )

    @staticmethod
    def _terminate_spawned(proc: _TerminableProcess) -> None:
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


def dirty_file_markers_from_porcelain(workspace_path: Path, porcelain: str) -> list[str]:
    markers: list[str] = []
    for line in porcelain.splitlines():
        path_text = porcelain_changed_path(line)
        if not path_text:
            continue
        path = workspace_path / path_text
        try:
            stat = path.stat()
        except OSError:
            continue
        markers.append(f"dirty-file:{path_text}:{stat.st_mtime_ns}:{stat.st_size}")
    return markers


def porcelain_changed_path(line: str) -> str | None:
    if len(line) < 4:
        return None
    path_text = line[3:]
    if " -> " in path_text:
        path_text = path_text.rsplit(" -> ", maxsplit=1)[-1]
    return path_text.strip().strip('"') or None


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )
