from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.prompt_builder import (
    _idea_create_root,
    build_task_prompt,
)
from agent_control_plane.shared.config import ControlConfig, ControlDefaults, RouteConfig


class PromptBuilderTest(unittest.TestCase):
    def test_idea_create_root_reports_cross_volume_error(self) -> None:
        with (
            patch(
                "agent_control_plane.features.agent_runner.lib.prompt_builder.os.path.relpath",
                side_effect=ValueError("different drives"),
            ),
            self.assertRaisesRegex(ValueError, "same filesystem volume"),
        ):
            _idea_create_root(Path("D:/slot"), Path("C:/project"))

    def test_prompt_contains_route_paths_and_idea_mcp_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _coordination_files(root, "task-1")
            config = ControlConfig(
                config_path=root / "workspaces.toml",
                project_root=root,
                coordination_root=root / ".agent-work",
                runs_root=root / "runs",
                database_path=root / "runs" / "jobs.sqlite3",
                worktree_root=root / "worktrees",
                worktree_base=root / "main",
                slot_root=root / "slots",
                agy_command="agy",
                codex_command="codex",
                defaults=ControlDefaults(
                    timeout_sec=10,
                    idle_timeout_sec=5,
                    print_timeout="10s",
                    max_restarts=0,
                    yolo=False,
                    allow_dirty=False,
                    prepare_slots=True,
                    guardrail_poll_sec=2.0,
                    forbidden_status_globs=("uv.lock", ".venv/**"),
                ),
                routes=MappingProxyType(
                    {
                        "main": RouteConfig(
                            name="main",
                            path=root / "main",
                            required_branch="main",
                            worktree_root=root / "worktrees",
                            worktree_base=root / "main",
                            source_roots=(Path("backend"), Path("frontend/src")),
                            test_roots=(Path("backend/tests"),),
                            exclude_dirs=(),
                        )
                    }
                ),
                slots=MappingProxyType({}),
                slot_prepare=(),
            )

            workspace = root.parent / "main-tiger-agent-slots" / "dev-3"
            prompt = build_task_prompt(
                config=config,
                task_id="task-1",
                route="main",
                workspace_path=workspace,
                expected_branch="review/pr",
                result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
            )

            self.assertIn("Workspace route: main", prompt)
            self.assertIn("Expected branch: review/pr", prompt)
            self.assertIn("Use only the IDEA MCP server `agentbridge_idea_64343`", prompt)
            self.assertIn(
                f"Expected IDEA MCP project root: {root.resolve(strict=False)}",
                prompt,
            )
            self.assertIn(
                "first IDEA MCP call must be\n  `mcp__agentbridge_idea_64343__get_project_info`",
                prompt,
            )
            self.assertIn("If the IDEA MCP project root differs", prompt)
            self.assertIn("call `mcp__agentbridge_idea_64343__read_file`", prompt)
            self.assertIn("Only use `tool_search` as an optional fallback", prompt)
            self.assertIn("mcp__agentbridge_idea_64343__*", prompt)
            self.assertIn(
                "`agentbridge_dataspell_8643`, `agentbridge_idea_8644` are forbidden", prompt
            )
            self.assertIn(
                f"IDEA MCP edit root: {workspace.resolve(strict=False)}",
                prompt,
            )
            self.assertIn(
                "IDEA MCP create root: ../main-tiger-agent-slots/dev-3",
                prompt,
            )
            self.assertIn("existing repository files by their absolute physical paths", prompt)
            self.assertIn("normal\n  surgical edits", prompt)
            self.assertIn("explicitly authorizes a whole-target rewrite", prompt)
            self.assertIn("preview the proposed content with `show_diff`", prompt)
            self.assertIn("even though the target already exists", prompt)
            self.assertIn("same existing physical file", prompt)
            self.assertIn("new repository file through `write_file`", prompt)
            self.assertIn("Never pass a non-existing file's absolute Windows path", prompt)
            self.assertIn("re-read it through its absolute physical path", prompt)
            self.assertIn("inspect `git_diff` with", prompt)
            self.assertIn(f'`git_status(repo="{workspace}")`', prompt)
            self.assertIn("empty `git_diff` is expected", prompt)
            self.assertIn("Accept either the exact", prompt)
            self.assertIn("new path as untracked (`??`)", prompt)
            self.assertIn("untracked parent directory that contains", prompt)
            self.assertIn("Git may collapse a wholly untracked directory", prompt)
            self.assertIn("An unrelated untracked directory is not evidence", prompt)
            self.assertIn("exact absolute physical workspace prefix", prompt)
            self.assertIn("Do not use project-wide `search_text`", prompt)
            self.assertIn("path-scoped `rg`/`rg --files`", prompt)
            self.assertIn("except for the verified `write_file` path described below", prompt)
            self.assertIn("canonical checkout", prompt)
            self.assertIn("forbidden tool usage", prompt)
            self.assertIn("mcp__agentbridge_idea_64343__run_in_terminal", prompt)
            self.assertIn("UV_PROJECT_ENVIRONMENT", prompt)
            self.assertIn(f"{workspace}/.venv", prompt)
            self.assertIn("Verify `sys.prefix`", prompt)
            self.assertNotIn("mcp__agentbridge_idea_8644", prompt)
            self.assertIn("Do not install dependencies", prompt)
            self.assertIn("Always write the result file", prompt)
            self.assertIn("Maintain live progress/state", prompt)
            self.assertIn(str(Path("D:/repo/.agent-work/tasks/task-1/result.md")), prompt)
            self.assertIn("exact progress file path", prompt)
            self.assertIn("absolute paths shown in this prompt", prompt)
            self.assertIn("equivalent project-relative `.agent-work/tasks/", prompt)
            self.assertIn("Synthetic junction edit paths remain forbidden", prompt)
            self.assertIn("agent-progress.md", prompt)
            self.assertIn("At the start of every resumed or compacted turn", prompt)
            self.assertIn("after at most three focused search/read rounds", prompt)
            self.assertIn("Keep each search/read round narrow", prompt)
            self.assertIn("returns exit code 124", prompt)
            self.assertIn("For smoke/audit tasks", prompt)
            self.assertIn("bounded discovery under roughly 25 IDEA MCP calls", prompt)
            self.assertIn("roughly 60 total", prompt)
            self.assertIn("progress checkpoint, not a completion boundary", prompt)
            self.assertIn("next action must be writing result.md", prompt)
            self.assertIn("The brief defines the bounded scope", prompt)
            self.assertIn("Do not return partial solely because of diff size", prompt)
            self.assertIn("A whole-target-file rewrite or IDEA `write_file` is permitted", prompt)
            self.assertIn("failed surgical retry permits IDEA", prompt)
            self.assertNotIn("If the task is too broad for one bounded change", prompt)
            self.assertNotIn("Do not rewrite whole files", prompt)
            self.assertIn("unrelated formatting churn", prompt)
            self.assertIn("review checkpoint, not an automatic stop", prompt)
            self.assertIn("Status: completed", prompt)
            self.assertIn("Status: partial", prompt)
            self.assertIn("Status: blocked", prompt)
            self.assertIn("Never finish with only terminal output", prompt)
            self.assertIn("run IDEA MCP diagnostics", prompt)
            self.assertIn("get_problems(path=...)", prompt)
            self.assertIn("0 files analyzed", prompt)
            self.assertIn("inconclusive", prompt)
            self.assertIn("record the diagnostic baseline", prompt)
            self.assertIn("Compare final diagnostics", prompt)
            self.assertIn("Treat new or worsened unresolved imports", prompt)
            self.assertIn("is non-blocking for this task", prompt)
            self.assertIn("Do not write Status: completed", prompt)
            self.assertIn("formatting problem", prompt)

            agentbridge_config = replace(
                config,
                defaults=replace(
                    config.defaults,
                    codex_disabled_mcp_servers=(
                        "agentbridge_dataspell_8643",
                        "agentbridge_idea_64343",
                    ),
                ),
            )
            agentbridge_prompt = build_task_prompt(
                config=agentbridge_config,
                task_id="task-1",
                route="main",
                workspace_path=workspace,
                expected_branch="review/pr",
                result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
            )
            self.assertIn("IDEA MCP server `agentbridge_idea_8644`", agentbridge_prompt)
            self.assertIn("mcp__agentbridge_idea_8644__get_project_info", agentbridge_prompt)
            self.assertIn("mcp__agentbridge_idea_8644__read_file", agentbridge_prompt)
            self.assertIn(
                "`agentbridge_dataspell_8643`, `agentbridge_idea_64343` are forbidden",
                agentbridge_prompt,
            )
            self.assertNotIn("mcp__agentbridge_idea_64343__read_file", agentbridge_prompt)

            named_server = "agentbridge_idea_8644"
            named_config = replace(
                agentbridge_config,
                routes=MappingProxyType(
                    {
                        "main": replace(
                            config.routes["main"],
                            ide_mcp_server=named_server,
                            ide_mcp_project_root=root / "hhru-idea-project",
                        )
                    }
                ),
            )
            named_prompt = build_task_prompt(
                config=named_config,
                task_id="task-1",
                route="main",
                workspace_path=workspace,
                expected_branch="review/pr",
                result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
            )
            self.assertIn(
                f"IDEA MCP server `{named_server}`",
                named_prompt,
            )
            self.assertIn(
                "mcp__agentbridge_idea_8644__get_project_info",
                named_prompt,
            )
            self.assertIn(
                f"Expected IDEA MCP project root: {(root / 'hhru-idea-project').resolve(strict=False)}",
                named_prompt,
            )

            disabled_named_config = replace(
                named_config,
                defaults=replace(
                    named_config.defaults,
                    codex_disabled_mcp_servers=(named_server,),
                ),
            )
            with self.assertRaisesRegex(ValueError, "selects disabled IDEA MCP server"):
                build_task_prompt(
                    config=disabled_named_config,
                    task_id="task-1",
                    route="main",
                    workspace_path=workspace,
                    expected_branch="review/pr",
                    result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
                )

    def test_prompt_uses_absolute_edit_root_and_relative_create_root_for_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _coordination_files(root, "task-1", protocol_name="agy-protocol.md")
            config = ControlConfig(
                config_path=root / "workspaces.toml",
                project_root=root,
                coordination_root=root / ".agent-work",
                runs_root=root / "runs",
                database_path=root / "runs" / "jobs.sqlite3",
                worktree_root=root / "worktrees",
                worktree_base=root / "main",
                slot_root=root / "slots",
                agy_command="agy",
                codex_command="codex",
                defaults=ControlDefaults(
                    timeout_sec=10,
                    idle_timeout_sec=5,
                    print_timeout="10s",
                    max_restarts=0,
                    yolo=False,
                    allow_dirty=False,
                    prepare_slots=True,
                    guardrail_poll_sec=2.0,
                    forbidden_status_globs=("uv.lock", ".venv/**"),
                ),
                routes=MappingProxyType({}),
                slots=MappingProxyType({}),
                slot_prepare=(),
            )

            prompt = build_task_prompt(
                config=config,
                task_id="task-1",
                route="main",
                workspace_path=root / "slots" / "work-slot-11",
                expected_branch="slot/work-slot-11",
                result_path=root / ".agent-work" / "tasks" / "task-1" / "result.md",
            )

            workspace = root / "slots" / "work-slot-11"
            self.assertIn(
                f"IDEA MCP edit root: {workspace.resolve(strict=False)}",
                prompt,
            )
            self.assertIn("IDEA MCP create root: slots/work-slot-11", prompt)
            self.assertIn(str(root / ".agent-work" / "agy-protocol.md"), prompt)
            self.assertNotIn("slot-links", prompt)
            self.assertNotIn("Do not pass direct absolute slot paths", prompt)

    def test_prompt_requires_existing_coordination_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ControlConfig(
                config_path=root / "workspaces.toml",
                project_root=root,
                coordination_root=root / ".agent-work",
                runs_root=root / "runs",
                database_path=root / "runs" / "jobs.sqlite3",
                worktree_root=root / "worktrees",
                worktree_base=root / "main",
                slot_root=root / "slots",
                agy_command="agy",
                codex_command="codex",
                defaults=ControlDefaults(
                    timeout_sec=10,
                    idle_timeout_sec=5,
                    print_timeout="10s",
                    max_restarts=0,
                    yolo=False,
                    allow_dirty=False,
                    prepare_slots=False,
                    guardrail_poll_sec=2.0,
                    forbidden_status_globs=(),
                ),
                routes=MappingProxyType({}),
                slots=MappingProxyType({}),
                slot_prepare=(),
            )

            with self.assertRaisesRegex(FileNotFoundError, "agent-protocol.md"):
                build_task_prompt(
                    config=config,
                    task_id="task-1",
                    route="main",
                    workspace_path=root / "repo",
                    expected_branch="main",
                    result_path=root / ".agent-work" / "tasks" / "task-1" / "result.md",
                )

            coordination_root = root / ".agent-work"
            coordination_root.mkdir(parents=True)
            (coordination_root / "agent-protocol.md").write_text(
                "# Protocol\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FileNotFoundError, "workspace-routing.md"):
                build_task_prompt(
                    config=config,
                    task_id="task-1",
                    route="main",
                    workspace_path=root / "repo",
                    expected_branch="main",
                    result_path=root / ".agent-work" / "tasks" / "task-1" / "result.md",
                )

            (coordination_root / "workspace-routing.md").write_text(
                "# Routing\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FileNotFoundError, "brief.md"):
                build_task_prompt(
                    config=config,
                    task_id="task-1",
                    route="main",
                    workspace_path=root / "repo",
                    expected_branch="main",
                    result_path=root / ".agent-work" / "tasks" / "task-1" / "result.md",
                )


def _coordination_files(
    root: Path,
    task_id: str,
    *,
    protocol_name: str = "agent-protocol.md",
) -> None:
    coordination_root = root / ".agent-work"
    task_dir = coordination_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (coordination_root / protocol_name).write_text("# Protocol\n", encoding="utf-8")
    (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
    (task_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
