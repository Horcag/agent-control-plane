from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from agent_control_plane.entities.workspace import find_forbidden_status_entries
from agent_control_plane.shared.config import SlotPrepareCommand
from agent_control_plane.shared.git_tools import GitError, workspace_state


class SlotPrepareError(RuntimeError):
    pass


def prepare_workspace_slot(
    *,
    slot_path: Path,
    commands: tuple[SlotPrepareCommand, ...],
    forbidden_status_globs: tuple[str, ...],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        result = _run_prepare_command(
            slot_path=slot_path,
            command=command,
            forbidden_status_globs=forbidden_status_globs,
        )
        results.append(result)
    return results


def _run_prepare_command(
    *,
    slot_path: Path,
    command: SlotPrepareCommand,
    forbidden_status_globs: tuple[str, ...],
) -> dict[str, Any]:
    working_dir = _slot_child(slot_path, command.working_dir)
    marker = _slot_child(slot_path, command.marker) if command.marker is not None else None
    if marker is not None and marker.exists():
        return {
            "name": command.name,
            "status": "skipped",
            "reason": f"marker exists: {marker}",
            "working_dir": str(working_dir),
            "command": list(command.command),
        }

    if not working_dir.exists() or not working_dir.is_dir():
        raise SlotPrepareError(f"Prepare working directory does not exist: {working_dir}")

    before_forbidden = _forbidden_status(slot_path, forbidden_status_globs)
    try:
        completed = subprocess.run(  # nosec B603
            list(command.command),
            cwd=working_dir,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=command.timeout_sec,
        )
    except FileNotFoundError as exc:
        raise SlotPrepareError(
            f"Prepare command is not available for {command.name}: {command.command[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SlotPrepareError(
            f"Prepare command timed out after {command.timeout_sec}s for {command.name}"
        ) from exc

    output_tail = _tail(completed.stdout or "", 40)
    if completed.returncode != 0:
        raise SlotPrepareError(
            f"Prepare command failed for {command.name} with exit code {completed.returncode}:\n"
            f"{output_tail}"
        )

    after_forbidden = _forbidden_status(slot_path, forbidden_status_globs)
    new_forbidden = sorted(after_forbidden - before_forbidden)
    if new_forbidden:
        raise SlotPrepareError(
            f"Prepare command created forbidden tracked changes: {'; '.join(new_forbidden)}"
        )

    return {
        "name": command.name,
        "status": "ran",
        "working_dir": str(working_dir),
        "command": list(command.command),
        "output_tail": output_tail,
    }


def _slot_child(slot_path: Path, path: Path) -> Path:
    if path.is_absolute():
        return path.resolve(strict=False)
    return (slot_path / path).resolve(strict=False)


def _forbidden_status(slot_path: Path, forbidden_status_globs: tuple[str, ...]) -> set[str]:
    try:
        state = workspace_state(slot_path)
    except GitError as exc:
        raise SlotPrepareError(f"Could not inspect git status after prepare: {exc}") from exc
    return {
        f"{entry.status} {entry.path} [{entry.matched_glob}]"
        for entry in find_forbidden_status_entries(state.porcelain, forbidden_status_globs)
    }


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])
