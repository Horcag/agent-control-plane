from __future__ import annotations

import tempfile
from pathlib import Path
from types import MappingProxyType

from agent_control_plane.features.slot_lifecycle.lib.ide_modules import ensure_slot_root_ide_module
from agent_control_plane.shared.config import (
    ControlConfig,
    ControlDefaults,
    RouteConfig,
    SlotConfig,
)


def test_shared_slot_module_uses_configured_python_sdk() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        slot_path = root / "slots" / "dev-1"
        slot_path.mkdir(parents=True)
        route = RouteConfig(
            name="dev",
            path=root / "repo",
            required_branch="dev",
            worktree_root=root / "worktrees",
            worktree_base=root / "repo",
            source_roots=(Path("backend"),),
            test_roots=(Path("backend/tests"),),
            exclude_dirs=(),
        )
        slot = SlotConfig(name="dev-1", route="dev", path=slot_path)
        config = ControlConfig(
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
                forbidden_status_globs=(),
                shared_ide_sdk_name="Python 3.12 (project)",
            ),
            routes=MappingProxyType({"dev": route}),
            slots=MappingProxyType({"dev-1": slot}),
            slot_prepare=(),
        )

        result = ensure_slot_root_ide_module(config)

        module_text = result.module_file.read_text(encoding="utf-8")
        assert 'jdkName="Python 3.12 (project)"' in module_text
        assert 'name="Python 3.12 (project) interpreter library"' in module_text
