from __future__ import annotations

import io
import json
import sys

from scripts.claude_deny_background_bash import main


def _run(payload: object) -> tuple[int, str]:
    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        exit_code = main()
        output = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout
    return exit_code, output


def test_denies_backgrounded_bash_calls() -> None:
    exit_code, out = _run(
        {"tool_name": "Bash", "tool_input": {"command": "sleep 5", "run_in_background": True}}
    )
    assert exit_code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["hookEventName"] == "PreToolUse"


def test_allows_synchronous_bash_calls() -> None:
    exit_code, out = _run({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
    assert exit_code == 0
    assert out == ""


def test_allows_non_bash_tools_even_with_the_flag_set() -> None:
    exit_code, out = _run(
        {"tool_name": "Read", "tool_input": {"run_in_background": True, "file_path": "x"}}
    )
    assert exit_code == 0
    assert out == ""


def test_ignores_malformed_input() -> None:
    exit_code, out = _run(["not", "a", "dict"])
    assert exit_code == 0
    assert out == ""
