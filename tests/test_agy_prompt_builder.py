from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from agent_control_plane.features.agent_runner.lib.prompt_builder import build_task_prompt
from agent_control_plane.shared.config import ControlConfig, ControlDefaults, RouteConfig


class AgyPromptBuilderTest(unittest.TestCase):
    def test_agy_uses_native_idea_contract_instead_of_codex_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordination_root = root / ".agent-work"
            task_dir = coordination_root / "tasks" / "agy-task"
            task_dir.mkdir(parents=True)
            (coordination_root / "agent-protocol.md").write_text("# Protocol\n", encoding="utf-8")
            (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
            (task_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")
            workspace = root / "slots" / "dev-1"
            config = ControlConfig(
                config_path=root / "workspaces.toml",
                project_root=root,
                coordination_root=coordination_root,
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
                routes=MappingProxyType(
                    {
                        "dev": RouteConfig(
                            name="dev",
                            path=root / "main",
                            required_branch="dev",
                            worktree_root=root / "worktrees",
                            worktree_base=root / "main",
                            source_roots=(Path("backend"),),
                            test_roots=(Path("backend/tests"),),
                            exclude_dirs=(),
                            ide_mcp_server="ide-mcp-server",
                        )
                    }
                ),
                slots=MappingProxyType({}),
                slot_prepare=(),
            )

            prompt = build_task_prompt(
                config=config,
                task_id="agy-task",
                route="dev",
                workspace_path=workspace,
                expected_branch="slot/dev-1-agy-task",
                result_path=task_dir / "result.md",
                backend="agy",
            )

            self.assertIn("native JetBrains IDEA MCP server `idea`", prompt)
            self.assertIn("mcp__idea__get_repositories", prompt)
            expected_project_root = coordination_root.parent.resolve(strict=False)
            self.assertIn(f'projectPath="{expected_project_root}"', prompt)
            self.assertIn("mcp__idea__read_file", prompt)
            self.assertIn("mcp__idea__apply_patch", prompt)
            self.assertIn("mcp__idea__create_new_file", prompt)
            self.assertIn("mcp__idea__execute_terminal_command", prompt)
            self.assertIn("mcp__idea__get_file_problems", prompt)
            self.assertIn("Git may run only inside that IDEA terminal", prompt)
            self.assertIn("UV_PROJECT_ENVIRONMENT", prompt)
            self.assertIn(f"{workspace.resolve(strict=False)}/.venv", prompt)
            self.assertIn("verify `sys.prefix`", prompt)
            self.assertIn(
                "Never inspect the canonical checkout or another slot as a\n  proxy",
                prompt,
            )
            self.assertIn(
                "inconclusive external-workspace diagnostic requires Status: partial or\n"
                "  Status: blocked",
                prompt,
            )
            self.assertNotIn("mcp__ide_mcp_server__", prompt)
            self.assertNotIn("mcp__agentbridge_idea", prompt)

            external_workspace = root.parent / "external-slot"
            external_prompt = build_task_prompt(
                config=config,
                task_id="agy-task",
                route="dev",
                workspace_path=external_workspace,
                expected_branch="slot/external",
                result_path=task_dir / "result.md",
                backend="agy",
            )
            resolved_external_workspace = external_workspace.resolve(strict=False)
            self.assertIn(f"Workspace path: {resolved_external_workspace}", external_prompt)
            self.assertIn(
                "may be physically outside the host project directory",
                external_prompt,
            )
            self.assertIn(
                f"VCS roots to contain the exact normalized physical\n  workspace "
                f"`{resolved_external_workspace}`",
                external_prompt,
            )

            read_only_prompt = build_task_prompt(
                config=config,
                task_id="agy-task",
                route="dev",
                workspace_path=external_workspace,
                expected_branch="slot/external",
                result_path=task_dir / "result.md",
                backend="agy",
                read_only=True,
            )
            self.assertIn("This is a read-only job", read_only_prompt)
            self.assertIn(f"Workspace path: {resolved_external_workspace}", read_only_prompt)

            agentbridge_route = replace(
                config.routes["dev"],
                agy_mcp_server="agentbridge-ide",
            )
            agentbridge_config = replace(
                config,
                routes=MappingProxyType({"dev": agentbridge_route}),
            )
            agentbridge_prompt = build_task_prompt(
                config=agentbridge_config,
                task_id="agy-task",
                route="dev",
                workspace_path=external_workspace,
                expected_branch="slot/external",
                result_path=task_dir / "result.md",
                backend="agy",
            )

            self.assertIn("AgentBridge MCP server `agentbridge-ide`", agentbridge_prompt)
            self.assertIn("`get_project_info`", agentbridge_prompt)
            self.assertIn("`git_status`", agentbridge_prompt)
            self.assertIn("`edit_text`", agentbridge_prompt)
            self.assertIn("dedicated `git_*` tools exclusively", agentbridge_prompt)
            self.assertIn("Junctions, symlinks, directory links", agentbridge_prompt)
            self.assertNotIn("mcp__idea__get_repositories", agentbridge_prompt)


if __name__ == "__main__":
    unittest.main()
