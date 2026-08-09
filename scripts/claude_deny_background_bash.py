"""Claude Code `PreToolUse` hook: deny backgrounded `Bash` calls.

Workers run headless via `claude -p`. There is no channel to deliver a
background-task completion notification back to a worker process, so a model
that backgrounds a `Bash` call and ends its turn to wait for that
notification leaves the job hung until it times out
(`runner_failure=exited_without_result`). This hook makes that move
impossible instead of relying on prompt text alone.

Wired in by `ClaudeExecRunner._build_command` via an inline `--settings`
JSON payload; see `docs/claude-worker-routing.md`.
"""

from __future__ import annotations

import json
import sys

_DENY_REASON = (
    "run_in_background is forbidden for this worker: a headless job has no "
    "notification channel, so a backgrounded Bash call that ends the turn leaves "
    "the job hung until it times out. Run the command synchronously and wait for "
    "it to finish."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not tool_input.get("run_in_background"):
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _DENY_REASON,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
