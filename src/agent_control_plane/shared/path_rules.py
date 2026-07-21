from __future__ import annotations

from pathlib import Path


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
