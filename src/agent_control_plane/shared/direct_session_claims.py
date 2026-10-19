"""Conservative evidence for the existing operator-owned direct-session claims.

Claims establish a hold, never authority to bypass an ACP job or mutate a workspace.
All supported claim writers and slot mutations share ``direct_session_lock``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from agent_control_plane.shared.config import interprocess_config_lock


def direct_session_claim_path(coordination_root: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError(f"Unsafe slot name for direct-session claim: {name!r}")
    return coordination_root / "direct-session-claims" / f"{name}.json"


def direct_session_lock(coordination_root: Path, name: str) -> AbstractContextManager[None]:
    return interprocess_config_lock(direct_session_claim_path(coordination_root, name))


def read_direct_session_claim_bytes(claim_path: Path) -> bytes:
    """Bound reads and reject special files without blocking on FIFOs or devices."""
    if claim_path.parent.is_symlink():
        raise ValueError("claim directory is a symlink")
    before = claim_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("claim must be a regular file, not a symlink or special file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(claim_path, flags)
    with os.fdopen(descriptor, "rb") as claim_file:
        opened = os.fstat(claim_file.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ValueError("claim file identity changed while opening")
        raw = claim_file.read(65537)
    if len(raw) > 65536:
        raise ValueError("claim exceeds 64 KiB")
    return raw


def read_direct_session_claim(
    coordination_root: Path, name: str, route: str, path: Path
) -> dict[str, Any] | None:
    """Return bounded validated owner evidence, or an error hold; never expire it."""
    claim_path = direct_session_claim_path(coordination_root, name)
    evidence: dict[str, Any] = {"claim_path": str(claim_path)}
    try:
        if claim_path.parent.is_symlink() or claim_path.is_symlink():
            raise ValueError("claim path is a symlink")
        raw = read_direct_session_claim_bytes(claim_path)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("claim must be an object")
        if value.get("schemaVersion", 1) != 1:
            raise ValueError("unsupported claim schemaVersion")
        chat = value.get("chatId", value.get("chat"))
        if (
            value.get("chatId") is not None
            and value.get("chat") is not None
            and value["chatId"] != value["chat"]
        ):
            raise ValueError("claim chat and chatId disagree")
        value["chatId"] = chat
        for field in ("chatId", "task", "branch"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise ValueError(f"claim {field} must be a nonempty string")
        if value.get("slot") != name or value.get("route") != route:
            raise ValueError("claim slot or route does not match configured slot")
        claim_workspace = value.get("path")
        if not isinstance(claim_workspace, str) or not Path(claim_workspace).is_absolute():
            raise ValueError("claim path must be absolute")
        if Path(claim_workspace).resolve(strict=False) != path.resolve(strict=False):
            raise ValueError("claim path does not match configured slot")
        evidence.update(
            {key: value[key] for key in ("slot", "route", "path", "chatId", "task", "branch")}
        )
        evidence["createdAt"] = value.get("createdAt")
    except FileNotFoundError:
        # A missing claim directory/file means no hold; a dangling directory symlink does not.
        return None
    except (OSError, ValueError, TypeError) as exc:
        evidence["error"] = str(exc)
    return evidence
