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

from agent_control_plane.features.agent_runner.lib.agy_output import (
    AgyOutput,
    private_file,
    vendor_log_path,
)
from agent_control_plane.features.agent_runner.lib.codex_watchdog import progress_signature
from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec
from agent_control_plane.features.agent_runner.lib.windows_pipe import WindowsPipeProcess
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
READER_JOIN_TIMEOUT_SEC = 0.25
READER_QUEUE_WAIT_SEC = 0.05
VENDOR_LOG_LIMIT = 8 * 1024 * 1024


class PtyProcessLike(Protocol):
    pid: int | None

    @property
    def exitstatus(self) -> int | None: ...

    def read(self, size: int) -> str: ...

    def isalive(self) -> bool: ...

    def terminate(self, force: bool = False) -> None: ...


class PtyProcessFactory(Protocol):
    """The spawn factory shared by POSIX PTY and Windows structured pipes.

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
    """Return the configured platform transport, preserving dependency preflight.

    Windows print-mode uses pipes because ConPTY adds VT bytes to NDJSON and
    can keep its reader blocked after child exit. Keep the declared pywinpty
    availability check for existing Windows environments. POSIX continues to
    use ptyprocess's Unicode PTY backend. Both expose the same spawn/read/exit
    contract; unexpected read errors never count as clean EOF.
    """
    distribution = "pywinpty" if os.name == "nt" else "ptyprocess"
    try:
        module = (
            importlib.import_module("winpty")
            if os.name == "nt"
            else importlib.import_module("ptyprocess")
        )
    except ImportError as exc:
        raise ImportError(f"{distribution} is not installed or cannot be imported: {exc}") from exc
    if os.name == "nt":
        return WindowsPipeProcess
    factory: PtyProcessFactory = module.PtyProcessUnicode
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
        output_queue: queue.Queue[str] = queue.Queue(maxsize=256)
        started_wall = time.time()
        started_mono = time.monotonic()
        deadline_mono = started_mono + spec.timeout_sec
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        private_file(vendor_log_path(spec.log_path))
        private_file(spec.log_path)
        capture = AgyOutput(
            spec.log_path, spec.agy_launch.job_id if spec.agy_launch else spec.log_path.parent.name
        )

        with spec.log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write("# agy structured stream run\n")
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
                capture.finish(clean=False)
                return AgyRunResult(
                    status="blocked",
                    completed=False,
                    exit_code=None,
                    result_status=None,
                    message=f"Failed to spawn agy stream: {type(exc).__name__}",
                )

            pid_observed(getattr(proc, "pid", None))
            reader_done = threading.Event()
            reader_stop = threading.Event()
            reader_errors: queue.Queue[str] = queue.Queue()
            reader = threading.Thread(
                target=self._reader,
                args=(proc, output_queue, reader_done, reader_errors, reader_stop),
                daemon=True,
            )
            reader.start()

            run_result: AgyRunResult | None = None
            try:
                run_result = self._monitor_process(
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
                    capture,
                )
                return run_result
            finally:
                reader_stop.set()
                self._terminate_spawned(proc)
                reader.join(READER_JOIN_TIMEOUT_SEC)
                if isinstance(proc, WindowsPipeProcess) and not reader.is_alive():
                    proc.close()
                if not capture.closed:
                    if reader.is_alive():
                        capture.invalid = True
                    with suppress(queue.Empty):
                        if reader_errors.get_nowait() != "PTY reader stopped before clean EOF":
                            capture.invalid = True
                    capture.finish(
                        clean=False, controller_stopped=bool(run_result and run_result.completed)
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
        capture: AgyOutput,
    ) -> AgyRunResult:
        output_tail = ""
        progress_sequence = capture.progress_sequence
        durable_signature, _ = progress_signature(spec)
        next_progress_poll_mono = last_activity_mono + DURABLE_PROGRESS_POLL_INTERVAL_SEC
        while True:
            vendor_path = vendor_log_path(spec.log_path)
            try:
                vendor_size = vendor_path.stat().st_size
            except OSError:
                self._terminate_spawned(proc)
                return self._stopped_result(
                    proc, "blocked", "AGY private vendor log is unavailable"
                )
            if vendor_size > VENDOR_LOG_LIMIT:
                self._terminate_spawned(proc)
                if proc.isalive():
                    return self._stopped_result(
                        proc,
                        "blocked",
                        "AGY private vendor log exceeded its size limit; child exit unverified, log preserved",
                    )
                try:
                    with vendor_path.open("r+b") as vendor:
                        vendor.truncate(VENDOR_LOG_LIMIT)
                except OSError:
                    return self._stopped_result(
                        proc, "blocked", "AGY private vendor log size cleanup failed"
                    )
                return self._stopped_result(
                    proc, "blocked", "AGY private vendor log exceeded its size limit"
                )
            drained_output = self._drain_output(output_queue, log, capture)
            if drained_output:
                output_tail = capture.diagnostic_tail
            if capture.progress_sequence != progress_sequence:
                last_activity_mono = time.monotonic()
                progress_sequence = capture.progress_sequence

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
                proc,
                output_queue,
                log,
                spec,
                started_wall,
                output_tail,
                reader_done,
                reader_errors,
                capture,
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
            if 0 < spec.idle_timeout_sec <= now - last_activity_mono and capture.has_inflight_turn:
                capture.note_quiet(log)
            stopped = self._timeout_result_if_needed(
                proc,
                spec,
                now,
                deadline_mono,
                last_activity_mono,
                active_operation=capture.has_inflight_turn,
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
        command.extend(
            [
                "--print",
                spec.prompt,
                "--print-timeout",
                spec.print_timeout,
                "--output-format",
                "stream-json",
                "--log-file",
                str(vendor_log_path(spec.log_path)),
            ]
        )
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
        command.extend(
            [
                "--print",
                "<prompt>",
                "--print-timeout",
                spec.print_timeout,
                "--output-format",
                "stream-json",
                "--log-file",
                str(vendor_log_path(spec.log_path)),
            ]
        )
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
        capture: AgyOutput | None = None,
    ) -> AgyRunResult | None:
        if proc.isalive():
            return None
        # Drain while waiting: a bounded producer queue may otherwise prevent the
        # reader from reaching EOF even after the child has exited.
        drained, final_output = self._drain_until_reader_done(
            output_queue, log, capture, reader_done
        )
        output_tail = (
            capture.diagnostic_tail
            if capture is not None
            else (output_tail + final_output)[-(FINAL_RESPONSE_LIMIT + 1) :]
        )
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
        authoritative = (
            capture.finish(clean=drained and reader_error is None) if capture else output_tail
        )
        response = self._final_response(authoritative or "", spec.prompt)
        if (
            spec.read_only
            and proc.exitstatus == 0
            and drained
            and reader_error is None
            and response is not None
        ):
            spec.result_path.parent.mkdir(parents=True, exist_ok=True)
            spec.result_path.write_text(response, encoding="utf-8")
            log.write(response)
            log.flush()
            recovered = inspect_result(spec.result_path, started_wall)
            if recovered.done:
                return self._completed_result(proc, recovered.status)
        cli_failure_reason = capture.failure_message if capture is not None else None
        if capture is not None and authoritative is not None and response is None:
            cli_failure_reason = "AGY CLI returned SUCCESS without the required final result format"
        if capture is not None and cli_failure_reason is None:
            cli_failure_reason = (
                "AGY structured stream did not provide a single complete valid final result"
            )
        cli_failure = (
            f"{cli_failure_reason} (configured --print-timeout {spec.print_timeout}); "
            "no valid result file"
            if cli_failure_reason is not None
            else None
        )
        return AgyRunResult(
            status="exited_without_result",
            completed=False,
            exit_code=proc.exitstatus,
            result_status=None,
            message=reader_error
            or cli_failure
            or result_state.reason
            or "agy exited without a valid result file",
        )

    def _timeout_result_if_needed(
        self,
        proc: PtyProcessLike,
        spec: AgyRunSpec,
        now: float,
        deadline_mono: float,
        last_activity_mono: float,
        *,
        active_operation: bool = False,
    ) -> AgyRunResult | None:
        if now >= deadline_mono:
            self._terminate_spawned(proc)
            return self._stopped_result(
                proc,
                "timeout",
                f"Timed out after {spec.timeout_sec} seconds",
            )
        if not active_operation and 0 < spec.idle_timeout_sec <= now - last_activity_mono:
            self._terminate_spawned(proc)
            return self._stopped_result(
                proc,
                "idle_timeout",
                (f"No verified AGY stream or durable progress for {spec.idle_timeout_sec} seconds"),
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
        stop_requested: threading.Event | None = None,
    ) -> None:
        try:
            error = PtyAgyRunner._read_output(proc, output_queue, stop_requested)
            if error is not None:
                reader_errors.put(error)
        finally:
            reader_done.set()

    @staticmethod
    def _read_output(
        proc: PtyProcessLike,
        output_queue: queue.Queue[str],
        stop_requested: threading.Event | None = None,
    ) -> str | None:
        while True:
            if stop_requested is not None and stop_requested.is_set():
                return "PTY reader stopped before clean EOF"
            try:
                chunk = proc.read(4096)
            except EOFError:
                return None
            except Exception as exc:  # noqa: BLE001 - report unexpected backend read failures
                error = f"PTY reader failed: {type(exc).__name__}"
                return error
            if not chunk:
                try:
                    eof = getattr(proc, "eof", None)
                    if not callable(eof) or eof() is not False:
                        return None
                except Exception as exc:  # noqa: BLE001 - EOF checks are backend operations
                    return f"PTY reader failed: {type(exc).__name__}"
                # pywinpty can return an empty poll before its socket reaches EOF.
                if stop_requested is None:
                    time.sleep(READER_QUEUE_WAIT_SEC)
                elif stop_requested.wait(READER_QUEUE_WAIT_SEC):
                    return "PTY reader stopped before clean EOF"
                continue
            while True:
                if stop_requested is not None and stop_requested.is_set():
                    return "PTY reader stopped before clean EOF"
                try:
                    output_queue.put(chunk, timeout=READER_QUEUE_WAIT_SEC)
                    break
                except queue.Full:
                    continue

    @staticmethod
    def _drain_until_reader_done(
        output_queue: queue.Queue[str],
        log: TextIO,
        capture: AgyOutput | None,
        reader_done: threading.Event | None,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + READER_DRAIN_TIMEOUT_SEC
        tail = ""
        while True:
            tail = (tail + PtyAgyRunner._drain_output(output_queue, log, capture))[
                -(FINAL_RESPONSE_LIMIT + 1) :
            ]
            if reader_done is None:
                return False, tail
            if reader_done.is_set() and output_queue.empty():
                return True, tail
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, tail
            reader_done.wait(min(0.01, remaining))

    @staticmethod
    def _drain_output(
        output_queue: queue.Queue[str], log: TextIO, capture: AgyOutput | None = None
    ) -> str:
        drained: list[str] = []
        for _ in range(256):
            try:
                chunk = output_queue.get_nowait()
            except queue.Empty:
                break
            if capture is not None:
                capture.feed(chunk, log)
                # Liveness marker only; diagnostics are classified by the parser.
                drained.append("[agy stream data]")
                continue
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
