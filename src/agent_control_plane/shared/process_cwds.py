from __future__ import annotations

import os
from pathlib import Path


def live_process_cwds(root: Path) -> list[int]:
    if os.name == "nt" or not Path("/proc").is_dir():
        return [-1]
    canonical = root.resolve(strict=False)
    matches: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except (PermissionError, OSError):
        return [-2]
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except OSError:
            return [-2]
        if cwd == canonical or cwd.is_relative_to(canonical):
            matches.append(int(entry.name))
    return matches
