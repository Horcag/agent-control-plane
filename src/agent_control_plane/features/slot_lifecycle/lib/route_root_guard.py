from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agent_control_plane.shared.path_rules import glob_matches_any

StatusFingerprint = tuple[str, str]

_CONFLICT_STATUSES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


@dataclass(frozen=True)
class RouteRootSnapshot:
    head: str | None
    entries: Mapping[str, StatusFingerprint]
    porcelain: str
    stable: bool


@dataclass
class RouteRootGuard:
    head: str | None
    entries: dict[str, StatusFingerprint]
    pending_index_since: float | None = None
    ignore_globs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.entries = dict(self.entries)

    def evaluate(
        self,
        snapshot: RouteRootSnapshot,
        *,
        now: float,
        staged_grace_sec: float,
    ) -> tuple[str, ...]:
        if not snapshot.stable:
            return ()

        changed = self._changed_paths(snapshot)
        if snapshot.head != self.head:
            # A baseline entry that disappeared because it was committed is not
            # a violation; only a path still present in the new snapshot (new or
            # further modified) is genuinely new dirty state.
            new_dirty = tuple(path for path in changed if path in snapshot.entries)
            violating = self._violating(new_dirty)
            if violating:
                return violating
            self.head = snapshot.head
            self.entries = dict(snapshot.entries)
            self.pending_index_since = None
            return ()

        if not changed:
            self.pending_index_since = None
            return ()

        violating = self._violating(changed)
        if not violating:
            self.pending_index_since = None
            return ()

        if self._only_index_changes(snapshot, violating):
            if self.pending_index_since is None:
                self.pending_index_since = now
                return ()
            if now - self.pending_index_since < staged_grace_sec:
                return ()

        return violating

    def ignored_changed_paths(self, snapshot: RouteRootSnapshot) -> tuple[str, ...]:
        """Paths that changed but were excluded from violation determination.

        Diagnostic-only: never affects `evaluate`'s baseline bookkeeping, and
        callers must not use this to decide whether dirty state was preserved.
        """
        if not self.ignore_globs:
            return ()
        return tuple(path for path in self._changed_paths(snapshot) if self._is_ignored(path))

    def _changed_paths(self, snapshot: RouteRootSnapshot) -> tuple[str, ...]:
        paths = self.entries.keys() | snapshot.entries.keys()
        return tuple(
            sorted(path for path in paths if self.entries.get(path) != snapshot.entries.get(path))
        )

    def _violating(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        if not self.ignore_globs:
            return paths
        return tuple(path for path in paths if not self._is_ignored(path))

    def _is_ignored(self, path: str) -> bool:
        return glob_matches_any(path, self.ignore_globs)

    @staticmethod
    def _only_index_changes(
        snapshot: RouteRootSnapshot,
        changed: tuple[str, ...],
    ) -> bool:
        return all(
            (entry := snapshot.entries.get(path)) is not None and _is_index_only(entry[0])
            for path in changed
        )


def _is_index_only(status: str) -> bool:
    return (
        len(status) == 2
        and status not in _CONFLICT_STATUSES
        and status[0] not in {" ", "?", "!"}
        and status[1] == " "
    )
