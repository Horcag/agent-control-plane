from __future__ import annotations

import tomllib
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.shared.config import ControlConfig
from agent_control_plane.shared.git_tools import GitError, run_git


class ConfigBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfigBootstrapResult:
    config_path: Path
    changed: bool
    route: str
    slot: str
    route_added: bool
    slot_added: bool
    repo_path: Path
    slot_path: Path
    required_branch: str
    source_roots: tuple[Path, ...]
    test_roots: tuple[Path, ...]
    exclude_dirs: tuple[Path, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "changed": self.changed,
            "route": self.route,
            "slot": self.slot,
            "route_added": self.route_added,
            "slot_added": self.slot_added,
            "repo_path": str(self.repo_path),
            "slot_path": str(self.slot_path),
            "required_branch": self.required_branch,
            "source_roots": [path.as_posix() for path in self.source_roots],
            "test_roots": [path.as_posix() for path in self.test_roots],
            "exclude_dirs": [path.as_posix() for path in self.exclude_dirs],
        }


@dataclass(frozen=True)
class RepoLayout:
    source_roots: tuple[Path, ...]
    test_roots: tuple[Path, ...]
    exclude_dirs: tuple[Path, ...]


def bootstrap_slot_config(
    config: ControlConfig,
    *,
    slot_name: str,
    route_name: str,
    repo_path: Path | None = None,
    required_branch: str | None = None,
    slot_path: Path | None = None,
) -> ConfigBootstrapResult:
    route_config = config.routes.get(route_name)
    repo = repo_path or (route_config.path if route_config else None)
    if repo is None:
        raise ConfigBootstrapError("repo_path is required when the route is not configured")
    repo = repo.resolve(strict=False)
    if not repo.exists():
        raise ConfigBootstrapError(f"Repository path does not exist: {repo}")

    branch = required_branch or (
        route_config.required_branch if route_config else _current_branch(repo)
    )
    if not branch:
        raise ConfigBootstrapError(f"Could not infer required branch for route {route_name}")

    target_slot_path = (slot_path or (config.slot_root / slot_name)).resolve(strict=False)
    layout = (
        RepoLayout(
            source_roots=route_config.source_roots,
            test_roots=route_config.test_roots,
            exclude_dirs=route_config.exclude_dirs,
        )
        if route_config is not None
        else infer_repo_layout(repo)
    )
    raw = _load_raw_toml(config.config_path)
    route_added = False
    slot_added = False
    additions: list[str] = []

    if route_config is not None:
        if repo_path is not None and route_config.path.resolve(strict=False) != repo:
            raise ConfigBootstrapError(
                f"Route {route_name} already points to {route_config.path}, not {repo}"
            )
        if required_branch is not None and route_config.required_branch != required_branch:
            raise ConfigBootstrapError(
                f"Route {route_name} already requires {route_config.required_branch!r}, "
                f"not {required_branch!r}"
            )
    else:
        additions.append(
            _format_route_table(
                route_name=route_name,
                repo_path=repo,
                required_branch=branch,
                worktree_root=config.worktree_root,
                source_roots=layout.source_roots,
                test_roots=layout.test_roots,
                exclude_dirs=layout.exclude_dirs,
            )
        )
        route_added = True

    existing_slot = config.slots.get(slot_name)
    if existing_slot is not None:
        if existing_slot.route != route_name:
            raise ConfigBootstrapError(
                f"Slot {slot_name} already belongs to route {existing_slot.route!r}, "
                f"not {route_name!r}"
            )
        if existing_slot.path.resolve(strict=False) != target_slot_path:
            raise ConfigBootstrapError(
                f"Slot {slot_name} already points to {existing_slot.path}, not {target_slot_path}"
            )
    else:
        _ensure_slot_table_absent(raw, slot_name)
        additions.append(_format_slot_table(slot_name, route_name, target_slot_path))
        slot_added = True

    if additions:
        _append_tables(config.config_path, additions)

    return ConfigBootstrapResult(
        config_path=config.config_path,
        changed=bool(additions),
        route=route_name,
        slot=slot_name,
        route_added=route_added,
        slot_added=slot_added,
        repo_path=repo,
        slot_path=target_slot_path,
        required_branch=branch,
        source_roots=layout.source_roots,
        test_roots=layout.test_roots,
        exclude_dirs=layout.exclude_dirs,
    )


def infer_repo_layout(repo_path: Path) -> RepoLayout:
    iml_layout = _layout_from_iml(repo_path)
    if iml_layout is not None:
        source_roots = iml_layout.source_roots or _infer_source_roots(repo_path)
        test_roots = _merge_paths(iml_layout.test_roots, _infer_test_roots(repo_path))
        exclude_dirs = _merge_paths(iml_layout.exclude_dirs, _infer_exclude_dirs(repo_path))
        return RepoLayout(source_roots, test_roots, exclude_dirs)
    return RepoLayout(
        source_roots=_infer_source_roots(repo_path),
        test_roots=_infer_test_roots(repo_path),
        exclude_dirs=_infer_exclude_dirs(repo_path),
    )


def _current_branch(repo_path: Path) -> str:
    try:
        return run_git(repo_path, "branch", "--show-current")
    except GitError as exc:
        raise ConfigBootstrapError(f"Could not read current branch in {repo_path}: {exc}") from exc


def _load_raw_toml(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def _ensure_slot_table_absent(raw: dict[str, Any], slot_name: str) -> None:
    slots = raw.get("slots", {})
    if isinstance(slots, dict) and slot_name in slots:
        raise ConfigBootstrapError(f"Slot table already exists but was not loaded: {slot_name}")


def _format_route_table(
    *,
    route_name: str,
    repo_path: Path,
    required_branch: str,
    worktree_root: Path,
    source_roots: tuple[Path, ...],
    test_roots: tuple[Path, ...],
    exclude_dirs: tuple[Path, ...],
) -> str:
    lines = [
        f"[routes.{_toml_key(route_name)}]",
        f'path = "{_toml_path(repo_path)}"',
        f'required_branch = "{_escape(required_branch)}"',
        f'worktree_root = "{_toml_path(worktree_root)}"',
        f'worktree_base = "{_toml_path(repo_path)}"',
    ]
    if source_roots:
        lines.append(f"source_roots = {_toml_path_array(source_roots)}")
    if test_roots:
        lines.append(f"test_roots = {_toml_path_array(test_roots)}")
    if exclude_dirs:
        lines.append("exclude_dirs = [")
        lines.extend(f'  "{_toml_path(path)}",' for path in exclude_dirs)
        lines.append("]")
    return "\n".join(lines)


def _format_slot_table(slot_name: str, route_name: str, slot_path: Path) -> str:
    return "\n".join(
        [
            f'[slots."{_escape(slot_name)}"]',
            f'route = "{_escape(route_name)}"',
            f'path = "{_toml_path(slot_path)}"',
        ]
    )


def _append_tables(config_path: Path, tables: list[str]) -> None:
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text + "\n\n" + "\n\n".join(tables) + "\n", encoding="utf-8")


def _layout_from_iml(repo_path: Path) -> RepoLayout | None:
    module_file = repo_path / f"{repo_path.name}.iml"
    if not module_file.exists():
        return None
    root = ET.parse(module_file).getroot()  # nosec B314
    source_roots: list[Path] = []
    test_roots: list[Path] = []
    exclude_dirs: list[Path] = []
    for source in root.findall(".//sourceFolder"):
        path = _module_dir_relative(source.get("url"))
        if path is None:
            continue
        if source.get("isTestSource") == "true":
            test_roots.append(path)
        else:
            source_roots.append(path)
    for excluded in root.findall(".//excludeFolder"):
        path = _module_dir_relative(excluded.get("url"))
        if path is not None:
            exclude_dirs.append(path)
    return RepoLayout(
        source_roots=tuple(source_roots),
        test_roots=tuple(test_roots),
        exclude_dirs=tuple(exclude_dirs),
    )


def _module_dir_relative(url: str | None) -> Path | None:
    prefix = "file://$MODULE_DIR$/"
    if not url or not url.startswith(prefix):
        return None
    text = url.removeprefix(prefix).strip("/")
    if not text:
        return None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _infer_source_roots(repo_path: Path) -> tuple[Path, ...]:
    candidates = [
        Path("backend/src"),
        Path("backend"),
        Path("frontend/src"),
        Path("src"),
    ]
    roots: list[Path] = []
    for candidate in candidates:
        if (repo_path / candidate).is_dir() and not _is_shadowed(candidate, roots):
            roots.append(candidate)
    return tuple(roots)


def _infer_test_roots(repo_path: Path) -> tuple[Path, ...]:
    candidates = [Path("backend/tests"), Path("frontend/tests"), Path("tests")]
    return tuple(candidate for candidate in candidates if (repo_path / candidate).is_dir())


def _infer_exclude_dirs(repo_path: Path) -> tuple[Path, ...]:
    candidates = [
        Path(".venv"),
        Path("backend/.venv"),
        Path("frontend/.venv"),
        Path("frontend/.next"),
        Path("frontend/coverage"),
        Path("frontend/node_modules"),
        Path("frontend/playwright-report"),
        Path("frontend/test-results"),
        Path("dist"),
        Path("frontend/build"),
        Path("frontend/dist"),
        Path(".mypy_cache"),
        Path(".pytest_cache"),
        Path(".ruff_cache"),
        Path("backend/.mypy_cache"),
        Path("backend/.pytest_cache"),
        Path("frontend/.mypy_cache"),
        Path("frontend/.pytest_cache"),
        Path("logs"),
        Path("out"),
        Path("scratch"),
    ]
    return tuple(candidate for candidate in candidates if (repo_path / candidate).exists())


def _merge_paths(first: tuple[Path, ...], second: tuple[Path, ...]) -> tuple[Path, ...]:
    merged: list[Path] = []
    for path in [*first, *second]:
        if path not in merged:
            merged.append(path)
    return tuple(merged)


def _is_shadowed(path: Path, roots: list[Path]) -> bool:
    return any(
        path == root or path.is_relative_to(root) or root.is_relative_to(path) for root in roots
    )


def _toml_key(value: str) -> str:
    if value.replace("_", "").replace("-", "").isalnum():
        return value
    return f'"{_escape(value)}"'


def _toml_path(path: Path) -> str:
    return _escape(path.as_posix())


def _toml_path_array(paths: tuple[Path, ...]) -> str:
    return "[" + ", ".join(f'"{_toml_path(path)}"' for path in paths) + "]"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
