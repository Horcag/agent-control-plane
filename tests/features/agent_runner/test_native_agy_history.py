from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agent_control_plane.features.agent_runner.lib.native_agy_history import (
    NativeAgyHistory,
    NativeProgress,
)

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="native profile ownership proof requires POSIX uid"
)


def database(path: Path, cid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE trajectory_meta(trajectory_id TEXT, cascade_id TEXT); CREATE TABLE steps(idx INTEGER PRIMARY KEY, metadata BLOB, step_payload BLOB);"
        )
        db.execute("INSERT INTO trajectory_meta VALUES (?, ?)", (str(uuid4()), cid))
        db.execute("INSERT INTO steps VALUES (0, ?, ?)", (b"PRIVATE", b"PRIVATE"))


def setup_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[NativeAgyHistory, Path, str, str]:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    cid = str(uuid4())
    path = home / ".gemini/antigravity-cli/conversations" / f"{cid}.db"
    database(path, cid)
    workspace = str(tmp_path / "workspace")
    return NativeAgyHistory(Path(workspace), 0), path, cid, workspace


def test_scalar_history_advances_without_stdout_and_never_reads_payload_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer, path, cid, workspace = setup_history(tmp_path, monkeypatch)
    connect = sqlite3.connect
    reads = []

    def restricted_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if table == "steps" and column not in ("idx", ""):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(sqlite3, "connect", restricted_connect)
    assert observer.poll(cid, workspace, 0) is None
    with connect(path) as db:
        db.executemany(
            "INSERT INTO steps VALUES (?, ?, ?)",
            [(index, b"PRIVATE", b"PRIVATE") for index in range(1, 5)],
        )
    assert observer.poll(cid, workspace, 1) is None  # Coalesced, no query.
    assert observer.poll(cid, workspace, 2) == NativeProgress(4, 5)
    assert observer.poll(cid, workspace, 4) is None
    with connect(path) as db:
        db.execute("DELETE FROM steps WHERE idx = 4")
    assert observer.poll(cid, workspace, 6) is None  # Retrograde never replaces high-water mark.
    with connect(path) as db:
        db.execute("INSERT INTO steps VALUES (4, NULL, NULL)")
    assert observer.poll(cid, workspace, 8) is None
    assert ("steps", "idx") in reads
    assert not any(column in ("metadata", "step_payload") for _, column in reads)


@pytest.mark.parametrize(
    "failure",
    [
        "cwd",
        "cid",
        "cid_blob",
        "cid_collation",
        "owner",
        "old",
        "symlink",
        "profile_symlink",
        "missing",
        "malformed",
        "schema",
        "replaced",
    ],
)
def test_unproven_native_history_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    observer, path, cid, workspace = setup_history(tmp_path, monkeypatch)
    if failure == "cwd":
        workspace += "/other"
    elif failure == "cid":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE trajectory_meta SET cascade_id = ?", (str(uuid4()),))
    elif failure == "cid_blob":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE trajectory_meta SET cascade_id = ?", (b"PRIVATE" * 10_000,))
    elif failure == "cid_collation":
        with sqlite3.connect(path) as db:
            db.executescript(
                "DROP TABLE trajectory_meta; "
                "CREATE TABLE trajectory_meta(trajectory_id TEXT, cascade_id TEXT COLLATE NOCASE);"
            )
            db.execute("INSERT INTO trajectory_meta VALUES (?, ?)", (str(uuid4()), cid.upper()))
    elif failure == "owner":
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    elif failure == "old":
        observer.started_wall = time.time() + 60
    elif failure == "symlink":
        target = path.with_suffix(".elsewhere")
        path.rename(target)
        path.symlink_to(target)
    elif failure == "profile_symlink":
        profile = path.parent
        other = profile.with_name("foreign-profile")
        profile.rename(other)
        profile.symlink_to(other, target_is_directory=True)
    elif failure == "missing":
        path.unlink()
    elif failure == "malformed":
        path.write_bytes(b"not sqlite")
    elif failure == "schema":
        with sqlite3.connect(path) as db:
            db.executescript("DROP TABLE steps; CREATE VIEW steps AS SELECT 0 AS idx;")
    elif failure == "replaced":
        assert observer.poll(cid, workspace, 0) is None
        replacement = path.with_suffix(".replacement")
        database(replacement, cid)
        os.replace(replacement, path)
    assert observer.poll(cid, workspace, 2) is None
    assert observer.previous is None or failure == "replaced"


def test_locked_history_is_optional_and_does_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer, path, cid, workspace = setup_history(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert observer.poll(cid, workspace, 0) is None
        assert time.monotonic() - started < 0.5


def test_observer_cannot_switch_conversation_after_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer, path, cid, workspace = setup_history(tmp_path, monkeypatch)
    assert observer.poll(cid, workspace, 0) is None
    other_cid = str(uuid4())
    database(path.with_name(f"{other_cid}.db"), other_cid)
    assert observer.poll(other_cid, workspace, 2) is None
    assert observer.conversation_id == cid


def test_count_only_backfill_is_baseline_not_new_history_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer, path, cid, workspace = setup_history(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO steps VALUES (2, NULL, NULL)")
    assert observer.poll(cid, workspace, 0) is None
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO steps VALUES (1, NULL, NULL)")
    assert observer.poll(cid, workspace, 2) is None
    assert observer.previous == NativeProgress(2, 3)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO steps VALUES (3, NULL, NULL)")
    assert observer.poll(cid, workspace, 4) == NativeProgress(3, 4)


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE steps(idx INTEGER, other INTEGER, PRIMARY KEY(idx, other))",
        "CREATE TABLE steps(idx INTEGER PRIMARY KEY DESC)",
        "CREATE TABLE steps(idx INTEGER PRIMARY KEY) WITHOUT ROWID",
    ],
)
def test_non_rowid_alias_schema_is_rejected_before_reading_blob_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str
) -> None:
    observer, path, cid, workspace = setup_history(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE steps")
        db.execute(schema)
        db.execute("INSERT INTO steps(idx) VALUES (?)", (b"PRIVATE" * 10_000,))
    connect = sqlite3.connect
    reads = []

    def checked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ and table == "steps":
                reads.append(column)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    assert observer.poll(cid, workspace, 0) is None
    assert reads == []
