from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

from agent_control_plane.features.slot_lifecycle.lib.ide_modules import (
    ensure_slot_ide_module,
    ensure_slot_ide_vcs_mappings,
    ensure_slot_root_ide_module,
    remove_slot_ide_module,
    unload_slot_ide_module,
)
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
)


class IdeModulesTest(unittest.TestCase):
    def test_ensure_slot_ide_module_writes_iml_and_modules_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            result = ensure_slot_ide_module(config, config.slots["dev-1"])

            self.assertTrue(result.changed)
            self.assertTrue(result.loaded)
            self.assertTrue(result.module_file.exists())
            self.assertIn("frontend/node_modules", result.module_file.read_text(encoding="utf-8"))
            modules_text = result.modules_xml.read_text(encoding="utf-8")
            self.assertIn("$PROJECT_DIR$/.agent-work/agentbridge-slot-dev-1.iml", modules_text)
            workspace_text = result.workspace_xml.read_text(encoding="utf-8")
            self.assertIn('<module name="agentbridge-slot-dev-1" />', workspace_text)
            self.assertNotIn(
                '<component name="UnloadedModulesList">\n    <module name="agentbridge-slot-dev-1" />',
                workspace_text,
            )

    def test_ensure_slot_ide_module_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            ensure_slot_ide_module(config, config.slots["dev-1"])
            second = ensure_slot_ide_module(config, config.slots["dev-1"])

            self.assertFalse(second.changed)
            modules_text = second.modules_xml.read_text(encoding="utf-8")
            self.assertEqual(modules_text.count("agentbridge-slot-dev-1.iml"), 2)

    def test_unload_slot_ide_module_keeps_project_entry_and_marks_unloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            ensure_slot_ide_module(config, config.slots["dev-1"])
            result = unload_slot_ide_module(config, "dev-1")

            self.assertTrue(result.changed)
            self.assertFalse(result.loaded)
            self.assertTrue(result.present)
            self.assertIn(
                "agentbridge-slot-dev-1.iml", result.modules_xml.read_text(encoding="utf-8")
            )
            workspace_text = result.workspace_xml.read_text(encoding="utf-8")
            self.assertIn('<component name="UnloadedModulesList">', workspace_text)
            self.assertIn('<module name="agentbridge-slot-dev-1" />', workspace_text)

    def test_ensure_loads_previously_unloaded_module_without_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            ensure_slot_ide_module(config, config.slots["dev-1"])
            unload_slot_ide_module(config, "dev-1")
            result = ensure_slot_ide_module(config, config.slots["dev-1"])

            workspace_text = result.workspace_xml.read_text(encoding="utf-8")
            self.assertTrue(result.loaded)
            self.assertEqual(workspace_text.count('name="agentbridge-slot-dev-1"'), 1)

    def test_ensure_slot_root_ide_module_writes_single_container_module(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            result = ensure_slot_root_ide_module(config)

            self.assertTrue(result.changed)
            self.assertEqual(result.module_name, "agentbridge-slots-root")
            self.assertTrue(result.loaded)
            self.assertTrue(result.module_file.exists())
            module_text = result.module_file.read_text(encoding="utf-8")
            self.assertIn('<content url="file://$MODULE_DIR$/../slots/dev-1">', module_text)
            self.assertIn("dev-1/frontend/node_modules", module_text)
            modules_text = result.modules_xml.read_text(encoding="utf-8")
            self.assertIn("$PROJECT_DIR$/.agent-work/agentbridge-slots-root.iml", modules_text)
            self.assertNotIn("agentbridge-slot-dev-1.iml", modules_text)
            workspace_text = result.workspace_xml.read_text(encoding="utf-8")
            self.assertIn('<module name="agentbridge-slots-root" />', workspace_text)

    def test_ensure_slot_root_ide_module_supports_sibling_slot_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            family_root = Path(temp)
            control_root = family_root / "agent-control-plane"
            slot_root = family_root / "project-agent-slots"
            slot_path = slot_root / "work-slot-1"
            slot_path.mkdir(parents=True)
            base_config = _config(control_root, slot_path)
            slot = SlotConfig(name="work-slot-1", route="dev", path=slot_path)
            config = replace(
                base_config,
                coordination_root=control_root / ".slots" / "coordination",
                runs_root=control_root / ".slots" / "runs",
                database_path=control_root / ".slots" / "jobs.sqlite3",
                worktree_root=slot_root,
                slot_root=slot_root,
                slots=MappingProxyType({"work-slot-1": slot}),
            )

            result = ensure_slot_root_ide_module(config)
            vcs_result = ensure_slot_ide_vcs_mappings(config)

            module_text = result.module_file.read_text(encoding="utf-8")
            self.assertIn(
                '<content url="file://$MODULE_DIR$/../../../project-agent-slots/work-slot-1">',
                module_text,
            )
            vcs_text = Path(cast(str, vcs_result["vcs_xml"])).read_text(encoding="utf-8")
            self.assertIn("$PROJECT_DIR$/../../project-agent-slots/work-slot-1", vcs_text)

    def test_ensure_slot_root_ide_module_excludes_route_specific_sdk_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "reports-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path, route="reports")

            result = ensure_slot_root_ide_module(config)

            module_text = result.module_file.read_text(encoding="utf-8")
            self.assertNotIn("reports-1", module_text)
            self.assertNotIn("reports-1/backend/src", module_text)
            self.assertNotIn("reports-1/frontend/src", module_text)
            self.assertNotIn("reports-1/scripts", module_text)
            self.assertNotIn("reports-1/backend/tests", module_text)
            self.assertNotIn("reports-1/frontend/tests", module_text)

    def test_ensure_slot_ide_module_uses_route_specific_sdk_and_source_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "reports-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path, route="reports")

            result = ensure_slot_ide_module(config, config.slots["reports-1"])

            module_text = result.module_file.read_text(encoding="utf-8")
            self.assertIn('jdkName="Python 3.12 (.venv)"', module_text)
            self.assertIn('reports-1" isTestSource="false', module_text)
            self.assertIn('reports-1/frontend" isTestSource="false', module_text)
            self.assertIn("reports-1/scripts", module_text)
            self.assertIn("reports-1/frontend/tests", module_text)

    def test_ensure_slot_ide_vcs_mappings_adds_each_configured_slot_as_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            result = ensure_slot_ide_vcs_mappings(config)

            self.assertTrue(result["changed"])
            vcs_text = (root / ".idea" / "vcs.xml").read_text(encoding="utf-8")
            self.assertIn("$PROJECT_DIR$/slots/dev-1", vcs_text)
            self.assertIn('vcs="Git"', vcs_text)

    def test_remove_slot_ide_module_removes_legacy_module_from_project_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slot_path = root / "slots" / "dev-1"
            slot_path.mkdir(parents=True)
            config = _config(root, slot_path)

            ensure_slot_ide_module(config, config.slots["dev-1"])
            result = remove_slot_ide_module(config, "dev-1")

            self.assertTrue(result.changed)
            self.assertFalse(result.present)
            self.assertFalse(result.loaded)
            modules_text = result.modules_xml.read_text(encoding="utf-8")
            self.assertNotIn("agentbridge-slot-dev-1.iml", modules_text)
            workspace_text = result.workspace_xml.read_text(encoding="utf-8")
            self.assertNotIn('name="agentbridge-slot-dev-1"', workspace_text)


def _config(root: Path, slot_path: Path, route: str = "dev") -> ControlConfig:
    coordination_root = root / ".agent-work"
    slot_name = slot_path.name
    slot = SlotConfig(name=slot_name, route=route, path=slot_path)
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=coordination_root,
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=root / "repo",
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
                "dev": RouteConfig(
                    name="dev",
                    path=root / "repo",
                    required_branch="dev",
                    worktree_root=root / "worktrees",
                    worktree_base=root / "repo",
                    source_roots=(Path("backend"), Path("frontend/src")),
                    test_roots=(Path("backend/tests"),),
                    exclude_dirs=(),
                ),
                "reports": RouteConfig(
                    name="reports",
                    path=root / "reports",
                    required_branch="release",
                    worktree_root=root / "worktrees",
                    worktree_base=root / "reports",
                    source_roots=(
                        Path("."),
                        Path("backend/src"),
                        Path("frontend"),
                        Path("frontend/src"),
                        Path("scripts"),
                    ),
                    test_roots=(Path("backend/tests"), Path("frontend/tests")),
                    exclude_dirs=(Path("frontend/build"),),
                    ide_sdk_name="Python 3.12 (.venv)",
                ),
            }
        ),
        slots=MappingProxyType({slot_name: slot}),
        slot_prepare=(),
    )


if __name__ == "__main__":
    unittest.main()
