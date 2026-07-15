from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 30_000

Migration = Callable[[sqlite3.Connection], None]

_MIGRATIONS_TABLE_SQL = """
create table if not exists schema_migrations (
    component text not null,
    version integer not null,
    checksum text not null,
    applied_at text not null,
    primary key(component, version)
)
"""


@contextmanager
def control_database(
    database_path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Iterator[sqlite3.Connection]:
    """Open one configured ACP SQLite unit of work without changing journal mode."""
    _validate_timeout(busy_timeout_ms)
    path = database_path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"pragma busy_timeout = {busy_timeout_ms}")  # nosec B608
        db.execute("pragma foreign_keys = on")
        db.execute("pragma synchronous = normal")
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
    finally:
        db.close()


def bootstrap_control_database(
    database_path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> None:
    """Perform the one-time, persistent ACP database bootstrap.

    WAL conversion is intentionally kept out of :func:`control_database`: an ordinary
    status/read connection must never attempt a journal-mode transition while an older
    worker still owns a write transaction.
    """
    _validate_timeout(busy_timeout_ms)
    path = database_path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (busy_timeout_ms / 1000)
    while True:
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
            db.execute(f"pragma busy_timeout = {busy_timeout_ms}")  # nosec B608
            current_mode = str(db.execute("pragma journal_mode").fetchone()[0]).lower()
            if current_mode != "wal":
                enabled_mode = str(db.execute("pragma journal_mode = wal").fetchone()[0]).lower()
                if enabled_mode != "wal":
                    raise RuntimeError(
                        f"Could not enable WAL for ACP database {path}: {enabled_mode}"
                    )
            db.execute("pragma synchronous = normal")
            db.execute(_MIGRATIONS_TABLE_SQL)
            db.commit()
            return
        except sqlite3.OperationalError as exc:
            if db is not None:
                db.rollback()
            if not _is_lock_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        finally:
            if db is not None:
                db.close()


def apply_schema_migration(
    database_path: Path,
    *,
    component: str,
    version: int,
    checksum: str,
    migrate: Migration,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> bool:
    """Apply one component migration exactly once under a cross-process write lock."""
    normalized_component = component.strip()
    normalized_checksum = checksum.strip()
    if not normalized_component:
        raise ValueError("component must not be empty")
    if version <= 0:
        raise ValueError("version must be positive")
    if not normalized_checksum:
        raise ValueError("checksum must not be empty")
    bootstrap_control_database(database_path, busy_timeout_ms=busy_timeout_ms)

    with control_database(database_path, busy_timeout_ms=busy_timeout_ms) as db:
        existing = _migration_checksum(db, normalized_component, version)
    if existing is not None:
        _require_matching_checksum(
            normalized_component,
            version,
            expected=normalized_checksum,
            actual=existing,
        )
        return False

    with control_database(database_path, busy_timeout_ms=busy_timeout_ms) as db:
        db.execute("begin immediate")
        existing = _migration_checksum(db, normalized_component, version)
        if existing is not None:
            _require_matching_checksum(
                normalized_component,
                version,
                expected=normalized_checksum,
                actual=existing,
            )
            return False
        migrate(db)
        db.execute(
            """
            insert into schema_migrations (component, version, checksum, applied_at)
            values (?, ?, ?, ?)
            """,
            (
                normalized_component,
                version,
                normalized_checksum,
                datetime.now(UTC).isoformat(),
            ),
        )
    return True


def _migration_checksum(db: sqlite3.Connection, component: str, version: int) -> str | None:
    row = db.execute(
        "select checksum from schema_migrations where component = ? and version = ?",
        (component, version),
    ).fetchone()
    return str(row["checksum"]) if row is not None else None


def _require_matching_checksum(
    component: str,
    version: int,
    *,
    expected: str,
    actual: str,
) -> None:
    if actual != expected:
        raise RuntimeError(
            f"Migration checksum mismatch for {component} v{version}: "
            f"database={actual!r}, code={expected!r}"
        )


def _validate_timeout(busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message
