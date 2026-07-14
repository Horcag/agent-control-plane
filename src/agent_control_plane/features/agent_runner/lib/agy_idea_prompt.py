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
    mcp_server: str = "idea",
    workspace_create_root: str = ".",
    read_only: bool,
) -> str:
    """Build the AGY prompt for the configured JetBrains MCP contract."""

    if mcp_server != "idea":
        return _build_agentbridge_task_prompt(
            task_id=task_id,
            route=route,
            workspace_path=workspace_path,
            workspace_create_root=workspace_create_root,
            expected_branch=expected_branch,
            result_path=result_path,
            progress_path=progress_path,
            protocol_path=protocol_path,
            routing_path=routing_path,
            brief_path=brief_path,
            expected_idea_project_root=expected_idea_project_root,
            mcp_server=mcp_server,
            read_only=read_only,
        )

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
- The assigned workspace may be physically outside the host project directory
  when IDEA has attached it as a VCS/module root. Physical containment neither
  grants nor proves access; the exact repository-root canary above is mandatory.
- Junctions, symlinks, directory links, reparse points, aliases, and canonical-root
  proxy paths are forbidden. If a native IDEA operation rejects the exact assigned
  workspace, report the failure and stop instead of constructing a path workaround.
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
- Every diagnostic claim must refer to the exact changed physical file under
  `{workspace_path}`. Never inspect the canonical checkout or another slot as a
  proxy. If native IDEA diagnostics reject an attached external workspace,
  record the exact failure and treat that check as inconclusive; canonical-file
  findings are not evidence for the assigned workspace.
- When the brief makes exact IDEA diagnostics a completion condition, an
  inconclusive external-workspace diagnostic requires Status: partial or
  Status: blocked even if terminal linters and tests pass.
- Use `mcp__idea__reformat_file` only for changed files. Do not mass-format,
  suppress diagnostics, or rewrite unrelated code.
- Run the narrowest existing tests, linters, type checks, and format checks through
  `mcp__idea__execute_terminal_command`. Do not install dependencies or modify
  lockfiles; slot preparation belongs to the control-plane.
- IDEA terminal commands may inherit the host IDE's Python environment rather
  than the assigned workspace environment. Before any Python or `uv` command, if
  `{workspace_path}/.venv` exists, make the command self-contained: set both
  `VIRTUAL_ENV` and `UV_PROJECT_ENVIRONMENT` to that exact workspace `.venv`,
  prepend its `Scripts` directory on Windows (or `bin` on POSIX) to `PATH`, and
  change directory to `{workspace_path}`. Prefer the exact workspace `.venv`
  Python executable and verify `sys.prefix` resolves inside it; never target the
  canonical checkout's inherited environment.
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


def _build_agentbridge_task_prompt(
    *,
    task_id: str,
    route: str,
    workspace_path: Path,
    workspace_create_root: str,
    expected_branch: str,
    result_path: Path,
    progress_path: Path,
    protocol_path: Path,
    routing_path: Path,
    brief_path: Path,
    expected_idea_project_root: Path,
    mcp_server: str,
    read_only: bool,
) -> str:
    access_rule = (
        "- This is a read-only job. Repository writes, Git mutations, commits, and pushes are forbidden."
        if read_only
        else "- Repository writes are allowed only under the assigned workspace and only when required by the brief."
    )

    return f"""Task ID: {task_id}
Workspace route: {route}
Workspace path: {workspace_path}
AgentBridge create root: {workspace_create_root}
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
- Use only the configured AgentBridge MCP server `{mcp_server}` and its direct tools
  for repository reads, edits, Git inspection, terminals, tests, formatting, and
  diagnostics. Do not use the native `idea` server, DataSpell, another AgentBridge
  instance, web search, hosted tools, raw shell tools, or MCP discovery wrappers.
- The first MCP call must invoke `{mcp_server}` tool `get_project_info`. Require its
  reported project Path, after normalizing separators and resolving it, to equal
  exactly `{expected_idea_project_root}`. If it differs, write Status: blocked with
  expected and actual roots, then stop before repository reads or writes.
- After the project-root canary, invoke `git_status` and `git_log` with
  `repo="{workspace_path}"`, then `read_file` on `{protocol_path}` and the remaining
  required files. Use exact absolute physical paths for every existing repository
  file; relative paths and another checkout are forbidden.
- The first coordination write must update `{progress_path}` with Current phase,
  Confirmed facts, Target files, Next action, Changed files, and Open risks.
- Use path-scoped `rg` through `run_in_terminal` or narrowly scoped AgentBridge
  searches. Project-wide searches across all indexed checkouts are forbidden.
- Edit an existing repository file only through `edit_text` using its exact absolute
  physical path under `{workspace_path}`. Re-read it immediately and inspect
  `git_diff(repo="{workspace_path}")` after each coherent edit phase.
- Create a new repository file only through `write_file` at a project-relative path
  under `{workspace_create_root}`, then re-read its exact absolute path under
  `{workspace_path}` and require `git_status(repo="{workspace_path}")` to show it.
- Junctions, symlinks, directory links, reparse points, aliases, and canonical-root
  proxy paths are forbidden. Never create one to expose an external workspace to the
  IDE. If an exact AgentBridge operation rejects the assigned workspace, report the
  failure and stop instead of constructing a path workaround.
- Use dedicated `git_*` tools exclusively for Git. Terminal Git is forbidden. Never
  stage, commit, switch branches, merge, rebase, reset, or push unless the brief
  explicitly requests that exact mutation.
- Reserve one terminal tab named exactly `{task_id}` for necessary commands. Reuse
  and close that exact tab before finishing.
- AgentBridge/IDE terminal tabs may inherit the host IDE's Python environment
  rather than the assigned workspace environment. Before any Python or `uv`
  command, if `{workspace_path}/.venv` exists, make the command self-contained:
  set both `VIRTUAL_ENV` and `UV_PROJECT_ENVIRONMENT` to that exact workspace
  `.venv`, prepend its `Scripts` directory on Windows (or `bin` on POSIX) to
  `PATH`, and set the working directory to `{workspace_path}`. Prefer the exact
  workspace `.venv` Python executable and verify `sys.prefix` resolves inside it;
  never target the canonical checkout's inherited environment.
- Before edits, record `get_problems` baselines for target files that exist. After
  edits, run `get_problems` on every changed file using its exact physical workspace
  path. A zero-file or unavailable analysis is inconclusive, not clean; never inspect
  the canonical checkout or another slot as a proxy.
- Verify current branch and HEAD before editing. Preserve inherited and unrelated
  changes. Keep edits surgical; do not mass-format or broaden task scope.
- Do not install or resolve dependencies, generate lockfiles, suppress diagnostics,
  or weaken quality gates. Run only the narrowest existing checks required by the
  brief through AgentBridge terminals.
- Keep discovery bounded. After at most three focused read/search rounds, record the
  exact target files and plan or return a precise partial/blocked result.
- If any tool rejects the exact workspace twice, or a route-root/forbidden artifact
  appears, stop and write Status: partial or Status: blocked. Do not devise an alias,
  junction, copy, or shell-write workaround.
- Always write `{result_path}` before stopping, including on failure, interruption,
  or partial completion. Never finish with terminal output alone.

Mandatory result file format:
- Start with exactly one of: Status: completed, Status: partial, Status: blocked.
- Include: Changed files, What changed, Verification performed,
  Not verified / remaining risks.
- State the exact physical workspace, branch, and HEAD/commit SHA.
- If blocked or partial, include the exact blocker and next concrete action.
"""
