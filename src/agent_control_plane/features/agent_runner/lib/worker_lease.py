from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, cast

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WorkerLeaseError(RuntimeError):
    pass


class WorkerLeaseState(StrEnum):
    MISSING = "missing"
    HELD_MATCH = "held_match"
    HELD_MISMATCH = "held_mismatch"
    RELEASED_MATCH = "released_match"
    RELEASED_MISMATCH = "released_mismatch"


@dataclass(frozen=True)
class WorkerLeaseProbe:
    state: WorkerLeaseState
    observed_instance_id: str | None


class WorkerLease:
    """Process-owned lease whose OS lock survives PID reuse safely."""

    def __init__(self, run_dir: Path, worker_instance_id: str) -> None:
        normalized = _worker_instance_id(worker_instance_id)
        self.path = run_dir / "worker.lease"
        self.identity_path = run_dir / "worker.instance"
        self.worker_instance_id = normalized
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise WorkerLeaseError("Worker lease is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            _ensure_lock_byte(handle)
            if not _try_lock(handle):
                raise WorkerLeaseError(f"Worker lease is already held: {self.path}")
            self.identity_path.write_text(self.worker_instance_id + "\n", encoding="utf-8")
        except Exception:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> WorkerLease:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class FinalizationLease(WorkerLease):
    """Single-writer process lease for replayable terminal finalization."""

    def __init__(self, run_dir: Path, finalizer_instance_id: str) -> None:
        super().__init__(run_dir, finalizer_instance_id)
        self.path = run_dir / "finalizer.lease"
        self.identity_path = run_dir / "finalizer.instance"


def probe_worker_lease(run_dir: Path, worker_instance_id: str) -> WorkerLeaseProbe:
    expected = _worker_instance_id(worker_instance_id)
    path = run_dir / "worker.lease"
    identity_path = run_dir / "worker.instance"
    try:
        handle = path.open("r+b")
    except FileNotFoundError:
        return WorkerLeaseProbe(WorkerLeaseState.MISSING, None)
    except OSError as exc:
        raise WorkerLeaseError(f"Could not inspect worker lease {path}: {exc}") from exc

    with handle:
        held = not _try_lock(handle)
        if not held:
            _unlock(handle)
    observed = _read_instance_id(identity_path)

    matches = observed == expected
    if held and matches:
        state = WorkerLeaseState.HELD_MATCH
    elif held:
        state = WorkerLeaseState.HELD_MISMATCH
    elif matches:
        state = WorkerLeaseState.RELEASED_MATCH
    else:
        state = WorkerLeaseState.RELEASED_MISMATCH
    return WorkerLeaseProbe(state, observed)


def _worker_instance_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("worker_instance_id must be one non-empty line")
    return normalized


def _ensure_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() > 0:
        return
    handle.write(b"\0")
    handle.flush()


def _read_instance_id(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkerLeaseError(f"Could not read worker identity {path}: {exc}") from exc
    return value or None


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            posix_fcntl = cast(Any, fcntl)
            posix_fcntl.flock(
                handle.fileno(),
                posix_fcntl.LOCK_EX | posix_fcntl.LOCK_NB,
            )
    except OSError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    posix_fcntl = cast(Any, fcntl)
    posix_fcntl.flock(handle.fileno(), posix_fcntl.LOCK_UN)
