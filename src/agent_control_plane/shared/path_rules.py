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
