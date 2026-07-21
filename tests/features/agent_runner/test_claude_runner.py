from pathlib import Path

from agent_control_plane.features.agent_runner.lib.claude_runner import ClaudeExecRunner
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
        prompt="secret task prompt",
        workspace_path=Path("D:/repo/workspace"),
        result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
        log_path=Path("D:/repo/runs/job-1/attempt-001.log"),
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


def test_read_only_forces_plan_permission_mode() -> None:
    command = _command(_spec(read_only=True))
    assert command[command.index("--permission-mode") + 1] == "plan"
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
