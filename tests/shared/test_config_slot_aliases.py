from pathlib import Path

import pytest

from agent_control_plane.shared.config import load_config


@pytest.mark.parametrize("symlink", [False, True])
def test_config_rejects_duplicate_physical_slot_paths(tmp_path: Path, symlink: bool) -> None:
    slot = tmp_path / "slots" / "first"
    slot.mkdir(parents=True)
    alias = tmp_path / "slots" / "second"
    if symlink:
        alias.symlink_to(slot, target_is_directory=True)
    else:
        alias = slot
    config_path = tmp_path / "workspaces.toml"
    config_path.write_text(
        '[control]\ncoordination_root=".agent-work"\nruns_root="runs"\n'
        'database="db"\nworktree_root="worktrees"\nworktree_base="repo"\nslot_root="slots"\n'
        '[routes.app]\npath="repo"\nrequired_branch="main"\n'
        f'[slots.first]\nroute="app"\npath="{slot}"\n'
        f'[slots.second]\nroute="app"\npath="{alias}"\n'
    )
    with pytest.raises(ValueError, match="duplicates physical slot path"):
        load_config(config_path)
