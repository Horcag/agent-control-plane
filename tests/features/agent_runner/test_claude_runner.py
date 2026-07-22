import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.claude_runner import (
    ClaudeExecRunner,
    ClaudeProcessMonitor,
)
from agent_control_plane.features.agent_runner.lib.runner import AgentRunSpec


def _spec(
    *,
    read_only: bool = False,
    yolo: bool = False,
    claude_permission_mode: str = "acceptEdits",
    claude_allowed_tools: tuple[str, ...] = (),
    claude_max_turns: int = 0,
    claude_bare: bool = True,
    codex_resume_thread_id: str | None = None,
    workspace_access: str = "native",
    claude_mcp_config_path: Path | None = None,
    tool_call_budget: int = 0,
    tool_call_budget_grace_sec: int = 0,
    log_path: Path | None = None,
    result_path: Path | None = None,
) -> AgentRunSpec:
    return AgentRunSpec(
        backend="claude",
        agy_command="agy",
        codex_command="codex",
        codex_model="claude-opus-4-8",
        codex_reasoning_effort="high",
        codex_sandbox_mode="workspace-write",
        codex_disabled_mcp_servers=(),
        codex_resume_thread_id=codex_resume_thread_id,
        tool_call_budget=tool_call_budget,
        tool_call_budget_grace_sec=tool_call_budget_grace_sec,
        prompt="secret task prompt",
        workspace_path=Path("D:/repo/workspace"),
        result_path=result_path or Path("D:/repo/.agent-work/tasks/task-1/result.md"),
        log_path=log_path or Path("D:/repo/runs/job-1/attempt-001.log"),
        print_timeout="10s",
        timeout_sec=30,
        idle_timeout_sec=10,
        yolo=yolo,
        read_only=read_only,
        workspace_access=workspace_access,
        claude_command="claude",
        claude_model="claude-opus-4-8",
        claude_reasoning_effort="high",
        claude_permission_mode=claude_permission_mode,
        claude_allowed_tools=claude_allowed_tools,
        claude_max_turns=claude_max_turns,
        claude_bare=claude_bare,
        claude_mcp_config_path=claude_mcp_config_path,
    )


def _command(spec: AgentRunSpec, session_id: str = "11111111-2222-3333-4444-555555555555"):
    return ClaudeExecRunner._build_command(
        spec,
        spec.claude_model or spec.codex_model,
        spec.claude_reasoning_effort or spec.codex_reasoning_effort,
        session_id,
    )


def test_command_targets_headless_stream_json_with_model_and_effort() -> None:
    command = _command(_spec())
    assert command[0] == "claude"
    assert "-p" in command
    assert command[command.index("--model") + 1] == "claude-opus-4-8"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"


def test_prompt_is_never_passed_on_the_command_line() -> None:
    command = _command(_spec())
    assert all("secret task prompt" not in part for part in command)


def test_fresh_runs_pin_the_session_id_and_native_result_dir() -> None:
    command = _command(_spec())
    assert command[command.index("--session-id") + 1] == "11111111-2222-3333-4444-555555555555"
    assert command[command.index("--add-dir") + 1] == str(Path("D:/repo/.agent-work/tasks/task-1"))
    assert "--resume" not in command


def test_resume_reuses_the_prior_session() -> None:
    command = _command(_spec(codex_resume_thread_id="prior-session"))
    assert command[command.index("--resume") + 1] == "prior-session"
    assert "--session-id" not in command


def test_read_only_uses_default_permission_mode_not_plan() -> None:
    # Headless `claude -p` cannot complete plan mode's ExitPlanMode approval, so read-only
    # runs under default prompting and relies on the restricted allowlist instead.
    command = _command(_spec(read_only=True))
    assert command[command.index("--permission-mode") + 1] == "default"
    assert "plan" not in command
    assert "--dangerously-skip-permissions" not in command


def test_yolo_bypasses_permissions_instead_of_permission_mode() -> None:
    command = _command(_spec(yolo=True))
    assert "--dangerously-skip-permissions" in command
    assert "--permission-mode" not in command


def test_allowed_tools_and_max_turns_are_forwarded() -> None:
    command = _command(_spec(claude_allowed_tools=("Read", "Bash"), claude_max_turns=7))
    assert command[command.index("--allowedTools") + 1] == "Read,Bash"
    assert command[command.index("--max-turns") + 1] == "7"


def test_ide_mcp_access_does_not_expose_the_result_dir() -> None:
    command = _command(_spec(workspace_access="ide_mcp"))
    assert "--add-dir" not in command


def test_ide_mcp_passes_the_selected_server_mcp_config() -> None:
    mcp_config = Path("D:/repo/runs/job-1/claude-mcp-config.json")
    command = _command(_spec(workspace_access="ide_mcp", claude_mcp_config_path=mcp_config))
    assert command[command.index("--mcp-config") + 1] == str(mcp_config)
    # claude_bare isolation stays on, so --strict-mcp-config + --mcp-config means the
    # worker loads exactly the one selected IDE MCP server and nothing else.
    assert "--strict-mcp-config" in command
    assert "--add-dir" not in command


def test_native_jobs_do_not_pass_an_mcp_config() -> None:
    command = _command(_spec())
    assert "--mcp-config" not in command


def test_bare_isolation_flags_are_on_by_default() -> None:
    command = _command(_spec())
    assert "--bare" not in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--setting-sources") + 1] == "project"


def test_bare_isolation_can_be_disabled() -> None:
    command = _command(_spec(claude_bare=False))
    assert "--bare" not in command
    assert "--strict-mcp-config" not in command
    assert "--setting-sources" not in command


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.wait_timeout: float | None = None

    def poll(self) -> int | None:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeout = timeout
        return 0

    def kill(self) -> None:
        self.killed = True


def _write_claude_tool_calls(log_path: Path, *, count: int) -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": f"call-{i}", "name": "Bash", "input": {}}
                    ]
                },
            }
        )
        for i in range(count)
    ]
    lines.append(json.dumps({"type": "result", "subtype": "success", "is_error": False}))
    log_path.with_suffix(".events.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def _write_verification(root: Path, *, status: str) -> None:
    (root / "verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "changed_files": [],
                "checks": [],
                "unverified": [],
            }
        ),
        encoding="utf-8",
    )


def test_claude_budget_breach_kills_immediately_when_grace_is_zero() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        log_path = root / "attempt-001.log"
        _write_claude_tool_calls(log_path, count=2)
        result_path = root / "result.md"
        proc = _FakeProc()

        result = ClaudeProcessMonitor().monitor(
            proc,
            _spec(
                read_only=False,
                log_path=log_path,
                result_path=result_path,
                tool_call_budget=1,
                tool_call_budget_grace_sec=0,
            ),
            started_wall=0.0,
            deadline_mono=time.monotonic() + 10,
            last_output_mono=time.monotonic(),
            last_log_size=0,
            log=io.StringIO(),
            cancel_requested=lambda: False,
        )

        assert result.status == "tool_call_budget"
        assert proc.terminated
        assert "Claude tool-call budget of 1 exceeded" in result.message
        assert not any(event.kind == "budget_breach" for event in result.lifecycle_events)


def test_claude_budget_breach_grace_window_completes_normally_when_handoff_lands_in_time() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        log_path = root / "attempt-001.log"
        _write_claude_tool_calls(log_path, count=2)
        result_path = root / "result.md"
        result_path.write_text("Status: partial\n", encoding="utf-8")
        _write_verification(root, status="partial")
        proc = _FakeProc()

        result = ClaudeProcessMonitor().monitor(
            proc,
            _spec(
                read_only=False,
                log_path=log_path,
                result_path=result_path,
                tool_call_budget=1,
                tool_call_budget_grace_sec=120,
            ),
            started_wall=0.0,
            deadline_mono=time.monotonic() + 10,
            last_output_mono=time.monotonic(),
            last_log_size=0,
            log=io.StringIO(),
            cancel_requested=lambda: False,
        )

        assert result.status == "completed"
        assert not proc.terminated
        assert any(event.kind == "budget_breach" for event in result.lifecycle_events)


def test_claude_budget_breach_grace_window_expires_without_handoff() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        log_path = root / "attempt-001.log"
        _write_claude_tool_calls(log_path, count=2)
        result_path = root / "result.md"
        proc = _FakeProc()

        result = ClaudeProcessMonitor().monitor(
            proc,
            _spec(
                read_only=False,
                log_path=log_path,
                result_path=result_path,
                tool_call_budget=1,
                tool_call_budget_grace_sec=1,
            ),
            started_wall=0.0,
            deadline_mono=time.monotonic() + 10,
            last_output_mono=time.monotonic(),
            last_log_size=0,
            log=io.StringIO(),
            cancel_requested=lambda: False,
        )

        assert result.status == "tool_call_budget"
        assert proc.terminated
        assert "grace of" in result.message
        assert any(event.kind == "budget_breach" for event in result.lifecycle_events)


def test_claude_budget_breach_runaway_cap_terminates_immediately_during_grace() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        log_path = root / "attempt-001.log"
        events_path = log_path.with_suffix(".events.jsonl")
        # budget=1 -> runaway cap = 1 + max(4, 1 // 10) = 5; two calls cross the budget
        # (breach), then four more appended slower than the monitor's poll cadence push the
        # count to 6 (> 5), firing the runaway cap instead of waiting out the grace window.
        _write_claude_tool_calls(log_path, count=2)
        result_path = root / "result.md"
        proc = _FakeProc()

        def _append_more_calls() -> None:
            for i in range(4):
                time.sleep(0.3)
                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": f"extra-{i}",
                                            "name": "Bash",
                                            "input": {},
                                        }
                                    ]
                                },
                            }
                        )
                        + "\n"
                    )

        appender = threading.Thread(target=_append_more_calls, daemon=True)
        appender.start()
        try:
            result = ClaudeProcessMonitor().monitor(
                proc,
                _spec(
                    read_only=False,
                    log_path=log_path,
                    result_path=result_path,
                    tool_call_budget=1,
                    tool_call_budget_grace_sec=120,
                ),
                started_wall=0.0,
                deadline_mono=time.monotonic() + 15,
                last_output_mono=time.monotonic(),
                last_log_size=0,
                log=io.StringIO(),
                cancel_requested=lambda: False,
            )
        finally:
            appender.join(timeout=5)

        assert result.status == "tool_call_budget"
        assert proc.terminated
        assert "runaway cap" in result.message


if __name__ == "__main__":
    unittest.main()
