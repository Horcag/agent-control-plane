from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database


@dataclass(frozen=True)
class SlotRecord:
    name: str
    route: str
    path: Path
    status: str
    active_job_id: str | None
    created_at: str
    updated_at: str
    last_used_at: str | None
    use_count: int
    note: str | None


class SlotStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="slot_store",
            version=1,
            checksum="slot-store-v1-20260715",
            migrate=self._migrate_schema,
        )

    @staticmethod
    def _migrate_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists slots (
                name text primary key,
                route text not null,
                path text not null,
                status text not null,
                active_job_id text,
                created_at text not null,
                updated_at text not null,
                last_used_at text,
                use_count integer not null default 0,
                note text
            )
            """
        )

    def register_slot(
        self,
        name: str,
        route: str,
        path: Path,
        *,
        note: str | None = None,
    ) -> SlotRecord:
        self.initialize()
        now = utc_now()
        existing = self.get_slot(name)
        with self._connect() as db:
            if existing is None:
                db.execute(
                    """
                    insert into slots (
                        name, route, path, status, active_job_id, created_at,
                        updated_at, last_used_at, use_count, note
                    )
                    values (?, ?, ?, ?, null, ?, ?, null, 0, ?)
                    """,
                    (name, route, str(path), "available", now, now, note),
                )
            else:
                db.execute(
                    """
                    update slots
                    set route = ?, path = ?, updated_at = ?
                    where name = ?
                    """,
                    (route, str(path), now, name),
                )
        return self.require_slot(name)

    def mark_available(self, name: str, *, note: str | None = None) -> SlotRecord:
        return self._update_inactive_status(name, "available", note)

    def mark_status(self, name: str, status: str, *, note: str | None = None) -> SlotRecord:
        normalized = status.strip()
        if not normalized:
            raise ValueError("Slot status must not be empty")
        if normalized in {"active", "finalizing"}:
            raise ValueError(f"Slot status {normalized!r} requires an explicit owner operation")
        return self._update_inactive_status(name, normalized, note)

    def mark_deleted(
        self,
        name: str,
        *,
        note: str | None = None,
        force: bool = False,
    ) -> SlotRecord:
        if not force:
            return self._update_inactive_status(name, "deleted", note)
        self.initialize()
        with self._connect() as db:
            cursor = db.execute(
                """
                update slots set status = 'deleted', active_job_id = null,
                    updated_at = ?, note = ? where name = ?
                """,
                (utc_now(), note, name),
            )
            if cursor.rowcount != 1:
                raise SlotStoreError(f"Slot is missing: {name}")
        return self.require_slot(name)

    def acquire_slot(self, name: str, job_id: str) -> SlotRecord:
        self.initialize()
        now = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                """
                update slots
                set status = 'active',
                    active_job_id = ?,
                    updated_at = ?,
                    last_used_at = ?,
                    use_count = use_count + 1,
                    note = null
                where name = ? and active_job_id is null and status = 'available'
                """,
                (job_id, now, now, name),
            )
            if cursor.rowcount != 1:
                raise SlotStoreError(f"Slot is already active or missing: {name}")
        return self.require_slot(name)

    def claim_for_finalization(self, name: str, job_id: str) -> SlotRecord:
        """Atomically exclude new owners before inspecting or cleaning a terminal slot."""
        self.initialize()
        now = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                """
                update slots
                set status = 'finalizing', active_job_id = ?, updated_at = ?,
                    note = 'terminal finalization in progress'
                where name = ? and status != 'deleted'
                    and (active_job_id is null or active_job_id = ?)
                """,
                (job_id, now, name, job_id),
            )
            if cursor.rowcount != 1:
                raise SlotStoreError(
                    f"Slot {name} is owned by another job; cannot finalize {job_id}"
                )
        return self.require_slot(name)

    def release_slot(
        self,
        name: str,
        job_id: str,
        *,
        status: str = "available",
        note: str | None = None,
    ) -> SlotRecord:
        self.initialize()
        now = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                """
                update slots
                set status = ?,
                    active_job_id = null,
                    updated_at = ?,
                    note = ?
                where name = ? and active_job_id = ?
                """,
                (status, now, note, name, job_id),
            )
            if cursor.rowcount != 1:
                raise SlotStoreError(f"Slot {name} is not active for job {job_id}")
        return self.require_slot(name)

    def get_slot(self, name: str) -> SlotRecord | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute("select * from slots where name = ?", (name,)).fetchone()
        return _slot_from_row(row) if row else None

    def require_slot(self, name: str) -> SlotRecord:
        record = self.get_slot(name)
        if record is None:
            raise SlotStoreError(f"Slot not found: {name}")
        return record

    def list_slots(self) -> list[SlotRecord]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute("select * from slots order by route, name").fetchall()
        return [_slot_from_row(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with control_database(self.database_path) as db:
            yield db

    def _update_inactive_status(
        self,
        name: str,
        status: str,
        note: str | None,
    ) -> SlotRecord:
        self.initialize()
        with self._connect() as db:
            cursor = db.execute(
                """
                update slots set status = ?, active_job_id = null, updated_at = ?, note = ?
                where name = ? and active_job_id is null
                """,
                (status, utc_now(), note, name),
            )
            if cursor.rowcount != 1:
                raise SlotStoreError(f"Slot is active or missing: {name}")
        return self.require_slot(name)


class SlotStoreError(RuntimeError):
    pass


def _slot_from_row(row: sqlite3.Row) -> SlotRecord:
    return SlotRecord(
        name=row["name"],
        route=row["route"],
        path=Path(row["path"]),
        status=row["status"],
        active_job_id=row["active_job_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
        use_count=row["use_count"],
        note=row["note"],
    )
