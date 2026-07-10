from __future__ import annotations

from pathlib import Path

from agent_control_plane.shared.config import ControlConfig


def build_task_prompt(
    *,
    config: ControlConfig,
    task_id: str,
    route: str,
    workspace_path: Path,
    expected_branch: str,
    result_path: Path,
) -> str:
    task_dir = config.coordination_root / "tasks" / task_id
    brief_path = task_dir / "brief.md"
    progress_path = task_dir / "agent-progress.md"
    protocol_path = config.coordination_root / "agent-protocol.md"
    routing_path = config.coordination_root / "workspace-routing.md"
    agentbridge_edit_root = _agentbridge_edit_root(config, workspace_path)

    return f"""Task ID: {task_id}
Workspace route: {route}
Workspace path: {workspace_path}
AgentBridge edit root: {agentbridge_edit_root}
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
- Use AgentBridge/IDE tools for repository edits and diagnostics when available.
- In Codex / `codex exec`, first call `mcp__agentbridge_ide.read_file` directly
  on the first path under "Read before acting". Registered AgentBridge HTTP MCP tools
  are normally available without a discovery call.
- Only use `tool_search` as an optional fallback when that function is actually
  available and the direct AgentBridge call reports an unknown or unavailable tool.
  Never block merely because `tool_search` is absent.
- After a successful direct or discovered call, use ordinary `mcp__agentbridge_ide`
  tools for repository reads, edits, git inspection, and diagnostics.
- Do not use DataSpell AgentBridge tools. The presence of a disabled `dataspell_ide`
  server in config or logs is not evidence that ordinary AgentBridge is unavailable.
- Do not use web search, raw shell `exec`, `list_mcp_resources`, or
  `list_mcp_resource_templates` for delegated repository work. The runner treats
  those markers as forbidden tool usage and will stop the attempt.
- Use `mcp__agentbridge_ide.run_command` for necessary local commands such as
  tests; do not use the raw shell tool directly.
- Do not treat `codex mcp list`, `list_mcp_resources`, or raw HTTP probes of `/mcp`
  as sufficient evidence that AgentBridge edit/diagnostic tools are unavailable;
  those checks do not lazy-load deferred tool metadata.
- Only block for missing AgentBridge tools after one direct AgentBridge call fails.
  If `tool_search` exists, use it once as a fallback. Include the exact direct-call and
  optional discovery output in the result.
- The control-plane initializes the progress file before runner start. Your first
  coordination action is to update the exact progress file path shown above:
  `{progress_path}` through `mcp__agentbridge_ide.write_file` or
  `mcp__agentbridge_ide.edit_text`; if it is unexpectedly missing, create it
  immediately at that exact path.
- The IDE indexes multiple checkouts. For every repository `read_file` and every
  `git_*` call, use the exact absolute physical workspace prefix:
  `{workspace_path}`. Relative repository paths and another checkout are forbidden.
- Before discovery, verify the physical workspace with `git_status` and `git_log`,
  both using `repo="{workspace_path}"`, then read one known file through an
  absolute path under `{workspace_path}`.
- Do not use project-wide AgentBridge `search_text`, `search_symbols`,
  `list_project_files`, `list_directory_tree`, or `attach_external_dir` in a
  delegated workspace. They can mix indexed checkouts. Use
  `mcp__agentbridge_ide.run_command` with path-scoped `rg`/`rg --files` against
  `{workspace_path}`, then read each discovered file by its absolute physical path.
- For repository `write_file` and `edit_text`, first read the exact physical file.
  When the AgentBridge edit root differs from the workspace path, it is the only
  permitted project-relative junction for the edit itself:
  `{agentbridge_edit_root}`. Do not pass direct absolute slot paths to `write_file`.
- Before each repository edit, confirm the physical source path starts with
  `{workspace_path}`, the edit path starts with `{agentbridge_edit_root}`, and both
  identify the same existing file. If either check fails, write Status: blocked.
  Never retry against the route root or canonical checkout.
- After each edit, re-read the absolute physical path and inspect `git_diff` with
  `repo="{workspace_path}"`. A successful junction write without a physical-slot
  diff is a routing failure, not a completed edit.
- In the final result, state the exact physical workspace, branch, and HEAD commit.
  Every cited repository path must begin with `{workspace_path}`.
- For coordination files under `{config.coordination_root}`, write to the exact
  absolute paths shown in this prompt: `{progress_path}` and `{result_path}`.
  Do not substitute `.agent-work/tasks/...` under the active IDE project unless it
  is exactly the same directory as `{config.coordination_root}`. If AgentBridge
  `write_file`/`edit_text` cannot write those absolute coordination paths, use
  `mcp__agentbridge_ide.run_command` once to write the same exact path with the
  local shell. This fallback is allowed only for coordination files, never for
  repository source edits.
- Verify the current directory and branch before editing.
- Do not switch branches in canonical route workspaces.
- Do not install dependencies yourself; slot preparation is handled by the control-plane before the job when configured.
- Do not generate or modify lockfiles.
- Do not suppress diagnostics to make the task look complete.
- If AgentBridge is unavailable for required edits or diagnostics, write a blocked result.
- If verification requires missing dependencies, report the blocker instead of installing packages.
- If something fails, name the exact tool or command and the exact failure.
- After the final edit, collect the changed file list and run AgentBridge diagnostics
  for every changed repository file that the IDE can inspect.
- For each changed file, prefer `get_problems(path=...)` for the pass/fail
  diagnostic check. `get_highlights` is useful for quick-fixes, but it is not
  enough by itself.
- Never treat `No highlights found in 0 files analyzed`, `0 files analyzed`, or
  any equivalent zero-file diagnostic result as clean. Treat it as inconclusive,
  retry with `include_unindexed=true` or `get_problems(path=...)`, and report the
  exact inconclusive output if the IDE still cannot analyze the file.
- Do not write Status: completed while any task-caused IDE error, warning, type
  warning, lint warning, unresolved import, syntax problem, or formatting problem
  remains.
- Before repository-wide exploration or any edit, create or update the live
  progress file with: Current phase, Confirmed facts, Target files, Next action,
  Changed files, and Open risks.
- At the start of every resumed or compacted turn, read the live progress file
  first and continue from it. Do not restart completed discovery unless the file
  says the prior discovery is invalid.
- Keep discovery bounded: after at most three focused search/read rounds, either
  write the exact implementation target files and plan to the progress file or
  write a partial/blocked result that names the missing input.
- Keep each search/read round narrow: prefer path-scoped `rg`, `rg --files`, and
  focused file ranges. Do not read huge legacy/front-end files end-to-end when a
  symbol search or smaller range can answer the question.
- If a shell or MCP tool call times out, returns exit code 124, or reports a
  repeated transport timeout twice, stop retrying that path, update progress,
  and write Status: partial or Status: blocked with the exact failing command.
- For smoke/audit tasks, if the inspected behavior is already present and no code
  change is needed, write Status: completed with exact evidence and stop instead
  of broadening the search.
- For audit-only tasks with no source changes, stop after either one successful
  focused verification command or two failed verification attempts. Do not keep
  trying alternate command spellings; write Status: partial/blocked with the exact
  blocker.
- Hard cap each attempt at roughly 25 AgentBridge tool calls after the first
  progress update. If you reach that cap, immediately write Status: partial or
  Status: completed using the evidence already collected.
- If the progress file already says the verification/conclusion is complete, the
  next action must be writing result.md, not another search/read/test command.
- Do not edit files until the progress file names the intended target files and
  behavior change. If the task is too broad for one bounded change, write a
  partial result with the next concrete split instead of widening the scope.
- Keep edits surgical. Do not rewrite whole files, mass-format unrelated code, or
  change indentation/whitespace outside the necessary block.
- If an edit, patch, or diff application fails, immediately re-read the target file
  and retry once with the current content. If the retry also fails, write
  Status: partial or Status: blocked with the exact failed operation and stop.
  Do not keep printing the same diff or repeating analysis without a new successful
  file read/edit/result write.
- If the diff grows beyond the task scope, contains unrelated formatting churn,
  or exceeds roughly 120 changed lines without an explicit need in the brief,
  stop, preserve the changed file list in the progress file, and write a
  Status: partial result instead of continuing.
- After every edit, update the live progress file before doing more analysis so
  a compacted/resumed worker can recover without repeating work.
- The progress file is an internal handoff artifact; the final result file is
  still mandatory.
- Treat unresolved imports as real diagnostics unless a focused runtime/linter
  check proves they are only IDE source-root/indexing noise. Report such proven
  IDE-index issues explicitly under Diagnostics with the exact verification
  command.
- If a remaining diagnostic is unrelated, stale, IDE-index-only, or outside
  scope, verify it with a focused check and report it under Diagnostics; use
  Status: partial unless the task explicitly allows that diagnostic class.
- Run the narrowest relevant formatter/linter/type/test commands already
  available in the repo. If they cannot run because dependencies are missing,
  report that exact blocker instead of claiming completion.
- Always write the result file before stopping, even when blocked, interrupted, or only partially done.
- Never finish with only terminal output. The result file is the required handoff artifact.

Mandatory result file format:
- Start the file with exactly one of: Status: completed, Status: partial, Status: blocked.
- Include these sections: Changed files, What changed, Verification performed, Not verified / remaining risks.
- If blocked or partial, include the exact blocker and the next concrete action.
- When done, write the result file and stop.
"""


def _agentbridge_edit_root(config: ControlConfig, workspace_path: Path) -> str:
    workspace = workspace_path.resolve(strict=False)
    slot_root = config.slot_root.resolve(strict=False)
    if workspace != slot_root and workspace.is_relative_to(slot_root):
        return f".agent-work/slot-links/{workspace.name}"
    return str(workspace_path)
