from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path
from types import MappingProxyType

from agent_control_plane.features.slot_lifecycle.lib.ide_inspections import (
    ensure_duplicate_inspection_same_module,
)
from agent_control_plane.shared.config import ControlConfig, ControlDefaults


class IdeInspectionsTest(unittest.TestCase):
    def test_limits_duplicate_candidates_to_the_current_module(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = ensure_duplicate_inspection_same_module(_config(root))

            profile = ET.parse(result.profile_file).getroot()  # nosec B314
            tool = profile.find(".//inspection_tool[@class='DuplicatedCode']")
            self.assertIsNotNone(tool)
            assert tool is not None
            self.assertEqual(tool.get("enabled"), "true")
            self.assertEqual(tool.get("enabled_by_default"), "true")
            self.assertEqual(tool.get("level"), "WEAK WARNING")
            global_settings = tool.find("GlobalSettings")
            self.assertIsNotNone(global_settings)
            assert global_settings is not None
            self.assertEqual(
                global_settings.get("restrictedDuplicateScope"),
                "SAME_MODULE",
            )
            self.assertFalse((root / ".idea" / "scopes" / "Agent_Slots.xml").exists())

    def test_is_idempotent_and_preserves_unrelated_profile_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            idea_dir = root / ".idea"
            profile_file = idea_dir / "inspectionProfiles" / "Project_Default.xml"
            profile_file.parent.mkdir(parents=True)
            profile_file.write_text(
                """<component name="InspectionProjectProfileManager">
  <profile version="1.0">
    <option name="myName" value="Project Default" />
    <inspection_tool class="DuplicatedCode" enabled="false" level="WARNING" enabled_by_default="false">
      <option name="MINIMUM_SIZE" value="123" />
      <GlobalSettings lowerBound="77" />
      <Languages>
        <language minSize="42" name="Python" />
      </Languages>
    </inspection_tool>
    <inspection_tool class="SpellCheckingInspection" enabled="false" level="TYPO" enabled_by_default="false" />
  </profile>
</component>
""",
                encoding="utf-8",
            )

            first = ensure_duplicate_inspection_same_module(_config(root))
            first_profile_text = profile_file.read_text(encoding="utf-8")
            second = ensure_duplicate_inspection_same_module(_config(root))

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(profile_file.read_text(encoding="utf-8"), first_profile_text)

            profile_root = ET.parse(profile_file).getroot()  # nosec B314
            tool = profile_root.find(".//inspection_tool[@class='DuplicatedCode']")
            assert tool is not None
            self.assertEqual(tool.get("enabled"), "true")
            self.assertEqual(tool.get("enabled_by_default"), "true")
            self.assertEqual(tool.get("level"), "WEAK WARNING")
            self.assertIsNotNone(tool.find("option[@name='MINIMUM_SIZE']"))
            global_settings = tool.find("GlobalSettings")
            assert global_settings is not None
            self.assertEqual(global_settings.get("lowerBound"), "77")
            self.assertEqual(
                global_settings.get("restrictedDuplicateScope"),
                "SAME_MODULE",
            )
            self.assertIsNotNone(tool.find("Languages/language[@name='Python']"))
            self.assertIsNotNone(
                profile_root.find(".//inspection_tool[@class='SpellCheckingInspection']")
            )


def _config(root: Path) -> ControlConfig:
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
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
        routes=MappingProxyType({}),
        slots=MappingProxyType({}),
        slot_prepare=(),
    )


if __name__ == "__main__":
    unittest.main()
