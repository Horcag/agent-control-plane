from __future__ import annotations

from pathlib import Path


def build_agy_task_prompt(
    *,
    task_id: str,
    route: str,
    workspace_path: Path,
    expected_branch: str,
    result_path: Path,
    progress_path: Path,
    protocol_path: Path,
    routing_path: Path,
    brief_path: Path,
    expected_idea_project_root: Path,
    read_only: bool,
) -> str:
    """Build the AGY prompt for JetBrains' native `idea` MCP contract."""

    access_rule = (
        "- This is a read-only job. Repository writes, Git mutations, commits, and pushes are forbidden."
        if read_only
        else "- Repository writes are allowed only under the assigned workspace and only when required by the brief."
    )

    return f"""Task ID: {task_id}
Workspace route: {route}
Workspace path: {workspace_path}
Expected IDEA host project root: {expected_idea_project_root}
Expected branch: {expected_branch}

Read before acting:
- {protocol_path}
- {routing_path}
- {brief_path}

Execute the task only in:
- {workspace_path}

Write the final result to:
- {result_path}

Maintain live progress/state in:
- {progress_path}

Mandatory execution rules:
{access_rule}
- Use only the native JetBrains IDEA MCP server `idea` through direct
  `mcp__idea__*` tools for repository reads, edits, terminal commands, tests,
  diagnostics, formatting, and Git commands.
- AgentBridge, DataSpell, web search, hosted tools, raw shell tools, and MCP
  discovery/resource-list calls are forbidden. Do not configure or fall back to
  another MCP server.
- The first call must be `mcp__idea__get_repositories` with
  `projectPath="{expected_idea_project_root}"`, the open IDEA host project.
  Require the returned VCS roots to contain the exact normalized physical
  workspace `{workspace_path}`. If it does not, write Status: blocked with the
  actual roots and stop before reading or editing.
- After the canary succeeds, call `mcp__idea__read_file` directly for
  `{protocol_path}`, with `projectPath="{expected_idea_project_root}"`.
  Registered tools are available directly; do not use `call_mcp_tool` wrappers.
- Pass `projectPath="{expected_idea_project_root}"` on every native IDEA
  call. The assigned slot is a VCS/module root inside that open host project; it
  is not itself a separately open IDEA project. Still target only exact absolute
  repository paths under `{workspace_path}`.
- Read existing repository files by exact absolute paths under
  `{workspace_path}`. Never substitute the canonical checkout or another slot.
- Use `mcp__idea__search_in_files_by_text` only with a narrow
  `directoryToSearch`, or `mcp__idea__execute_terminal_command` for path-scoped
  `rg`/specific checks. Project-wide searches across indexed checkouts are
  forbidden.
- Before editing, verify branch, HEAD, and clean status using
  `mcp__idea__execute_terminal_command` with
  `projectPath="{expected_idea_project_root}"`; every command must first change
  directory to `{workspace_path}`. Git may run only inside that IDEA terminal
  tool and only against the assigned workspace.
- Edit existing files with `mcp__idea__apply_patch`, using exact physical
  slot paths (or their verified host-project-relative form) and
  `projectPath="{expected_idea_project_root}"`. Create new files with
  `mcp__idea__create_new_file` only under the assigned slot module and verify
  their exact physical location immediately.
- After every coherent edit phase, re-read changed files, inspect `git diff` and
  `git status` through `mcp__idea__execute_terminal_command`, and update the
  exact progress file.
- Use `mcp__idea__get_file_problems` for every changed file, with its verified
  host-project-relative path and `projectPath="{expected_idea_project_root}"`.
  Record a pre-edit diagnostic baseline for intended existing targets and
  compare it with the final result. A zero-file or unavailable analysis is
  inconclusive, not clean.
- Use `mcp__idea__reformat_file` only for changed files. Do not mass-format,
  suppress diagnostics, or rewrite unrelated code.
- Run the narrowest existing tests, linters, type checks, and format checks through
  `mcp__idea__execute_terminal_command`. Do not install dependencies or modify
  lockfiles; slot preparation belongs to the control-plane.
- The first coordination write must update `{progress_path}` through the native
  IDEA MCP. Include Current phase, Confirmed facts, Target files, Next action,
  Changed files, and Open risks.
- Keep discovery bounded. After at most three focused search/read rounds, record
  exact target files and the implementation plan, or write a partial/blocked
  result naming the missing input.
- Preserve user changes. Keep edits surgical and task-scoped. If a patch fails,
  re-read and retry once; if it fails again, write a partial/blocked result.
- Do not claim completion while any new task-caused error, warning, type issue,
  lint issue, unresolved import, formatting problem, test failure, forbidden
  change, or dirty unrelated file remains.
- Before completion, verify exact branch, HEAD, changed files, diff, and clean
  post-commit status through the native IDEA MCP terminal. Never push.
- Always write `{result_path}` before stopping, including on failure,
  interruption, or partial completion. Never finish with terminal output alone.

Mandatory result file format:
- Start with exactly one of: Status: completed, Status: partial, Status: blocked.
- Include: Changed files, What changed, Verification performed,
  Not verified / remaining risks.
- State the exact physical workspace, branch, and HEAD/commit SHA.
- If blocked or partial, include the exact blocker and next concrete action.
"""
