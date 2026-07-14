from __future__ import annotations

import os
import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
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
                    env=_workspace_environment(spec.workspace_path),
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
                sessions_root=spec.codex_sessions_root,
            )
            return replace(result, metrics=metrics)

    @staticmethod
    def _build_command(spec: AgentRunSpec) -> list[str]:
        last_message_path = spec.log_path.with_suffix(".last-message.md")
        command = [spec.codex_command, "exec"]
        if spec.codex_resume_thread_id is not None:
            command.append("resume")
        command.extend(
            [
                "--model",
                spec.codex_model,
                "--json",
                "-c",
                f'model_reasoning_effort="{spec.codex_reasoning_effort}"',
                "-c",
                'approval_policy="never"',
            ]
        )
        for feature_name in CODEX_SPARK_DISABLED_FEATURES:
            command.extend(["--disable", feature_name])
        for server_name in spec.codex_disabled_mcp_servers:
            command.extend(["-c", f"mcp_servers.{server_name}.enabled=false"])
        if spec.codex_resume_thread_id is None:
            command.extend(["--cd", str(spec.workspace_path)])
        command.extend(["--output-last-message", str(last_message_path)])
        if spec.yolo:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        elif spec.codex_resume_thread_id is None:
            command.extend(
                [
                    "--sandbox",
                    "read-only" if spec.read_only else spec.codex_sandbox_mode,
                ]
            )
        if spec.codex_resume_thread_id is not None:
            command.append(spec.codex_resume_thread_id)
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


def _workspace_environment(workspace_path: Path) -> dict[str, str]:
    """Isolate a worker from the controller's active Python environment."""

    environment = os.environ.copy()
    inherited_virtual_env = environment.pop("VIRTUAL_ENV", None)
    environment.pop("UV_PYTHON", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("CONDA_PREFIX", None)

    path_entries = environment.get("PATH", "").split(os.pathsep)
    if inherited_virtual_env:
        inherited_scripts = _virtual_environment_scripts(Path(inherited_virtual_env))
        inherited_key = os.path.normcase(os.path.normpath(str(inherited_scripts)))
        path_entries = [
            entry
            for entry in path_entries
            if os.path.normcase(os.path.normpath(entry)) != inherited_key
        ]

    local_virtual_env = workspace_path / ".venv"
    if local_virtual_env.is_dir():
        local_scripts = _virtual_environment_scripts(local_virtual_env)
        environment["VIRTUAL_ENV"] = str(local_virtual_env)
        environment["UV_PROJECT_ENVIRONMENT"] = str(local_virtual_env)
        path_entries.insert(0, str(local_scripts))

    environment["PATH"] = os.pathsep.join(path_entries)
    return environment


def _virtual_environment_scripts(virtual_env: Path) -> Path:
    """Return the executable directory for a platform virtual environment."""

    return virtual_env / ("Scripts" if os.name == "nt" else "bin")
