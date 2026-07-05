from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True)
class ForbiddenStatusEntry:
    status: str
    path: str
    matched_glob: str


def find_forbidden_status_entries(
    porcelain: str,
    forbidden_globs: tuple[str, ...],
) -> list[ForbiddenStatusEntry]:
    entries: list[ForbiddenStatusEntry] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = _status_path(line)
        normalized = _normalize(path)
        for pattern in forbidden_globs:
            if _matches(normalized, pattern):
                entries.append(ForbiddenStatusEntry(status=status, path=path, matched_glob=pattern))
                break
    return entries


def find_new_forbidden_status_entries(
    porcelain: str,
    forbidden_globs: tuple[str, ...],
    baseline_entries: list[ForbiddenStatusEntry],
) -> list[ForbiddenStatusEntry]:
    baseline = {_entry_key(entry) for entry in baseline_entries}
    return [
        entry
        for entry in find_forbidden_status_entries(porcelain, forbidden_globs)
        if _entry_key(entry) not in baseline
    ]


def _entry_key(entry: ForbiddenStatusEntry) -> tuple[str, str, str]:
    return entry.status, _normalize(entry.path), _normalize(entry.matched_glob)


def _status_path(line: str) -> str:
    path = line[3:].strip()
    if " -> " in path:
        path = path.rsplit(" -> ", maxsplit=1)[1].strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _matches(path: str, pattern: str) -> bool:
    normalized_pattern = _normalize(pattern)
    if fnmatch(path, normalized_pattern):
        return True
    return fnmatch(path, f"**/{normalized_pattern}")
