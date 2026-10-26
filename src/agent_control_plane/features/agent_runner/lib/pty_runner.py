from __future__ import annotations

import importlib
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol, TextIO

from agent_control_plane.features.agent_runner.lib.codex_watchdog import progress_signature
from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec
from agent_control_plane.shared.result_envelope import (
    RESULT_ENVELOPE_SECTION_PATTERN,
    RESULT_ENVELOPE_SECTIONS,
    result_envelope_section_key,
)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b].*?(?:\x07|\x1b\\)")
TRUST_PROMPT_TAIL_LIMIT = 12_000
DURABLE_PROGRESS_POLL_INTERVAL_SEC = 2.0
FINAL_RESPONSE_LIMIT = 64_000
READER_DRAIN_TIMEOUT_SEC = 1.0


class PtyProcessLike(Protocol):
    pid: int | None
    exitstatus: int | None

    def read(self, size: int) -> str: ...

    def isalive(self) -> bool: ...

    def terminate(self, force: bool = False) -> None: ...


class PtyProcessFactory(Protocol):
    """The class object a PTY backend exposes, as opposed to one of its processes.

    `spawn` is a classmethod on both backends, so what the loader returns is a
    class rather than an instance, and `PtyProcessLike` -- which describes an
    instance -- does not cover it.
    """

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: str | None = ...,
        dimensions: tuple[int, int] = ...,
    ) -> PtyProcessLike: ...


def _load_pty_process() -> PtyProcessFactory:
    """Return the PTY backend for this platform.

    agy is a TUI: it has to be driven through a terminal, not a pipe, so the
    runner needs a real PTY on every platform it supports. Windows has one
    implementation of that (ConPTY through pywinpty) and POSIX has another
    (openpty through ptyprocess), and the two are separate packages -- neither
    installs on the other platform, which is why the dependency is split by
    marker in pyproject.

    They agree closely enough to sit behind `PtyProcessLike`: both expose
    `spawn(argv, cwd=..., dimensions=(rows, cols))`, a `read(size)` that hands
    back text, `isalive()`, `terminate(force=...)`, `pid` and `exitstatus`. The
    one thing to get right is text: ptyprocess splits bytes and str across two
    classes, so this asks for `PtyProcessUnicode`, whose `read` returns str like
    pywinpty\'s does. The reader accepts EOFError or an empty read as clean EOF;
    other backend exceptions must prevent recovery from incomplete output.
    """
    if os.name == "nt":
        attribute, distribution = "PtyProcess", "pywinpty"
    else:
        attribute, distribution = "PtyProcessUnicode", "ptyprocess"
    try:
        module = (
            importlib.import_module("winpty")
            if os.name == "nt"
            else importlib.import_module("ptyprocess")
        )
    except ImportError as exc:
        raise ImportError(f"{distribution} is not installed or cannot be imported: {exc}") from exc
    factory: PtyProcessFactory = getattr(module, attribute)
    return factory


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
            pty_process = _load_pty_process()
        except ImportError as exc:
            return AgyRunResult(
                status="blocked",
                completed=False,
                exit_code=None,
                result_status=None,
                message=str(exc),
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
                proc = pty_process.spawn(
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
            reader_done = threading.Event()
            reader_errors: queue.Queue[str] = queue.Queue()
            reader = threading.Thread(
                target=self._reader,
                args=(proc, output_queue, reader_done, reader_errors),
                daemon=True,
            )
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
                reader_done,
                reader_errors,
            )

    def _monitor_process(
        self,
        proc: PtyProcessLike,
        output_queue: queue.Queue[str],
        log: TextIO,
        spec: AgyRunSpec,
        started_wall: float,
        last_activity_mono: float,
        deadline_mono: float,
        cancel_requested: Callable[[], bool],
        reader_done: threading.Event,
        reader_errors: queue.Queue[str],
    ) -> AgyRunResult:
        output_tail = ""
        durable_signature, _ = progress_signature(spec)
        next_progress_poll_mono = last_activity_mono + DURABLE_PROGRESS_POLL_INTERVAL_SEC
        while True:
            drained_output = self._drain_output(output_queue, log)
            if drained_output:
                last_activity_mono = time.monotonic()
                output_tail = (output_tail + drained_output)[-(FINAL_RESPONSE_LIMIT + 1) :]

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

            exited = self._exited_result_if_dead(
                proc, output_queue, log, spec, started_wall, output_tail, reader_done, reader_errors
            )
            if exited is not None:
                return exited

            now = time.monotonic()
            if now >= next_progress_poll_mono:
                last_activity_mono, durable_signature = self._refresh_durable_activity(
                    spec,
                    previous_signature=durable_signature,
                    last_activity_mono=last_activity_mono,
                    now=now,
                )
                next_progress_poll_mono = now + DURABLE_PROGRESS_POLL_INTERVAL_SEC
            stopped = self._timeout_result_if_needed(
                proc, spec, now, deadline_mono, last_activity_mono
            )
            if stopped is not None:
                return stopped

            time.sleep(0.2)

    @staticmethod
    def _refresh_durable_activity(
        spec: AgyRunSpec,
        *,
        previous_signature: tuple[str, ...] | None,
        last_activity_mono: float,
        now: float,
    ) -> tuple[float, tuple[str, ...] | None]:
        current_signature, _ = progress_signature(spec)
        if current_signature is None:
            return last_activity_mono, previous_signature
        if current_signature != previous_signature:
            return now, current_signature
        return last_activity_mono, current_signature

    @staticmethod
    def _command_prefix(spec: AgyRunSpec) -> list[str]:
        executable = spec.agy_launch.executable if spec.agy_launch is not None else spec.agy_command
        if executable.endswith((".py", ".pyw")):
            return [sys.executable, executable]
        return [executable]

    @staticmethod
    def _build_command(spec: AgyRunSpec) -> list[str]:
        launch = spec.agy_launch
        command = PtyAgyRunner._command_prefix(spec)
        if launch is not None and launch.add_new_project:
            command.append("--new-project")
        if spec.yolo:
            command.append("--dangerously-skip-permissions")
        if spec.agy_model:
            command.extend(["--model", spec.agy_model])
        command.extend(["--print", spec.prompt, "--print-timeout", spec.print_timeout])
        return command

    @staticmethod
    def _display_command(spec: AgyRunSpec) -> str:
        launch = spec.agy_launch
        command = PtyAgyRunner._command_prefix(spec)
        if launch is not None and launch.add_new_project:
            command.append("--new-project")
        if spec.yolo:
            command.append("--dangerously-skip-permissions")
        if spec.agy_model:
            command.extend(["--model", spec.agy_model])
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
        output_tail: str = "",
        reader_done: threading.Event | None = None,
        reader_errors: queue.Queue[str] | None = None,
    ) -> AgyRunResult | None:
        if proc.isalive():
            return None
        # Process death can precede the reader's final queue writes. Never recover
        # from an incomplete stream, even when its prefix looks like a result.
        drained = reader_done is not None and reader_done.wait(READER_DRAIN_TIMEOUT_SEC)
        output_tail = (output_tail + self._drain_output(output_queue, log))[
            -(FINAL_RESPONSE_LIMIT + 1) :
        ]
        result_state = inspect_result(spec.result_path, started_wall)
        if result_state.done:
            return self._completed_result(proc, result_state.status)
        trust_prompt_message = self._trust_prompt_message_if_needed(output_tail)
        if trust_prompt_message is not None:
            return self._stopped_result(proc, "blocked", trust_prompt_message)
        if (
            "Eligibility check failed: Your current account is not eligible for Antigravity"
            in ANSI_RE.sub("", output_tail)
        ):
            return AgyRunResult(
                status="blocked",
                completed=False,
                exit_code=proc.exitstatus,
                result_status=None,
                message="Antigravity CLI rejected the active account at its eligibility check; verify that account before retrying. The proxy account pool cannot repair this local CLI refusal.",
            )
        reader_error = None
        if reader_errors is not None:
            with suppress(queue.Empty):
                reader_error = reader_errors.get_nowait()
        if spec.read_only and proc.exitstatus == 0 and drained and reader_error is None:
            response = self._final_response(output_tail, spec.prompt)
            if response is not None:
                spec.result_path.parent.mkdir(parents=True, exist_ok=True)
                spec.result_path.write_text(response, encoding="utf-8")
                recovered = inspect_result(spec.result_path, started_wall)
                if recovered.done:
                    return self._completed_result(proc, recovered.status)
        return AgyRunResult(
            status="exited_without_result",
            completed=False,
            exit_code=proc.exitstatus,
            result_status=None,
            message=reader_error or result_state.reason or "agy exited without a valid result file",
        )

    def _timeout_result_if_needed(
        self,
        proc: PtyProcessLike,
        spec: AgyRunSpec,
        now: float,
        deadline_mono: float,
        last_activity_mono: float,
    ) -> AgyRunResult | None:
        if now >= deadline_mono:
            self._terminate_spawned(proc)
            return self._stopped_result(
                proc,
                "timeout",
                f"Timed out after {spec.timeout_sec} seconds",
            )
        if 0 < spec.idle_timeout_sec <= now - last_activity_mono:
            self._terminate_spawned(proc)
            return self._stopped_result(
                proc,
                "idle_timeout",
                (f"No AGY terminal output or durable progress for {spec.idle_timeout_sec} seconds"),
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
    def _reader(
        proc: PtyProcessLike,
        output_queue: queue.Queue[str],
        reader_done: threading.Event,
        reader_errors: queue.Queue[str],
    ) -> None:
        try:
            error = PtyAgyRunner._read_output(proc, output_queue)
            if error is not None:
                reader_errors.put(error)
        finally:
            reader_done.set()

    @staticmethod
    def _read_output(proc: PtyProcessLike, output_queue: queue.Queue[str]) -> str | None:
        while True:
            try:
                chunk = proc.read(4096)
            except EOFError:
                output_queue.put("\n[pty reader stopped: EOF]\n")
                return None
            except Exception as exc:  # noqa: BLE001 - report unexpected backend read failures
                error = f"PTY reader failed: {type(exc).__name__}: {exc}"
                output_queue.put(f"\n[{error}]\n")
                return error
            if not chunk:
                output_queue.put("\n[pty reader stopped: empty read]\n")
                return None
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
                drained.append(chunk)
        return "".join(drained)

    @staticmethod
    def _final_response(output: str, prompt: str) -> str | None:
        if len(output) > FINAL_RESPONSE_LIMIT:
            return None
        text = ANSI_RE.sub("", output).replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
        text = re.sub(r"\n\[pty reader stopped: (?:EOF|empty read)\]$", "", text).rstrip()
        if prompt.strip() and prompt.strip() in text:
            return None
        if "```" in text or "~~~" in text:
            return None
        if not re.match(r"\AStatus: (completed|partial|blocked)\n", text):
            return None
        if len(re.findall(r"(?i)\b(?:status|статус)\s*:", text)) != 1:
            return None
        sections: list[str] = []
        content: list[str] = []
        for line in text.splitlines()[1:]:
            match = RESULT_ENVELOPE_SECTION_PATTERN.fullmatch(line)
            if match:
                if sections and not any(part.strip() for part in content):
                    return None
                sections.append(result_envelope_section_key(match.group(1)))
                content = [match.group(2)]
            elif sections:
                content.append(line)
            elif line.strip() and not re.fullmatch(r"Escalation-Classification: [a-z_]+", line):
                return None
        if sections != list(RESULT_ENVELOPE_SECTIONS) or not any(part.strip() for part in content):
            return None
        return text + "\n"

    @staticmethod
    def _trust_prompt_message_if_needed(output_tail: str) -> str | None:
        normalized = " ".join(
            ANSI_RE.sub("", output_tail[-TRUST_PROMPT_TAIL_LIMIT:]).lower().split()
        )
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
