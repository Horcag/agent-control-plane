from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

# Canary paths spanning a range of depths and name shapes. A pattern that matches
# every one of them matches (for practical purposes) the whole tree, regardless of
# how it is spelled -- this is what lets glob_matches_whole_tree() catch not just a
# bare "**" but any equivalent.
_TOP_LEVEL_CANARIES = (
    "a",
    "a.ext",
    ".hidden",
)
_NESTED_CANARIES = (
    "dir/file.ext",
    "very/deeply/nested/path/to/a/file.ext",
)
_WHOLE_TREE_CANARIES = _TOP_LEVEL_CANARIES + _NESTED_CANARIES


def compile_forward_slash_glob(pattern: str) -> re.Pattern[str]:
    """Compile a `/`-separated glob (`**`, `*`, `?`) into a regex.

    The pattern and the paths it is matched against are always treated as
    forward-slash text -- callers own converting platform paths (e.g. Windows
    backslashes) before matching, so behaviour never depends on the OS running
    the guard.
    """
    normalized = pattern.replace("\\", "/")
    parts: list[str] = []
    i = 0
    while i < len(normalized):
        if normalized[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif normalized[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif normalized[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(normalized[i]))
            i += 1
    return re.compile(f"^{''.join(parts)}$")


def glob_matches(path: str, pattern: str) -> bool:
    """Match a forward-slash path against a forward-slash glob pattern."""
    return compile_forward_slash_glob(pattern).match(path.replace("\\", "/")) is not None


def glob_matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(glob_matches(path, pattern) for pattern in patterns)


def glob_matches_whole_tree(pattern: str) -> bool:
    """Return whether `pattern` would silence a path guard for practical purposes.

    Used to reject ignore-list entries that blanket the tree instead of narrowly
    excluding operator-owned paths. Two shapes qualify:

    - it matches every canary, e.g. a bare `**`;
    - it matches every *nested* canary, e.g. `**/**` or `**/*`. Such a pattern
      spares only files sitting directly in the root, and no real repository
      keeps its sources there, so the guard is effectively off.
    """
    return all(glob_matches(canary, pattern) for canary in _WHOLE_TREE_CANARIES) or all(
        glob_matches(canary, pattern) for canary in _NESTED_CANARIES
    )


def is_same_or_child(path: Path, parent: Path) -> bool:
    path = path.resolve(strict=False)
    parent = parent.resolve(strict=False)
    return path == parent or is_child(path, parent)


def is_child(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def is_known_temporary_patch_artifact(path_text: str) -> bool:
    """Return whether a path names a proven throwaway patch artifact."""
    name = Path(path_text).name.lower()
    return name.endswith((".rej", ".orig")) or (
        name.endswith(".patch") and name.startswith(("tmp_", "single_"))
    )
