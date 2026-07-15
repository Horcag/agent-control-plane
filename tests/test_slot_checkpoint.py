from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_control_plane.features.result_handoff import (
    SlotCheckpointError,
    clean_checkpointed_workspace,
    create_slot_checkpoint,
)
from agent_control_plane.shared.git_tools import run_git, workspace_state


def test_checkpoint_captures_all_non_ignored_changes_without_moving_head(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "repo")
    base_sha = run_git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _run(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("staged and unstaged\n", encoding="utf-8")
    (repo / "untracked.bin").write_bytes(b"\x00checkpoint\xff")
    (repo / "deleted.txt").unlink()
    (repo / "ignored.txt").write_text("keep me\n", encoding="utf-8")

    checkpoint = create_slot_checkpoint(
        repo,
        job_id="job/with unsafe ref chars",
        task_id="terminal-task",
        terminal_status="completed",
        scratch_root=tmp_path / "scratch",
    )

    assert run_git(repo, "rev-parse", "HEAD") == base_sha
    assert run_git(repo, "rev-parse", checkpoint.ref_name) == checkpoint.commit_sha
    assert run_git(repo, "rev-parse", f"{checkpoint.commit_sha}^{{tree}}") == checkpoint.tree_sha
    assert run_git(repo, "show", f"{checkpoint.commit_sha}:tracked.txt") == "staged and unstaged"
    assert (
        run_git(repo, "show", f"{checkpoint.commit_sha}:untracked.bin")
        .encode("utf-8", errors="replace")
        .startswith(b"\x00checkpoint")
    )
    assert "deleted.txt" not in run_git(repo, "ls-tree", "-r", "--name-only", checkpoint.commit_sha)
    assert "ignored.txt" not in run_git(repo, "ls-tree", "-r", "--name-only", checkpoint.commit_sha)

    clean_checkpointed_workspace(repo, checkpoint, scratch_root=tmp_path / "scratch")

    assert run_git(repo, "rev-parse", "HEAD") == base_sha
    assert workspace_state(repo).porcelain == ""
    assert (repo / "ignored.txt").read_text(encoding="utf-8") == "keep me\n"
    assert run_git(repo, "rev-parse", checkpoint.ref_name) == checkpoint.commit_sha


def test_cleanup_fails_closed_when_workspace_changed_after_checkpoint(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("worker result\n", encoding="utf-8")
    checkpoint = create_slot_checkpoint(
        repo,
        job_id="job-1",
        task_id="task-1",
        terminal_status="cancelled",
        scratch_root=tmp_path / "scratch",
    )
    (repo / "late-change.txt").write_text("must survive\n", encoding="utf-8")

    with pytest.raises(SlotCheckpointError, match="changed after checkpoint"):
        clean_checkpointed_workspace(repo, checkpoint, scratch_root=tmp_path / "scratch")

    assert (repo / "late-change.txt").read_text(encoding="utf-8") == "must survive\n"
    assert workspace_state(repo).dirty


def test_existing_job_ref_with_different_tree_is_never_overwritten(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    first = create_slot_checkpoint(
        repo,
        job_id="job-1",
        task_id="task-1",
        terminal_status="completed",
        scratch_root=tmp_path / "scratch",
    )
    (repo / "tracked.txt").write_text("second\n", encoding="utf-8")

    with pytest.raises(SlotCheckpointError, match="already exists with a different tree"):
        create_slot_checkpoint(
            repo,
            job_id="job-1",
            task_id="task-1",
            terminal_status="completed",
            scratch_root=tmp_path / "scratch",
        )

    assert run_git(repo, "rev-parse", first.ref_name) == first.commit_sha


def _committed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _run(path, "init")
    _run(path, "checkout", "-b", "main")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _run(path, "add", ".")
    _run(
        path,
        "-c",
        "user.name=ACP Test",
        "-c",
        "user.email=acp-test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    return path


def _run(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
