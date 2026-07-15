from __future__ import annotations

import os
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.agy_idea_prompt import build_agy_task_prompt
from agent_control_plane.shared.agent_backends import AGY_BACKEND, CODEX_BACKEND
from agent_control_plane.shared.config import ControlConfig


def build_task_prompt(
    *,
    config: ControlConfig,
    task_id: str,
    route: str,
    workspace_path: Path,
    expected_branch: str,
    result_path: Path,
    backend: str = CODEX_BACKEND,
    read_only: bool = False,
    codex_tool_call_budget: int = 0,
) -> str:
    task_dir = config.coordination_root / "tasks" / task_id
    brief_path = task_dir / "brief.md"
    progress_path = task_dir / "agent-progress.md"
    protocol_path = _protocol_path(config.coordination_root)
    routing_path = _required_file(config.coordination_root / "workspace-routing.md")
    _required_file(brief_path)
    idea_edit_path = workspace_path.resolve(strict=False)
    idea_edit_root = str(idea_edit_path)
    idea_project_root = config.coordination_root.parent.resolve(strict=False)
    route_config = config.routes.get(route)
    expected_idea_project_root = (
        route_config.ide_mcp_project_root
        if route_config is not None and route_config.ide_mcp_project_root is not None
        else idea_project_root
    ).resolve(strict=False)
    if backend == AGY_BACKEND:
        agy_mcp_server = (
            route_config.agy_mcp_server
            if route_config is not None and route_config.agy_mcp_server is not None
            else "idea"
        )
        workspace_create_root = (
            _idea_create_root(idea_edit_path, expected_idea_project_root)
            if agy_mcp_server != "idea"
            else "."
        )
        return build_agy_task_prompt(
            task_id=task_id,
            route=route,
            workspace_path=idea_edit_path,
            expected_branch=expected_branch,
            result_path=result_path,
            progress_path=progress_path,
            protocol_path=protocol_path,
            routing_path=routing_path,
            brief_path=brief_path,
            expected_idea_project_root=expected_idea_project_root,
            mcp_server=agy_mcp_server,
            workspace_create_root=workspace_create_root,
            read_only=read_only,
        )

    idea_create_root = _idea_create_root(idea_edit_path, idea_project_root)
    idea_server, tool_namespace, forbidden_idea_servers = _idea_mcp_settings(config, route)

    return f"""Task ID: {task_id}
Workspace route: {route}
Workspace path: {workspace_path}
IDEA MCP edit root: {idea_edit_root}
IDEA MCP create root: {idea_create_root}
Expected IDEA MCP project root: {expected_idea_project_root}
Expected branch: {expected_branch}
Hard tool-call budget: {codex_tool_call_budget or "runner default"}

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
- Use only the IDEA MCP server `{idea_server}` through native
  `{tool_namespace}*` tools for repository reads, edits, Git inspection,
  terminals, tests, and diagnostics.
- {forbidden_idea_servers} are forbidden for this job. Do not
  discover, call, configure, or fall back to those servers.
- In Codex / `codex exec`, the first IDEA MCP call must be
  `{tool_namespace}get_project_info`. Require its reported project Path,
  after normalizing separators and resolving it, to equal exactly
  `{expected_idea_project_root}`.
- If the IDEA MCP project root differs, do not read, search, edit, run Git, or
  execute repository commands through that server. Write Status: blocked with
  the expected and actual roots, then stop.
- After the project-root canary succeeds, call `{tool_namespace}read_file`
  directly on the first path under "Read before acting". Registered IDEA MCP
  tools are normally available without a discovery call.
- Only use `tool_search` as an optional fallback when that function is actually
  available and the direct IDEA MCP call reports an unknown or unavailable tool.
  Never block merely because `tool_search` is absent.
- After a successful direct or discovered call, use only ordinary
  `{tool_namespace}*` tools for local repository/IDE work.
- Do not use web search, raw shell `exec`, `list_mcp_resources`, or
  `list_mcp_resource_templates` for delegated repository work. The runner treats
  those markers as forbidden tool usage and will stop the attempt.
- Use `{tool_namespace}run_in_terminal` for necessary local commands such
  as path-scoped `rg`, tests, linters, and formatters. Use dedicated IDEA MCP
  `git_*` tools exclusively for Git; terminal Git is forbidden.
- Reserve one terminal tab named exactly `{task_id}`. Every `run_in_terminal`,
  `read_terminal_output`, `write_terminal_input`, and `close_terminal` call must
  pass `tab_name="{task_id}"`; never omit it and never append display suffixes
  such as ` (new)`. Close that exact tab before finishing.
- AgentBridge terminal tabs may inherit the host IDE's Python environment rather
  than the assigned workspace environment. Before any Python or `uv` command, if
  `{workspace_path}/.venv` exists, make the command self-contained: set both
  `VIRTUAL_ENV` and `UV_PROJECT_ENVIRONMENT` to that exact workspace `.venv`,
  prepend its `Scripts` directory on Windows (or `bin` on POSIX) to `PATH`, and
  set the working directory to `{workspace_path}`. Prefer invoking the exact
  workspace `.venv` Python executable for Python-based tools. Verify `sys.prefix`
  resolves inside `{workspace_path}/.venv` before accepting quality-gate output;
  never rely on or target the canonical checkout's inherited environment.
- Stay within the hard tool-call budget shown above. Batch independent reads
  when safe, avoid repeated full-log/status calls, and stop with a precise
  partial result before spending calls on speculative cleanup.
- Only block for missing IDEA MCP tools after one direct IDEA MCP call fails.
  If `tool_search` exists, use it once as a fallback. Include the exact
  direct-call and optional discovery output in the result.
- The control-plane initializes the progress file before runner start. Your first
  coordination action is to update the exact progress file path shown above:
  `{progress_path}` through `{tool_namespace}write_file` or
  `{tool_namespace}edit_text`; if it is unexpectedly missing, create it
  immediately at that exact path.
- The IDEA project indexes multiple checkouts. For every repository `read_file`
  and every `git_*` call, use the exact absolute physical workspace prefix:
  `{workspace_path}`. Relative repository paths and another checkout are forbidden.
- Before discovery, verify the physical workspace with `git_status` and `git_log`,
  both using `repo="{workspace_path}"`, then read one known file through an
  absolute path under `{workspace_path}`.
- Do not use project-wide `search_text`, `search_symbols`, `list_project_files`,
  or `list_directory_tree` across all indexed checkouts. Use
  `{tool_namespace}run_in_terminal` with path-scoped `rg`/`rg --files`
  against `{workspace_path}`, then read each discovered file by its exact
  absolute physical path.
- Read existing repository files by their absolute physical paths under
  `{idea_edit_root}` and edit them through `edit_text` using the same absolute
  path. Synthetic junction edit paths are forbidden.
- Use the exact physical workspace path for repository edits to existing files.
- Before each edit of an existing repository file, confirm the source and edit
  paths both start with `{idea_edit_root}` and identify the same existing file.
  If either check fails, write Status: blocked. Never retry against the route
  root or canonical checkout.
- To create a new repository file through `write_file`, use its project-relative
  path under the IDEA MCP create root `{idea_create_root}`, for example
  `{idea_create_root}/path/to/new_file.py`.
- Do not pass its absolute path to `write_file`: IDEA rebases non-existing
  absolute Windows paths against the project root.
- Immediately after creating a repository file, re-read it through its absolute physical path under `{idea_edit_root}`.
- Then inspect `git_status(repo="{workspace_path}")`. Accept either the exact
  new path as untracked (`??`) or an untracked parent directory that contains
  that path; Git may collapse a wholly untracked directory to one status entry.
- When status is collapsed, normalize the reported parent and the absolute
  reread path and verify that the new file is beneath that parent inside the
  assigned workspace. An unrelated untracked directory is not evidence.
- An empty `git_diff` is expected for a new untracked file. Do not stage it
  merely to make the diff visible, and do not treat that empty diff as a routing
  failure. If the absolute reread fails or `git_status` lists neither the exact
  new path nor a covering untracked parent in the assigned workspace, write
  Status: blocked.
- After each edit, re-read the absolute physical path and inspect `git_diff` with
  `repo="{workspace_path}"`. A write without a diff in the assigned physical
  workspace is a routing failure, not a completed edit.
- In the final result, state the exact physical workspace, branch, and HEAD commit.
  Every cited repository path must begin with `{workspace_path}`.
- For coordination files under `{config.coordination_root}`, write to the exact
  absolute paths shown in this prompt: `{progress_path}` and `{result_path}`.
  Do not substitute another task directory. If an absolute coordination path is
  rejected, use the equivalent project-relative `.agent-work/tasks/{task_id}/...`
  path only after verifying the active IDEA project root is exactly
  `{config.coordination_root.parent}`.
- Verify the current directory and branch before editing.
- Do not switch branches in canonical route workspaces.
- Do not install dependencies yourself; slot preparation is handled by the control-plane before the job when configured.
- Do not generate or modify lockfiles.
- Do not suppress diagnostics to make the task look complete.
- If IDEA MCP is unavailable for required edits or diagnostics, write a blocked result.
- If verification requires missing dependencies, report the blocker instead of installing packages.
- If something fails, name the exact tool or command and the exact failure.
- Before the first edit, run `get_problems(path=...)` for every intended target
  file that IDEA can inspect and record the diagnostic baseline in the progress
  file. Do not spend task scope fixing pre-existing diagnostics.
- After the final edit, collect the changed file list and run IDEA MCP diagnostics
  for every changed repository file that the IDE can inspect.
- For each changed file, prefer `get_problems(path=...)` for the pass/fail
  diagnostic check. `get_highlights` is useful for quick-fixes, but it is not
  enough by itself. Compare final diagnostics with the recorded pre-edit baseline.
- Never treat `No highlights found in 0 files analyzed`, `0 files analyzed`, or
  any equivalent zero-file diagnostic result as clean. Treat it as inconclusive,
  retry with `include_unindexed=true` or `get_problems(path=...)`, and report the
  exact inconclusive output if the IDE still cannot analyze the file.
- Do not write Status: completed while any new or worsened task-caused IDE error,
  warning, type warning, lint warning, unresolved import, syntax problem, or
  formatting problem remains.
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
- Keep bounded discovery under roughly 25 IDEA MCP calls after the first progress
  update. For implementation tasks with explicit target files, roughly 60 total
  calls is a progress checkpoint, not a completion boundary: update the progress
  file, stop repeating discovery, and continue through the requested edits and
  verification while successful progress is still being made. Audit-only tasks
  keep the lower cap.
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
- If the diff grows beyond the task scope or contains unrelated formatting churn,
  stop, preserve the changed file list in the progress file, and write a
  Status: partial result instead of continuing. Treat roughly 120 changed lines as
  a review checkpoint, not an automatic stop, when the brief explicitly requires
  the larger change and the diff remains scoped.
- Update the live progress file after each coherent edit phase and before a long
  verification phase. Do not spend a tool call after every tiny edit; preserve
  enough state that a compacted/resumed worker can continue without rediscovery.
- The progress file is an internal handoff artifact; the final result file is
  still mandatory.
- Treat new or worsened unresolved imports as real diagnostics unless a focused
  runtime/linter check proves they are only IDE source-root/indexing noise. Report
  such proven IDE-index issues explicitly under Diagnostics with the exact
  verification command.
- A diagnostic already present in the pre-edit baseline, or proven unrelated,
  stale, IDE-index-only, or outside scope, is non-blocking for this task. Verify it
  with one focused check, report it under Diagnostics, and allow Status: completed
  when no task-caused regression remains.
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


def _idea_create_root(workspace_path: Path, project_root: Path) -> str:
    try:
        relative_path = os.path.relpath(workspace_path, project_root)
    except ValueError as exc:
        raise ValueError(
            "IDEA MCP repository file creation requires the workspace and IDEA "
            "project root to be on the same filesystem volume"
        ) from exc
    return Path(relative_path).as_posix()


def _idea_mcp_settings(config: ControlConfig, route: str) -> tuple[str, str, str]:
    disabled = set(config.defaults.codex_disabled_mcp_servers)
    route_config = config.routes.get(route)
    configured_server = route_config.ide_mcp_server if route_config is not None else None

    if configured_server is not None:
        if configured_server in disabled:
            raise ValueError(
                f"Route {route!r} selects disabled IDEA MCP server {configured_server!r}"
            )
        server = configured_server
    elif "agentbridge_idea_64343" in disabled and "agentbridge_idea_8644" not in disabled:
        server = "agentbridge_idea_8644"
    else:
        server = "agentbridge_idea_64343"

    tool_namespace = f"mcp__{server.replace('-', '_')}__"
    forbidden_servers = tuple(sorted(disabled - {server}))
    if not forbidden_servers:
        agentbridge_servers = {
            "agentbridge_dataspell_8643",
            "agentbridge_idea_64343",
            "agentbridge_idea_8644",
        }
        forbidden_servers = tuple(sorted(agentbridge_servers - {server}))
    forbidden_text = ", ".join(f"`{name}`" for name in forbidden_servers)
    return server, tool_namespace, forbidden_text


def _protocol_path(coordination_root: Path) -> Path:
    candidates = (
        coordination_root / "agent-protocol.md",
        coordination_root / "agy-protocol.md",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    expected = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Required agent protocol not found; expected one of: {expected}")


def _required_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"Required coordination file not found: {path}")
    return path
