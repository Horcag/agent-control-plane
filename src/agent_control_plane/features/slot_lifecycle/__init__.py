from agent_control_plane.features.slot_lifecycle.lib.config_bootstrap import (
    ConfigBootstrapError,
    ConfigBootstrapResult,
    RepoLayout,
    bootstrap_slot_config,
    infer_repo_layout,
)
from agent_control_plane.features.slot_lifecycle.lib.ide_modules import (
    IdeModuleError,
    IdeModuleResult,
    ensure_slot_ide_module,
    ensure_slot_ide_vcs_mappings,
    ensure_slot_root_ide_module,
    remove_slot_ide_module,
    remove_slot_root_ide_module,
    unload_slot_ide_module,
    unload_slot_root_ide_module,
)
from agent_control_plane.features.slot_lifecycle.lib.slot_manager import (
    CleanupDecision,
    SlotError,
    SlotManager,
    SlotStatus,
)
from agent_control_plane.features.slot_lifecycle.lib.slot_prepare import (
    SlotPrepareError,
    prepare_workspace_slot,
)
from agent_control_plane.features.slot_lifecycle.lib.worktree_manager import (
    WorktreeError,
    WorktreeSpec,
    create_worktree,
    remove_worktree,
)

__all__ = [
    "CleanupDecision",
    "ConfigBootstrapError",
    "ConfigBootstrapResult",
    "IdeModuleError",
    "IdeModuleResult",
    "RepoLayout",
    "SlotError",
    "SlotManager",
    "SlotPrepareError",
    "SlotStatus",
    "WorktreeError",
    "WorktreeSpec",
    "bootstrap_slot_config",
    "create_worktree",
    "ensure_slot_ide_module",
    "ensure_slot_ide_vcs_mappings",
    "ensure_slot_root_ide_module",
    "infer_repo_layout",
    "prepare_workspace_slot",
    "remove_slot_ide_module",
    "remove_slot_root_ide_module",
    "remove_worktree",
    "unload_slot_ide_module",
    "unload_slot_root_ide_module",
]
