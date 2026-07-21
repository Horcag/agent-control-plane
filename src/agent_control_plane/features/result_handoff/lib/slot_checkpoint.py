from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.shared.git_tools import (
    GIT_TIMEOUT_SEC,
    GitError,
    head_commit,
    workspace_state,
)
from agent_control_plane.shared.path_rules import (
    is_known_temporary_patch_artifact,
    is_same_or_child,
)


class SlotCheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlotCheckpoint:
    job_id: str
    task_id: str
    terminal_status: str
    workspace_path: Path
    ref_name: str
    commit_sha: str
    tree_sha: str
    base_sha: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "terminal_status": self.terminal_status,
            "workspace_path": str(self.workspace_path),
            "ref_name": self.ref_name,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "base_sha": self.base_sha,
        }


def create_slot_checkpoint(
    workspace_path: Path,
    *,
    job_id: str,
    task_id: str,
    terminal_status: str,
    scratch_root: Path,
) -> SlotCheckpoint:
    workspace = workspace_path.resolve(strict=False)
    scratch = _prepare_scratch_root(workspace, scratch_root)
    base_sha = head_commit(workspace)
    if base_sha is None:
        raise SlotCheckpointError("Cannot checkpoint a workspace without a HEAD commit")
    _assert_no_uncheckpointable_submodule_changes(workspace)
    tree_sha = _snapshot_tree(workspace, scratch)
    ref_name = _checkpoint_ref(job_id)
    existing_sha = _existing_ref(workspace, ref_name)
    if existing_sha is not None:
        existing_tree = _git(workspace, "rev-parse", f"{existing_sha}^{{tree}}")
        parents = _git(workspace, "rev-list", "--parents", "-n", "1", existing_sha).split()
        existing_base = parents[1] if len(parents) > 1 else None
        if existing_tree != tree_sha or existing_base != base_sha:
            raise SlotCheckpointError(
                f"Checkpoint ref {ref_name} already exists with a different tree or base"
            )
        return SlotCheckpoint(
            job_id=job_id,
            task_id=task_id,
            terminal_status=terminal_status,
            workspace_path=workspace,
            ref_name=ref_name,
            commit_sha=existing_sha,
            tree_sha=tree_sha,
            base_sha=base_sha,
        )

    message = (
        "agent-control-plane terminal checkpoint\n\n"
        f"Job: {_single_line(job_id)}\n"
        f"Task: {_single_line(task_id)}\n"
        f"Terminal status: {_single_line(terminal_status)}\n"
        "Review-required: true\n"
    )
    commit_sha = _git(
        workspace,
        "commit-tree",
        tree_sha,
        "-p",
        base_sha,
        input_text=message,
        extra_env={
            "GIT_AUTHOR_NAME": "Agent Control Plane",
            "GIT_AUTHOR_EMAIL": "checkpoint@agent-control-plane.invalid",
            "GIT_COMMITTER_NAME": "Agent Control Plane",
            "GIT_COMMITTER_EMAIL": "checkpoint@agent-control-plane.invalid",
        },
    )
    try:
        _git(workspace, "update-ref", ref_name, commit_sha, "0" * 40)
    except SlotCheckpointError as exc:
        raced_sha = _existing_ref(workspace, ref_name)
        if raced_sha is None:
            raise
        raced_tree = _git(workspace, "rev-parse", f"{raced_sha}^{{tree}}")
        raced_parents = _git(workspace, "rev-list", "--parents", "-n", "1", raced_sha).split()
        raced_base = raced_parents[1] if len(raced_parents) > 1 else None
        if raced_tree != tree_sha or raced_base != base_sha:
            raise SlotCheckpointError(
                f"Checkpoint ref {ref_name} was concurrently created with different content"
            ) from exc
        commit_sha = raced_sha
    verified_sha = _git(workspace, "rev-parse", "--verify", ref_name)
    verified_tree = _git(workspace, "rev-parse", f"{verified_sha}^{{tree}}")
    if verified_sha != commit_sha or verified_tree != tree_sha:
        raise SlotCheckpointError(f"Git ref verification failed for {ref_name}")
    return SlotCheckpoint(
        job_id=job_id,
        task_id=task_id,
        terminal_status=terminal_status,
        workspace_path=workspace,
        ref_name=ref_name,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        base_sha=base_sha,
    )


def clean_checkpointed_workspace(
    workspace_path: Path,
    checkpoint: SlotCheckpoint,
    *,
    scratch_root: Path,
) -> None:
    workspace = workspace_path.resolve(strict=False)
    if workspace != checkpoint.workspace_path.resolve(strict=False):
        raise SlotCheckpointError("Checkpoint belongs to a different workspace")
    scratch = _prepare_scratch_root(workspace, scratch_root)
    verify_slot_checkpoint(workspace, checkpoint)
    _assert_no_uncheckpointable_submodule_changes(workspace)
    current_tree = _snapshot_tree(workspace, scratch)
    if current_tree != checkpoint.tree_sha:
        raise SlotCheckpointError("Workspace changed after checkpoint; refusing cleanup")

    untracked = tuple(
        path
        for path in _git(
            workspace,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split("\0")
        if path
    )
    _git(workspace, "reset", "--hard", "HEAD")
    for path in untracked:
        _git(workspace, "clean", "-fd", "--", path)
    try:
        state = workspace_state(workspace)
    except GitError as exc:
        raise SlotCheckpointError(f"Could not verify cleaned workspace: {exc}") from exc
    if state.porcelain:
        raise SlotCheckpointError(
            "Workspace cleanup did not produce a clean slot; checkpoint ref was preserved"
        )


def verify_slot_checkpoint(workspace_path: Path, checkpoint: SlotCheckpoint) -> None:
    workspace = workspace_path.resolve(strict=False)
    if workspace != checkpoint.workspace_path.resolve(strict=False):
        raise SlotCheckpointError("Checkpoint belongs to a different workspace")
    current_head = head_commit(workspace)
    if current_head != checkpoint.base_sha:
        raise SlotCheckpointError("Workspace HEAD changed after checkpoint")
    verified_sha = _git(workspace, "rev-parse", "--verify", checkpoint.ref_name)
    verified_tree = _git(workspace, "rev-parse", f"{verified_sha}^{{tree}}")
    if verified_sha != checkpoint.commit_sha or verified_tree != checkpoint.tree_sha:
        raise SlotCheckpointError("Checkpoint ref no longer matches the verified commit and tree")


def checkpoint_changed_files(
    workspace_path: Path,
    checkpoint: SlotCheckpoint,
) -> list[dict[str, str]]:
    workspace = workspace_path.resolve(strict=False)
    if workspace != checkpoint.workspace_path.resolve(strict=False):
        raise SlotCheckpointError("Checkpoint belongs to a different workspace")
    output = _git(
        workspace,
        "diff",
        "--name-status",
        "-z",
        checkpoint.base_sha,
        checkpoint.commit_sha,
        "--",
    )
    values = [value for value in output.split("\0") if value]
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(values):
        status = values[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(values):
                raise SlotCheckpointError("Malformed rename/copy entry in checkpoint diff")
            previous_path = values[index]
            path = values[index + 1]
            index += 2
            changes.append(
                {
                    "path": path.replace("\\", "/"),
                    "previous_path": previous_path.replace("\\", "/"),
                    "status": status,
                }
            )
            continue
        if index >= len(values):
            raise SlotCheckpointError("Malformed path entry in checkpoint diff")
        path = values[index]
        index += 1
        changes.append({"path": path.replace("\\", "/"), "status": status})
    return changes


def checkpoint_temporary_patch_artifacts(
    workspace_path: Path,
    checkpoint: SlotCheckpoint,
) -> tuple[str, ...]:
    """Return newly introduced proven temporary patch artifacts in a checkpoint."""
    return tuple(
        change["path"]
        for change in checkpoint_changed_files(workspace_path, checkpoint)
        if change["status"].startswith(("A", "C", "R"))
        and is_known_temporary_patch_artifact(change["path"])
    )


def _snapshot_tree(workspace: Path, scratch_root: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="checkpoint-index-", dir=scratch_root) as temp:
        index_path = Path(temp) / "index"
        extra_env = {"GIT_INDEX_FILE": str(index_path)}
        _git(workspace, "read-tree", "HEAD", extra_env=extra_env)
        _git(workspace, "add", "-A", "--", ".", extra_env=extra_env)
        return _git(workspace, "write-tree", extra_env=extra_env)


def _prepare_scratch_root(workspace: Path, scratch_root: Path) -> Path:
    scratch = scratch_root.resolve(strict=False)
    if is_same_or_child(scratch, workspace):
        raise SlotCheckpointError("Checkpoint scratch_root must be outside the task workspace")
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _checkpoint_ref(job_id: str) -> str:
    digest = hashlib.sha256(job_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"refs/agent-control-plane/jobs/{digest}"


def _existing_ref(workspace: Path, ref_name: str) -> str | None:
    try:
        return _git(workspace, "show-ref", "--verify", "--hash", ref_name)
    except SlotCheckpointError:
        return None


def _assert_no_uncheckpointable_submodule_changes(workspace: Path) -> None:
    try:
        porcelain = workspace_state(workspace).porcelain
    except GitError as exc:
        raise SlotCheckpointError(f"Could not inspect workspace: {exc}") from exc
    for line in porcelain.splitlines():
        code = line[:2]
        if "m" in code or ("?" in code and code != "??"):
            raise SlotCheckpointError(
                "Dirty content inside a Git submodule cannot be represented by the controller ref"
            )


def _git(
    workspace: Path,
    *args: str,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    command = ["git", "-C", str(workspace), *args]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    run_kwargs: dict[str, Any] = {
        "input": input_text,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "check": False,
        "timeout": GIT_TIMEOUT_SEC,
        "env": env,
    }
    if input_text is None:
        run_kwargs["stdin"] = subprocess.DEVNULL
    try:
        proc = subprocess.run(  # nosec B603 B607
            command,
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        rendered_command = subprocess.list2cmdline(command)
        raise SlotCheckpointError(
            f"Git command timed out after {GIT_TIMEOUT_SEC}s in {workspace}: {rendered_command}"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"git exited {proc.returncode}"
        raise SlotCheckpointError(detail)
    return proc.stdout.strip()


def _single_line(value: str) -> str:
    return " ".join(value.splitlines()).strip()
