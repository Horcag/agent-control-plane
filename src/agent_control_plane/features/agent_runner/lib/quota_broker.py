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

from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database

FULL_CODEX_JOB_CAPACITY_UNITS = 30
_MODEL_CAPACITY_WEIGHTS = {
    "luna": 2,
    "terra": 5,
    "sol": 10,
}
_REASONING_CAPACITY_WEIGHTS = {
    "none": 1,
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 3,
}


def codex_job_capacity_units(model: str, reasoning_effort: str) -> int:
    """Return weighted concurrency units; unknown models consume one full slot."""

    family = model.strip().lower().rsplit("-", 1)[-1]
    model_weight = _MODEL_CAPACITY_WEIGHTS.get(family)
    if model_weight is None:
        return FULL_CODEX_JOB_CAPACITY_UNITS
    effort_weight = _REASONING_CAPACITY_WEIGHTS.get(reasoning_effort.strip().lower(), 3)
    return min(FULL_CODEX_JOB_CAPACITY_UNITS, model_weight * effort_weight)


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
    active_capacity_units: int = 0
    max_capacity_units: int = 0


class CodexRateLimitReader:
    """Read the newest primary Codex rate-limit snapshot from local rollouts."""

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
    """Cross-process concurrency and primary-window quota broker backed by SQLite."""

    def __init__(
        self,
        database_path: Path,
        *,
        max_concurrent_jobs: int,
        max_burst_jobs: int | None = None,
        soft_limit_percent: float = 100.0,
        rate_limit_reader: Callable[[], RateLimitSnapshot | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        effective_burst_jobs = (
            max_burst_jobs if max_burst_jobs is not None else max_concurrent_jobs * 4
        )
        if effective_burst_jobs <= 0:
            raise ValueError("max_burst_jobs must be positive")
        if effective_burst_jobs < max_concurrent_jobs:
            raise ValueError("max_burst_jobs must be at least max_concurrent_jobs")
        if not 0 < soft_limit_percent <= 100:
            raise ValueError("soft_limit_percent must be in (0, 100]")
        self.database_path = database_path
        self.max_concurrent_jobs = max_concurrent_jobs
        self.max_burst_jobs = effective_burst_jobs
        self.max_capacity_units = max_concurrent_jobs * FULL_CODEX_JOB_CAPACITY_UNITS
        self.soft_limit_percent = soft_limit_percent
        self.rate_limit_reader = rate_limit_reader
        self.clock = clock
        self._initialize()

    def try_acquire(
        self,
        job_id: str,
        *,
        worker_pid: int,
        capacity_units: int = FULL_CODEX_JOB_CAPACITY_UNITS,
    ) -> QuotaDecision:
        if worker_pid <= 0:
            raise ValueError("worker_pid must be positive")
        if not 0 < capacity_units <= FULL_CODEX_JOB_CAPACITY_UNITS:
            raise ValueError(f"capacity_units must be in [1, {FULL_CODEX_JOB_CAPACITY_UNITS}]")
        now = self.clock()
        snapshot = self.rate_limit_reader() if self.rate_limit_reader is not None else None

        with self._connect() as db:
            db.execute("begin immediate")
            self._reclaim_dead_leases(db)
            totals = db.execute(
                """
                select count(*)                         as active_jobs,
                       coalesce(sum(capacity_units), 0) as active_capacity_units
                from leases
                """
            ).fetchone()
            active_jobs = int(totals["active_jobs"])
            active_capacity_units = int(totals["active_capacity_units"])

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
                    active_capacity_units=active_capacity_units,
                    max_capacity_units=self.max_capacity_units,
                )

            existing = db.execute(
                "select worker_pid, capacity_units from leases where job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                resized_capacity_units = (
                    active_capacity_units - int(existing["capacity_units"]) + capacity_units
                )
                if resized_capacity_units > self.max_capacity_units:
                    return QuotaDecision(
                        acquired=False,
                        reason="weighted_capacity_limit",
                        active_jobs=active_jobs,
                        active_capacity_units=active_capacity_units,
                        max_capacity_units=self.max_capacity_units,
                    )
                db.execute(
                    """
                    update leases
                    set worker_pid     = ?,
                        heartbeat_at   = ?,
                        capacity_units = ?
                    where job_id = ?
                    """,
                    (worker_pid, now, capacity_units, job_id),
                )
                return QuotaDecision(
                    acquired=True,
                    reason=None,
                    active_jobs=active_jobs,
                    active_capacity_units=resized_capacity_units,
                    max_capacity_units=self.max_capacity_units,
                )

            if active_jobs >= self.max_burst_jobs:
                return QuotaDecision(
                    acquired=False,
                    reason="burst_job_limit",
                    active_jobs=active_jobs,
                    active_capacity_units=active_capacity_units,
                    max_capacity_units=self.max_capacity_units,
                )
            if active_capacity_units + capacity_units > self.max_capacity_units:
                return QuotaDecision(
                    acquired=False,
                    reason="weighted_capacity_limit",
                    active_jobs=active_jobs,
                    active_capacity_units=active_capacity_units,
                    max_capacity_units=self.max_capacity_units,
                )

            db.execute(
                """
                insert into leases (job_id,
                                    worker_pid,
                                    acquired_at,
                                    heartbeat_at,
                                    capacity_units)
                values (?, ?, ?, ?, ?)
                """,
                (job_id, worker_pid, now, now, capacity_units),
            )
            return QuotaDecision(
                acquired=True,
                reason=None,
                active_jobs=active_jobs + 1,
                active_capacity_units=active_capacity_units + capacity_units,
                max_capacity_units=self.max_capacity_units,
            )

    def release(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute("delete from leases where job_id = ?", (job_id,))

    def _initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="global_quota_broker",
            version=1,
            checksum="global-quota-broker-v1-20260715",
            migrate=self._migrate_schema,
        )

    @staticmethod
    def _migrate_schema(db: sqlite3.Connection) -> None:
        db.execute(
            f"""
            create table if not exists leases (
                job_id text primary key,
                worker_pid integer not null,
                acquired_at real not null,
                heartbeat_at real not null,
                capacity_units integer not null default {FULL_CODEX_JOB_CAPACITY_UNITS}
            )
            """
        )
        columns = {str(row["name"]) for row in db.execute("pragma table_info(leases)")}
        if "capacity_units" not in columns:
            db.execute(
                "alter table leases add column capacity_units "
                f"integer not null default {FULL_CODEX_JOB_CAPACITY_UNITS}"
            )

    @staticmethod
    def _reclaim_dead_leases(db: sqlite3.Connection) -> None:
        rows = db.execute("select job_id, worker_pid from leases").fetchall()
        dead = [str(row["job_id"]) for row in rows if not _pid_alive(int(row["worker_pid"]))]
        if dead:
            db.executemany("delete from leases where job_id = ?", ((job_id,) for job_id in dead))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with control_database(self.database_path) as db:
            yield db


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
