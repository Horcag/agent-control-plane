from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.git_tools import GitError, run_git, workspace_state
from agent_control_plane.shared.path_rules import is_same_or_child


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeSpec:
    base_repo: Path
    worktree_root: Path
    worktree_path: Path
    branch: str
    start_point: str


def create_worktree(spec: WorktreeSpec) -> None:
    base_repo = spec.base_repo.resolve(strict=False)
    worktree_root = spec.worktree_root.resolve(strict=False)
    worktree_path = spec.worktree_path.resolve(strict=False)

    if not is_same_or_child(worktree_path, worktree_root):
        raise WorktreeError(f"Worktree path is outside allowed root: {worktree_path}")
    if worktree_path.exists() and any(worktree_path.iterdir()):
        raise WorktreeError(f"Worktree path already exists and is not empty: {worktree_path}")

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    run_git(base_repo, "worktree", "add", "-b", spec.branch, str(worktree_path), spec.start_point)


def remove_worktree(base_repo: Path, worktree_path: Path, *, allow_dirty: bool = False) -> None:
    worktree_path = worktree_path.resolve(strict=False)
    if not worktree_path.exists():
        return

    try:
        state = workspace_state(worktree_path)
    except GitError as exc:
        raise WorktreeError(f"Could not inspect worktree before removal: {exc}") from exc

    if state.dirty and not allow_dirty:
        raise WorktreeError(f"Refusing to remove dirty worktree: {worktree_path}")

    args = ["worktree", "remove"]
    if allow_dirty:
        args.append("--force")
    args.append(str(worktree_path))
    run_git(base_repo.resolve(strict=False), *args)
