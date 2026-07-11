from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RateLimitSnapshot:
    used_percent: float
    resets_at: float
    observed_at: float


@dataclass(frozen=True)
class QuotaDecision:
    acquired: bool
    reason: str | None
    active_jobs: int
    retry_after_sec: float | None = None


class CodexRateLimitReader:
    """Read the newest five-hour Codex rate-limit snapshot from local rollouts."""

    def __init__(self, sessions_root: Path, *, max_files: int = 24) -> None:
        self.sessions_root = sessions_root
        self.max_files = max(1, max_files)

    def latest(self) -> RateLimitSnapshot | None:
        if not self.sessions_root.exists():
            return None
        candidates = sorted(
            self.sessions_root.rglob("*.jsonl"),
            key=_safe_mtime,
            reverse=True,
        )[: self.max_files]
        for path in candidates:
            snapshot = self._latest_in_file(path)
            if snapshot is not None:
                return snapshot
        return None

    @staticmethod
    def _latest_in_file(path: Path) -> RateLimitSnapshot | None:
        try:
            raw = _read_tail(path, max_bytes=1_048_576)
        except OSError:
            return None
        for line in reversed(raw.splitlines()):
            event = _json_object(line)
            if event is None:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits")
            if not isinstance(rate_limits, dict):
                rate_limits = event.get("rate_limits")
            if not isinstance(rate_limits, dict):
                continue
            primary = rate_limits.get("primary")
            if not isinstance(primary, dict):
                continue
            used_percent = _number(primary.get("used_percent"))
            resets_at = _number(primary.get("resets_at"))
            if used_percent is None or resets_at is None:
                continue
            observed_at = _timestamp(event.get("timestamp")) or _safe_mtime(path)
            return RateLimitSnapshot(
                used_percent=used_percent,
                resets_at=resets_at,
                observed_at=observed_at,
            )
        return None


class GlobalQuotaBroker:
    """Cross-process concurrency and five-hour quota broker backed by SQLite."""

    def __init__(
        self,
        database_path: Path,
        *,
        max_concurrent_jobs: int,
        soft_limit_percent: float = 100.0,
        rate_limit_reader: Callable[[], RateLimitSnapshot | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        if not 0 < soft_limit_percent <= 100:
            raise ValueError("soft_limit_percent must be in (0, 100]")
        self.database_path = database_path
        self.max_concurrent_jobs = max_concurrent_jobs
        self.soft_limit_percent = soft_limit_percent
        self.rate_limit_reader = rate_limit_reader
        self.clock = clock
        self._initialize()

    def try_acquire(self, job_id: str, *, worker_pid: int) -> QuotaDecision:
        if worker_pid <= 0:
            raise ValueError("worker_pid must be positive")
        now = self.clock()
        snapshot = self.rate_limit_reader() if self.rate_limit_reader is not None else None

        with self._connect() as db:
            db.execute("begin immediate")
            self._reclaim_dead_leases(db)
            active_jobs = int(db.execute("select count(*) from leases").fetchone()[0])

            if (
                snapshot is not None
                and snapshot.used_percent >= self.soft_limit_percent
                and snapshot.resets_at > now
            ):
                return QuotaDecision(
                    acquired=False,
                    reason="rate_limit_soft_cap",
                    active_jobs=active_jobs,
                    retry_after_sec=max(0.0, snapshot.resets_at - now),
                )

            existing = db.execute(
                "select worker_pid from leases where job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                db.execute(
                    "update leases set worker_pid = ?, heartbeat_at = ? where job_id = ?",
                    (worker_pid, now, job_id),
                )
                return QuotaDecision(acquired=True, reason=None, active_jobs=active_jobs)

            if active_jobs >= self.max_concurrent_jobs:
                return QuotaDecision(
                    acquired=False,
                    reason="concurrency_limit",
                    active_jobs=active_jobs,
                )

            db.execute(
                """
                insert into leases (job_id, worker_pid, acquired_at, heartbeat_at)
                values (?, ?, ?, ?)
                """,
                (job_id, worker_pid, now, now),
            )
            return QuotaDecision(
                acquired=True,
                reason=None,
                active_jobs=active_jobs + 1,
            )

    def release(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute("delete from leases where job_id = ?", (job_id,))

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                create table if not exists leases (
                    job_id text primary key,
                    worker_pid integer not null,
                    acquired_at real not null,
                    heartbeat_at real not null
                )
                """
            )

    @staticmethod
    def _reclaim_dead_leases(db: sqlite3.Connection) -> None:
        rows = db.execute("select job_id, worker_pid from leases").fetchall()
        dead = [str(row["job_id"]) for row in rows if not _pid_alive(int(row["worker_pid"]))]
        if dead:
            db.executemany("delete from leases where job_id = ?", ((job_id,) for job_id in dead))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("pragma busy_timeout = 30000")
        try:
            yield db
            db.commit()
        finally:
            db.close()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return int(kernel32.GetLastError()) == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_tail(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        return None


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
