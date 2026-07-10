from __future__ import annotations

import os
import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TextIO

from agent_control_plane.features.agent_runner.lib.codex_output import CodexOutputCapture
from agent_control_plane.features.agent_runner.lib.codex_process_monitor import (
    CodexProcessMonitor,
    terminate_spawned_process,
)
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec

CODEX_SPARK_DISABLED_FEATURES = ("image_generation",)


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

        with CodexOutputCapture(spec.log_path) as output:
            log = output.log
            if log is None:
                raise RuntimeError("Codex output log was not opened")
            last_message_path = spec.log_path.with_suffix(".last-message.md")
            log.write("# codex exec run\n")
            log.write(f"workspace: {spec.workspace_path}\n")
            log.write(f"model: {spec.codex_model}\n")
            log.write(f"reasoning_effort: {spec.codex_reasoning_effort}\n")
            log.write(f"events: {output.event_log_path}\n")
            log.write(f"last_message: {last_message_path}\n")
            log.write(f"command: {subprocess.list2cmdline(command)}\n\n")
            log.flush()

            try:
                proc = subprocess.Popen(  # nosec B603
                    command,
                    cwd=str(spec.workspace_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                result = AgentRunResult(
                    status="blocked",
                    completed=False,
                    exit_code=None,
                    result_status=None,
                    message=f"Failed to spawn codex exec: {exc}",
                )
            else:
                pid_observed(proc.pid)
                if proc.stdout is None:
                    terminate_spawned_process(proc)
                    result = AgentRunResult(
                        status="blocked",
                        completed=False,
                        exit_code=proc.poll(),
                        result_status=None,
                        message="codex exec started without a stdout pipe",
                    )
                else:
                    output.start(proc.stdout)
                    self._send_prompt(proc, spec.prompt, log)
                    result = CodexProcessMonitor().monitor(
                        proc,
                        spec,
                        started_wall,
                        deadline_mono,
                        started_mono,
                        0,
                        log,
                        cancel_requested,
                    )

            metrics = output.metrics(
                model=spec.codex_model,
                duration_sec=time.monotonic() - started_mono,
            )
            return replace(result, metrics=metrics)

    @staticmethod
    def _build_command(spec: AgentRunSpec) -> list[str]:
        last_message_path = spec.log_path.with_suffix(".last-message.md")
        command = [
            spec.codex_command,
            "exec",
            "--model",
            spec.codex_model,
            "--json",
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


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )
