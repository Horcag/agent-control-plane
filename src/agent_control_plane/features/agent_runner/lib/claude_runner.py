from __future__ import annotations

import subprocess  # nosec B404
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from agent_control_plane.features.agent_runner.lib.claude_telemetry import (
    claude_turn_completed,
    parse_claude_jsonl,
    render_claude_json_line,
    scan_claude_tool_constraints,
)
from agent_control_plane.features.agent_runner.lib.codex_output import CodexOutputCapture
from agent_control_plane.features.agent_runner.lib.codex_process_monitor import (
    CodexProcessMonitor,
    terminate_spawned_process,
)
from agent_control_plane.features.agent_runner.lib.codex_runner import (
    _creation_flags,
    _workspace_environment,
)
from agent_control_plane.features.agent_runner.lib.model_catalog import ModelCatalog
from agent_control_plane.features.agent_runner.lib.runner import AgentRunResult, AgentRunSpec


class ClaudeProcessMonitor(CodexProcessMonitor):
    """Codex monitor semantics with Claude stream-json event awareness."""

    def _turn_completed(self, event_log_path: Path) -> bool:
        return claude_turn_completed(event_log_path)

    def _scan_tool_constraints(
        self,
        spec: AgentRunSpec,
        scan_size: int,
        tool_call_count: int,
    ) -> tuple[str | None, int, int]:
        return scan_claude_tool_constraints(
            spec.log_path.with_suffix(".events.jsonl"),
            scan_size,
            tool_call_count,
            tool_call_budget=spec.codex_tool_call_budget,
        )


class ClaudeExecRunner:
    def __init__(self, catalog: ModelCatalog | None = None) -> None:
        self.catalog = catalog

    def run(
        self,
        spec: AgentRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        pid_observed: Callable[[int | None], None],
    ) -> AgentRunResult:
        model = spec.claude_model or spec.codex_model
        reasoning_effort = spec.claude_reasoning_effort or spec.codex_reasoning_effort
        session_id = spec.codex_resume_thread_id or str(uuid.uuid4())
        command = self._build_command(spec, model, reasoning_effort, session_id)
        started_wall = time.time()
        started_mono = time.monotonic()
        deadline_mono = started_mono + spec.timeout_sec

        with CodexOutputCapture(spec.log_path, render_line=render_claude_json_line) as output:
            log = output.log
            if log is None:
                raise RuntimeError("Claude output log was not opened")
            log.write("# claude exec run\n")
            log.write(f"workspace_access: {spec.workspace_access}\n")
            log.write(f"workspace: {spec.workspace_path}\n")
            log.write(f"model: {model}\n")
            log.write(f"reasoning_effort: {reasoning_effort}\n")
            log.write(f"session: {session_id}\n")
            log.write(f"events: {output.event_log_path}\n")
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
                    message=f"Failed to spawn claude -p: {exc}",
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
                        message="claude -p started without a stdout pipe",
                    )
                else:
                    output.start(proc.stdout)
                    self._send_prompt(proc, spec.prompt, log)
                    result = ClaudeProcessMonitor().monitor(
                        proc,
                        spec,
                        started_wall,
                        deadline_mono,
                        started_mono,
                        0,
                        log,
                        cancel_requested,
                    )

            output.join()
            metrics = parse_claude_jsonl(
                output.event_log_path,
                model=model,
                duration_sec=time.monotonic() - started_mono,
                sessions_root=spec.claude_sessions_root or _default_claude_sessions_root(),
                workspace_path=spec.workspace_path,
                catalog=self.catalog,
                session_id_hint=session_id,
            )
            return replace(result, metrics=metrics)

    @staticmethod
    def _build_command(
        spec: AgentRunSpec,
        model: str,
        reasoning_effort: str,
        session_id: str,
    ) -> list[str]:
        command = [
            spec.claude_command,
            "-p",
            "--model",
            model,
            "--effort",
            reasoning_effort,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if spec.codex_resume_thread_id is not None:
            command.extend(["--resume", spec.codex_resume_thread_id])
        else:
            command.extend(["--session-id", session_id])
        if spec.yolo:
            command.append("--dangerously-skip-permissions")
        elif spec.read_only:
            command.extend(["--permission-mode", "plan"])
        else:
            command.extend(["--permission-mode", spec.claude_permission_mode])
        if spec.claude_allowed_tools:
            command.extend(["--allowedTools", ",".join(spec.claude_allowed_tools)])
        if spec.workspace_access == "native":
            command.extend(["--add-dir", str(spec.result_path.parent)])
        if spec.claude_max_turns > 0:
            command.extend(["--max-turns", str(spec.claude_max_turns)])
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
            log.write(f"\n[failed to send prompt to claude stdin: {exc}]\n")
            log.flush()


def _default_claude_sessions_root() -> Path:
    return Path.home() / ".claude" / "projects"
