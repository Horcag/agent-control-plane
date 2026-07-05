from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable
from typing import Protocol, TextIO

from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b].*?(?:\x07|\x1b\\)")
TRUST_PROMPT_TAIL_LIMIT = 12_000


class PtyProcessLike(Protocol):
    pid: int | None
    exitstatus: int | None

    def read(self, size: int) -> str: ...

    def isalive(self) -> bool: ...

    def terminate(self, force: bool = False) -> None: ...


AgyRunSpec = AgentRunSpec
AgyRunResult = AgentRunResult


class PtyAgyRunner:
    def run(
        self,
        spec: AgyRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        pid_observed: Callable[[int | None], None],
    ) -> AgyRunResult:
        try:
            from winpty import PtyProcess  # type: ignore[import-untyped]
        except ImportError as exc:
            return AgyRunResult(
                status="blocked",
                completed=False,
                exit_code=None,
                result_status=None,
                message=f"pywinpty is not installed or cannot be imported: {exc}",
            )

        command = self._build_command(spec)
        output_queue: queue.Queue[str] = queue.Queue()
        started_wall = time.time()
        started_mono = time.monotonic()
        deadline_mono = started_mono + spec.timeout_sec
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        with spec.log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write("# agy ConPTY run\n")
            log.write(f"workspace: {spec.workspace_path}\n")
            log.write(f"command: {self._display_command(spec)}\n\n")
            log.flush()

            try:
                proc = PtyProcess.spawn(
                    command,
                    cwd=str(spec.workspace_path),
                    dimensions=(40, 180),
                )
            except Exception as exc:  # noqa: BLE001 - ConPTY spawn errors vary by backend
                return AgyRunResult(
                    status="blocked",
                    completed=False,
                    exit_code=None,
                    result_status=None,
                    message=f"Failed to spawn agy through ConPTY: {exc}",
                )

            pid_observed(getattr(proc, "pid", None))
            reader = threading.Thread(target=self._reader, args=(proc, output_queue), daemon=True)
            reader.start()

            return self._monitor_process(
                proc,
                output_queue,
                log,
                spec,
                started_wall,
                started_mono,
                deadline_mono,
                cancel_requested,
            )

    def _monitor_process(
        self,
        proc: PtyProcessLike,
        output_queue: queue.Queue[str],
        log: TextIO,
        spec: AgyRunSpec,
        started_wall: float,
        last_output_mono: float,
        deadline_mono: float,
        cancel_requested: Callable[[], bool],
    ) -> AgyRunResult:
        output_tail = ""
        while True:
            drained_output = self._drain_output(output_queue, log)
            if drained_output:
                last_output_mono = time.monotonic()
                output_tail = (output_tail + drained_output)[-TRUST_PROMPT_TAIL_LIMIT:]

            completed = self._completed_result_if_ready(proc, spec, started_wall, terminate=True)
            if completed is not None:
                return completed

            trust_prompt_message = self._trust_prompt_message_if_needed(output_tail)
            if trust_prompt_message is not None:
                self._terminate_spawned(proc)
                return self._stopped_result(proc, "blocked", trust_prompt_message)

            if cancel_requested():
                self._terminate_spawned(proc)
                return self._stopped_result(proc, "cancelled", "Cancel requested")

            exited = self._exited_result_if_dead(proc, output_queue, log, spec, started_wall)
            if exited is not None:
                return exited

            now = time.monotonic()
            stopped = self._timeout_result_if_needed(
                proc, spec, now, deadline_mono, last_output_mono
            )
            if stopped is not None:
                return stopped

            time.sleep(0.2)

    @staticmethod
    def _build_command(spec: AgyRunSpec) -> list[str]:
        command = [spec.agy_command]
        if spec.yolo:
            command.append("--dangerously-skip-permissions")
        command.extend(["--print", spec.prompt, "--print-timeout", spec.print_timeout])
        return command

    @staticmethod
    def _display_command(spec: AgyRunSpec) -> str:
        command = [spec.agy_command]
        if spec.yolo:
            command.append("--dangerously-skip-permissions")
        command.extend(["--print", "<prompt>", "--print-timeout", spec.print_timeout])
        return " ".join(command)

    def _completed_result_if_ready(
        self,
        proc: PtyProcessLike,
        spec: AgyRunSpec,
        started_wall: float,
        *,
        terminate: bool,
    ) -> AgyRunResult | None:
        result_state = inspect_result(spec.result_path, started_wall)
        if not result_state.done:
            return None
        if terminate:
            self._terminate_spawned(proc)
        return self._completed_result(proc, result_state.status)

    def _exited_result_if_dead(
        self,
        proc: PtyProcessLike,
        output_queue: queue.Queue[str],
        log: TextIO,
        spec: AgyRunSpec,
        started_wall: float,
    ) -> AgyRunResult | None:
        if proc.isalive():
            return None
        self._drain_output(output_queue, log)
        result_state = inspect_result(spec.result_path, started_wall)
        if result_state.done:
            return self._completed_result(proc, result_state.status)
        return AgyRunResult(
            status="exited_without_result",
            completed=False,
            exit_code=proc.exitstatus,
            result_status=None,
            message=result_state.reason or "agy exited without a valid result file",
        )

    def _timeout_result_if_needed(
        self,
        proc: PtyProcessLike,
        spec: AgyRunSpec,
        now: float,
        deadline_mono: float,
        last_output_mono: float,
    ) -> AgyRunResult | None:
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
                f"No terminal output for {spec.idle_timeout_sec} seconds",
            )
        return None

    @staticmethod
    def _completed_result(proc: PtyProcessLike, result_status: str | None) -> AgyRunResult:
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=proc.exitstatus,
            result_status=result_status,
            message=f"Result file completed with status {result_status}",
        )

    @staticmethod
    def _stopped_result(proc: PtyProcessLike, status: str, message: str) -> AgyRunResult:
        return AgyRunResult(
            status=status,
            completed=False,
            exit_code=proc.exitstatus,
            result_status=None,
            message=message,
        )

    @staticmethod
    def _reader(proc: PtyProcessLike, output_queue: queue.Queue[str]) -> None:
        while True:
            try:
                chunk = proc.read(4096)
            except Exception as exc:  # noqa: BLE001 - pywinpty reader exceptions are backend-specific
                output_queue.put(f"\n[pty reader stopped: {exc}]\n")
                return
            if not chunk:
                output_queue.put("\n[pty reader stopped: empty read]\n")
                return
            output_queue.put(chunk)

    @staticmethod
    def _drain_output(output_queue: queue.Queue[str], log: TextIO) -> str:
        drained: list[str] = []
        while True:
            try:
                chunk = output_queue.get_nowait()
            except queue.Empty:
                break
            text = ANSI_RE.sub("", chunk)
            if text:
                log.write(text)
                log.flush()
                drained.append(text)
        return "".join(drained)

    @staticmethod
    def _trust_prompt_message_if_needed(output_tail: str) -> str | None:
        normalized = " ".join(output_tail.lower().split())
        trust_question = "do you trust the contents of this project?"
        permission_text = "antigravity cli requires permission"
        if trust_question not in normalized or permission_text not in normalized:
            return None
        return (
            "Antigravity CLI is waiting for the workspace trust prompt. "
            "Background orchestration cannot answer this interactive prompt; "
            "trust the workspace once before starting an agent-control job."
        )

    @staticmethod
    def _terminate_spawned(proc: PtyProcessLike) -> None:
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except (OSError, RuntimeError):
            return
