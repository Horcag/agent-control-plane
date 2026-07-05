from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.clock import utc_now


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
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
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
                );
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
                    set route = ?, path = ?, updated_at = ?, note = coalesce(?, note)
                    where name = ?
                    """,
                    (route, str(path), now, note, name),
                )
        return self.require_slot(name)

    def mark_available(self, name: str, *, note: str | None = None) -> SlotRecord:
        return self._update_status(name, "available", None, note)

    def mark_deleted(self, name: str, *, note: str | None = None) -> SlotRecord:
        return self._update_status(name, "deleted", None, note)

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
                where name = ? and active_job_id is null
                """,
                (job_id, now, now, name),
            )
            if cursor.rowcount != 1:
                raise SlotStoreError(f"Slot is already active or missing: {name}")
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
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _update_status(
        self,
        name: str,
        status: str,
        active_job_id: str | None,
        note: str | None,
    ) -> SlotRecord:
        self.initialize()
        with self._connect() as db:
            db.execute(
                """
                update slots
                set status = ?, active_job_id = ?, updated_at = ?, note = ?
                where name = ?
                """,
                (status, active_job_id, utc_now(), note, name),
            )
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
