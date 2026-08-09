from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_control_plane.shared.process_liveness import process_is_alive
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database

DEFAULT_POLL_INTERVAL_SEC = 0.5


class NativeQualityGateSlotBroker:
    """Cross-process bound on concurrently running controller quality gate processes.

    `NativeQualityContract.max_parallel` only bounds gates *within* one job's
    battery (an in-process `ThreadPoolExecutor`). Finalization can happen in
    separate processes, so N jobs finalizing at the same time can still put
    `N * max_parallel` gate processes on the box. This reuses the SQLite
    cross-process lease pattern already established by `GlobalQuotaBroker`
    (dead-holder reclaim keyed on PID liveness) instead of an in-process
    semaphore, which would not see leases held by other processes.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        max_parallel: int,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_parallel <= 0:
            raise ValueError("max_parallel must be positive")
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        self.database_path = database_path
        self.max_parallel = max_parallel
        self.poll_interval_sec = poll_interval_sec
        self.clock = clock
        self._initialize()

    @contextmanager
    def acquire(self, *, holder_pid: int, timeout_sec: float) -> Iterator[bool]:
        """Hold one cross-process gate slot for the duration of the block.

        Yields `True` once a slot is held, or `False` if none freed up
        within `timeout_sec` of waiting. A caller that receives `False`
        never spawned a process and should report the gate as timed out
        rather than waiting indefinitely or bypassing the bound.
        """
        if holder_pid <= 0:
            raise ValueError("holder_pid must be positive")
        token = self._acquire_within(holder_pid, max(0.0, timeout_sec))
        try:
            yield token is not None
        finally:
            if token is not None:
                self._release(token)

    def _initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="native_quality_gate_slots",
            version=1,
            checksum="native-quality-gate-slots-v1-20260809",
            migrate=self._migrate_schema_v1,
        )

    @staticmethod
    def _migrate_schema_v1(db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists native_quality_gate_slots (
                slot_token text primary key,
                holder_pid integer not null,
                acquired_at real not null
            )
            """
        )

    def _acquire_within(self, holder_pid: int, timeout_sec: float) -> str | None:
        deadline = self.clock() + timeout_sec
        while True:
            token = self._try_acquire_once(holder_pid)
            if token is not None:
                return token
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            time.sleep(min(self.poll_interval_sec, remaining))

    def _try_acquire_once(self, holder_pid: int) -> str | None:
        with control_database(self.database_path) as db:
            db.execute("begin immediate")
            self._reclaim_dead_slots(db)
            held = db.execute("select count(*) as active from native_quality_gate_slots").fetchone()
            if int(held["active"]) >= self.max_parallel:
                return None
            token = uuid.uuid4().hex
            db.execute(
                """
                insert into native_quality_gate_slots (slot_token, holder_pid, acquired_at)
                values (?, ?, ?)
                """,
                (token, holder_pid, self.clock()),
            )
            return token

    def _release(self, token: str) -> None:
        with control_database(self.database_path) as db:
            db.execute(
                "delete from native_quality_gate_slots where slot_token = ?",
                (token,),
            )

    @staticmethod
    def _reclaim_dead_slots(db: sqlite3.Connection) -> None:
        rows = db.execute("select slot_token, holder_pid from native_quality_gate_slots").fetchall()
        dead = [
            str(row["slot_token"]) for row in rows if not process_is_alive(int(row["holder_pid"]))
        ]
        if dead:
            db.executemany(
                "delete from native_quality_gate_slots where slot_token = ?",
                ((token,) for token in dead),
            )
