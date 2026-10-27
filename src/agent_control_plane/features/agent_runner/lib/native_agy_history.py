"""Read bounded scalar progress from the current child's native conversation DB."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

POLL_INTERVAL_SEC = 2.0
QUERY_BUDGET_SEC = 0.05
STEP_LIMIT = 1_000_000_000


@dataclass(frozen=True)
class NativeProgress:
    step_index: int
    step_count: int


class NativeAgyHistory:
    def __init__(self, workspace: Path, started_wall: float) -> None:
        self.workspace = workspace
        self.started_wall = started_wall
        self.next_poll = 0.0
        self.conversation_id: str | None = None
        self.file_identity: tuple[int, int] | None = None
        self.previous: NativeProgress | None = None

    def poll(
        self, conversation_id: str | None, cwd: str | None, now: float
    ) -> NativeProgress | None:
        if now < self.next_poll:
            return None
        self.next_poll = now + POLL_INTERVAL_SEC
        if (
            not self.workspace.is_absolute()
            or cwd != str(self.workspace)
            or conversation_id is None
        ):
            return None
        try:
            if str(UUID(conversation_id)) != conversation_id:
                return None
            if self.conversation_id is not None and self.conversation_id != conversation_id:
                return None
            home = Path.home()
            root = home / ".gemini" / "antigravity-cli" / "conversations"
            path = root / f"{conversation_id}.db"
            before = self._safe_file(home, path)
            if before is None or before.st_mtime < self.started_wall:
                return None
            identity = (before.st_dev, before.st_ino)
            if self.file_identity is not None and self.file_identity != identity:
                return None
            deadline = time.monotonic() + QUERY_BUDGET_SEC
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
                db.execute("PRAGMA query_only = ON")
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                # Read one consistent scalar snapshot; never select any BLOB columns.
                db.execute("BEGIN")
                tables = dict(
                    db.execute(
                        "SELECT name, rootpage FROM sqlite_master "
                        "WHERE type = 'table' AND name IN ('trajectory_meta', 'steps')"
                    )
                )
                if set(tables) != {"trajectory_meta", "steps"} or any(
                    type(page) is not int or page <= 0 for page in tables.values()
                ):
                    return None
                columns = {
                    row[1]: (row[2].upper(), row[5])
                    for row in db.execute("PRAGMA table_info(steps)")
                }
                if (
                    columns.get("idx") != ("INTEGER", 1)
                    or sum(primary_key > 0 for _, primary_key in columns.values()) != 1
                ):
                    return None
                # Only a sole INTEGER PRIMARY KEY with no PK backing index aliases rowid.
                # Composite/DESC/WITHOUT ROWID keys can store BLOBs despite INTEGER affinity.
                if any(row[3] == "pk" for row in db.execute("PRAGMA index_list(steps)")):
                    return None
                owners = db.execute(
                    "SELECT COUNT(*), SUM(CASE WHEN typeof(cascade_id) = 'text' "
                    "AND cascade_id COLLATE BINARY = ? THEN 1 ELSE 0 END) FROM trajectory_meta",
                    (conversation_id,),
                ).fetchone()
                if owners != (1, 1):
                    return None
                count, index = db.execute("SELECT COUNT(*), MAX(idx) FROM steps").fetchone()
            after = self._safe_file(home, path)
            if after is None or (after.st_dev, after.st_ino) != identity:
                return None
            if (
                type(count) is not int
                or type(index) is not int
                or not 0 < count <= STEP_LIMIT
                or not 0 <= index <= STEP_LIMIT
                or count > index + 1
            ):
                return None
            current = NativeProgress(index, count)
            self.conversation_id = conversation_id
            self.file_identity = identity
            previous = self.previous
            if previous is not None and (
                current.step_index < previous.step_index or current.step_count < previous.step_count
            ):
                return None
            self.previous = current
            # Establish a baseline without crediting existing native backlog as fresh work.
            if previous is None or (
                current.step_index <= previous.step_index
                or current.step_count <= previous.step_count
            ):
                return None
            return current
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return None

    @staticmethod
    def _safe_file(home: Path, path: Path) -> os.stat_result | None:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return None  # No portable stdlib ownership proof on this platform.
        current = home
        candidates = [home]
        for part in path.relative_to(home).parts:
            current /= part
            candidates.append(current)
        for candidate in candidates:
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != getuid():
                return None
            if candidate == path:
                if not stat.S_ISREG(info.st_mode):
                    return None
            elif not stat.S_ISDIR(info.st_mode):
                return None
        file_info = info
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                info = sidecar.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != getuid():
                    return None
        return file_info
