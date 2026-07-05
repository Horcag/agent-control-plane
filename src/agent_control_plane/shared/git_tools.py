from __future__ import annotations

import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitWorkspaceState:
    branch: str
    porcelain: str

    @property
    def dirty(self) -> bool:
        return bool(self.porcelain.strip())


def run_git(path: Path, *args: str) -> str:
    proc = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(path), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"git exited {proc.returncode}"
        raise GitError(detail)
    return proc.stdout.strip()


def workspace_state(path: Path) -> GitWorkspaceState:
    return GitWorkspaceState(
        branch=run_git(path, "branch", "--show-current"),
        porcelain=run_git(path, "status", "--porcelain=v1", "-uall"),
    )


def diff_patch(path: Path) -> str:
    return run_git(path, "diff", "--binary")


def is_git_workspace(path: Path) -> bool:
    try:
        output = run_git(path, "rev-parse", "--is-inside-work-tree")
    except GitError:
        return False
    return output.strip().lower() == "true"
