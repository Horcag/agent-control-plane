from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.slot_lifecycle.lib.config_bootstrap import bootstrap_slot_config
from agent_control_plane.shared.config import load_config


class ConfigBootstrapTest(unittest.TestCase):
    def test_bootstrap_adds_slot_for_existing_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_base_config(root)
            config = load_config(config_path)

            result = bootstrap_slot_config(
                config,
                slot_name="dev-2",
                route_name="dev",
            )

            self.assertTrue(result.changed)
            self.assertFalse(result.route_added)
            self.assertTrue(result.slot_added)
            self.assertEqual(result.source_roots, (Path("backend"), Path("frontend/src")))
            self.assertEqual(result.test_roots, (Path("backend/tests"),))
            updated = config_path.read_text(encoding="utf-8")
            self.assertIn('[slots."dev-2"]', updated)
            self.assertIn('route = "dev"', updated)
            self.assertIn('/slots/dev-2"', updated)

    def test_bootstrap_adds_new_route_from_repo_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_base_config(root)
            repo = root / "reports"
            (repo / "backend" / "src").mkdir(parents=True)
            (repo / "frontend" / "src").mkdir(parents=True)
            (repo / "backend" / "tests").mkdir(parents=True)
            (repo / "frontend" / "tests").mkdir(parents=True)
            (repo / "frontend" / "build").mkdir(parents=True)
            (repo / "reports.iml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<module type="PYTHON_MODULE" version="4">
  <component name="NewModuleRootManager">
    <content url="file://$MODULE_DIR$">
      <sourceFolder url="file://$MODULE_DIR$/backend/src" isTestSource="false" />
      <sourceFolder url="file://$MODULE_DIR$/frontend/src" isTestSource="false" />
      <excludeFolder url="file://$MODULE_DIR$/frontend/build" />
    </content>
  </component>
</module>
""",
                encoding="utf-8",
            )
            config = load_config(config_path)

            result = bootstrap_slot_config(
                config,
                slot_name="reports-1",
                route_name="reports",
                repo_path=repo,
                required_branch="release",
            )

            self.assertTrue(result.changed)
            self.assertTrue(result.route_added)
            self.assertTrue(result.slot_added)
            self.assertEqual(result.source_roots, (Path("backend/src"), Path("frontend/src")))
            self.assertIn(Path("backend/tests"), result.test_roots)
            self.assertIn(Path("frontend/tests"), result.test_roots)
            self.assertIn(Path("frontend/build"), result.exclude_dirs)
            updated = config_path.read_text(encoding="utf-8")
            self.assertIn("[routes.reports]", updated)
            self.assertIn('worktree_base = "', updated)
            self.assertIn('source_roots = ["backend/src", "frontend/src"]', updated)
            self.assertIn('[slots."reports-1"]', updated)
            # A new route never inherits another project's slot directory.
            sibling_root = (root / "reports-agent-slots").as_posix()
            self.assertEqual(result.slot_path, root / "reports-agent-slots" / "reports-1")
            self.assertIn(f'slot_root = "{sibling_root}"', updated)
            self.assertIn(f'worktree_root = "{sibling_root}"', updated)
            self.assertNotIn(f'"{(root / "slots").as_posix()}/reports-1"', updated)

    def test_bootstrap_uses_route_slot_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_base_config(root)
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + f"""
[routes.other]
path = "{(root / "repo").as_posix()}"
required_branch = "main"
slot_root = "{(root / "other-slots").as_posix()}"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)

            result = bootstrap_slot_config(config, slot_name="other-1", route_name="other")

            self.assertEqual(result.slot_path, root / "other-slots" / "other-1")


def _write_base_config(root: Path) -> Path:
    config_path = root / "workspaces.toml"
    (root / "repo").mkdir()
    (root / "slots").mkdir()
    config_path.write_text(
        f"""
[control]
coordination_root = "{(root / ".agent-work").as_posix()}"
runs_root = "{(root / "runs").as_posix()}"
database = "{(root / "runs" / "jobs.sqlite3").as_posix()}"
worktree_root = "{(root / "worktrees").as_posix()}"
worktree_base = "{(root / "repo").as_posix()}"
slot_root = "{(root / "slots").as_posix()}"
agy_command = "agy"

[routes.dev]
path = "{(root / "repo").as_posix()}"
required_branch = "dev"
worktree_root = "{(root / "worktrees").as_posix()}"

[slots."dev-1"]
route = "dev"
path = "{(root / "slots" / "dev-1").as_posix()}"
""",
        encoding="utf-8",
    )
    return config_path


if __name__ == "__main__":
    unittest.main()
