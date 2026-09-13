from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class CliTokenStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class _TargetSnapshot:
    path: Path
    existed: bool
    data: bytes | None
    mode: int | None


def write_cli_tokens(paths: tuple[Path, ...], payload: str) -> tuple[str, ...]:
    """Atomically write one AGY CLI credential payload to all explicit targets."""
    targets = _preflight_targets(paths)
    payload_bytes = _validated_payload_bytes(payload)
    snapshots = [_snapshot_target(path) for path in targets]
    changed: list[_TargetSnapshot] = []
    temp_paths: list[Path] = []

    try:
        for snapshot in snapshots:
            temp_path = _write_tempfile(snapshot.path.parent, payload_bytes)
            temp_paths.append(temp_path)
            _require_snapshot_unchanged(snapshot)
            os.replace(temp_path, snapshot.path)
            temp_paths.remove(temp_path)
            _fsync_directory(snapshot.path.parent)
            changed.append(snapshot)

        mismatches = [
            snapshot.path for snapshot in snapshots if snapshot.path.read_bytes() != payload_bytes
        ]
        if mismatches:
            joined = ", ".join(str(path) for path in mismatches)
            raise CliTokenStoreError(f"Credential write verification failed for: {joined}")
    except BaseException as exc:
        rollback_messages = _rollback_changed_targets(changed, payload_bytes)
        for temp_path in temp_paths:
            with suppress(OSError):
                temp_path.unlink()
        if isinstance(exc, CliTokenStoreError):
            base_message = str(exc)
        else:
            base_message = f"Credential write failed: {exc.__class__.__name__}"
        if rollback_messages:
            base_message = f"{base_message}; rollback: {'; '.join(rollback_messages)}"
        raise CliTokenStoreError(base_message) from exc

    return tuple(str(path) for path in targets)


@contextmanager
def manager_switch_lock(database_path: Path, timeout_sec: float = 10) -> Iterator[None]:
    """Serialize ACP credential switches with one atomic same-root lock directory."""
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")
    lock_dir = database_path.with_name("acp-cli-switch.lock")
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    metadata = {
        "nonce": nonce,
        "created_at_monotonic": time.monotonic(),
        "owner": "agent-control-plane",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "created_at": time.time(),
        "note": "Inspect this directory before manual removal; stale locks are not reclaimed automatically.",
    }
    deadline = time.monotonic() + timeout_sec
    acquired = False
    try:
        try:
            while True:
                try:
                    lock_dir.mkdir(mode=0o700)
                    acquired = True
                    _write_lock_metadata(lock_dir, metadata)
                    break
                except FileExistsError as exc:
                    if time.monotonic() >= deadline:
                        raise CliTokenStoreError(
                            "Could not acquire AGY CLI switch lock before timeout; "
                            f"inspect possible stale lock directory before removal: {lock_dir}"
                        ) from exc
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            yield
        finally:
            if acquired:
                _remove_lock_dir_if_owned(lock_dir, nonce)
    except CliTokenStoreError:
        raise
    except OSError as exc:
        raise CliTokenStoreError("AGY CLI switch lock failed") from exc


def _write_lock_metadata(lock_dir: Path, metadata: dict[str, Any]) -> None:
    path = lock_dir / "owner.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            path.unlink()
        raise


def _remove_lock_dir_if_owned(lock_dir: Path, nonce: str) -> None:
    metadata_path = lock_dir / "owner.json"
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliTokenStoreError(
            f"AGY CLI switch lock cleanup uncertain; inspect lock directory: {lock_dir}"
        ) from exc
    if data.get("nonce") != nonce:
        raise CliTokenStoreError(
            f"AGY CLI switch lock ownership changed; inspect lock directory: {lock_dir}"
        )
    metadata_path.unlink()
    try:
        lock_dir.rmdir()
    except OSError as exc:
        raise CliTokenStoreError(
            f"AGY CLI switch lock cleanup uncertain; inspect lock directory: {lock_dir}"
        ) from exc
    finally:
        _fsync_directory(lock_dir.parent)


def _validated_payload_bytes(payload: str) -> bytes:
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CliTokenStoreError("Credential payload is not valid JSON") from exc

    if not isinstance(data, dict):
        raise CliTokenStoreError("Credential payload must be a JSON object")
    token = data.get("token")
    if not isinstance(token, dict):
        raise CliTokenStoreError("Credential payload must contain token object")
    _require_non_empty_string(token, "access_token")
    _require_non_empty_string(token, "refresh_token")
    _require_valid_expiry(token)
    if data.get("auth_method") != "consumer":
        raise CliTokenStoreError("Credential payload auth_method must be consumer")
    return payload.encode("utf-8")


def _require_non_empty_string(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CliTokenStoreError(f"Credential token field is required: {key}")


def _require_valid_expiry(data: dict[str, Any]) -> None:
    expiry = data.get("expiry")
    if not isinstance(expiry, str) or not expiry.strip():
        raise CliTokenStoreError("Credential token field is required: expiry")
    try:
        datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliTokenStoreError("Credential token field is invalid: expiry") from exc


def _preflight_targets(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    if not paths:
        raise CliTokenStoreError("At least one credential target path is required")

    targets = tuple(Path(path) for path in paths)
    seen: set[Path] = set()
    for path in targets:
        if path in seen:
            raise CliTokenStoreError(f"Duplicate credential target path: {path}")
        seen.add(path)
        parent = path.parent
        if parent.is_symlink():
            raise CliTokenStoreError(f"Credential target parent is a symlink: {parent}")
        if not parent.exists() or not parent.is_dir():
            raise CliTokenStoreError(
                f"Credential target parent is missing or not a directory: {parent}"
            )
        if path.is_symlink():
            raise CliTokenStoreError(f"Credential target is a symlink: {path}")
        if path.exists() and not path.is_file():
            raise CliTokenStoreError(f"Credential target is not a regular file: {path}")
    return targets


def _snapshot_target(path: Path) -> _TargetSnapshot:
    if not path.exists():
        return _TargetSnapshot(path=path, existed=False, data=None, mode=None)
    stat_result = path.stat()
    return _TargetSnapshot(
        path=path,
        existed=True,
        data=path.read_bytes(),
        mode=stat_result.st_mode,
    )


def _require_snapshot_unchanged(snapshot: _TargetSnapshot) -> None:
    if snapshot.path.is_symlink():
        raise CliTokenStoreError(f"Credential target changed before replace: {snapshot.path}")
    if not snapshot.existed:
        if snapshot.path.exists():
            raise CliTokenStoreError(f"Credential target appeared before replace: {snapshot.path}")
        return
    if not snapshot.path.exists():
        raise CliTokenStoreError(f"Credential target disappeared before replace: {snapshot.path}")
    if snapshot.path.read_bytes() != snapshot.data:
        raise CliTokenStoreError(f"Credential target changed before replace: {snapshot.path}")


def _write_tempfile(parent: Path, payload_bytes: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".acp-cli-token-", suffix=".tmp", dir=parent)
    temp_path = Path(name)
    try:
        if os.name == "posix":
            os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            temp_path.unlink()
        raise
    return temp_path


def _rollback_changed_targets(changed: list[_TargetSnapshot], own_bytes: bytes) -> list[str]:
    messages: list[str] = []
    for snapshot in reversed(changed):
        try:
            if not snapshot.path.exists() or snapshot.path.read_bytes() != own_bytes:
                messages.append(f"uncertain for {snapshot.path}: current bytes changed")
                continue
            if snapshot.existed:
                if snapshot.data is None:
                    messages.append(f"uncertain for {snapshot.path}: missing rollback snapshot")
                    continue
                temp_path: Path | None = None
                try:
                    temp_path = _write_tempfile(snapshot.path.parent, snapshot.data)
                    os.replace(temp_path, snapshot.path)
                finally:
                    if temp_path is not None and temp_path.exists():
                        with suppress(OSError):
                            temp_path.unlink()
                if snapshot.mode is not None:
                    with suppress(OSError):
                        snapshot.path.chmod(snapshot.mode)
            else:
                snapshot.path.unlink()
            _fsync_directory(snapshot.path.parent)
        except BaseException as exc:  # noqa: BLE001
            messages.append(f"uncertain for {snapshot.path}: {exc.__class__.__name__}")
    return messages


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        return
    finally:
        if fd is not None:
            os.close(fd)
