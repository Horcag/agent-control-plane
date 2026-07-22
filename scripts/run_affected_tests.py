from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess  # nosec B404
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FULL_SUITE_ENV_VAR = "ACP_QUALITY_FULL_SUITE"


@dataclass(frozen=True)
class TestSelection:
    tests: tuple[str, ...]
    full_suite: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class DependencyGraph:
    paths_by_module: dict[str, str]
    reverse_dependencies: dict[str, frozenset[str]]
    test_files: frozenset[str]

    @classmethod
    def build(cls, repo_root: Path) -> DependencyGraph:
        python_files = _python_files(repo_root)
        modules_by_path = {
            relative_path: _module_name(relative_path) for relative_path in python_files
        }
        paths_by_module = {
            module: relative_path
            for relative_path, module in modules_by_path.items()
            if module is not None
        }
        dependencies: dict[str, set[str]] = defaultdict(set)
        for relative_path in python_files:
            source = (repo_root / relative_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative_path)
            current_module = modules_by_path[relative_path]
            for imported_module in _imported_modules(tree, current_module, relative_path):
                dependencies[relative_path].update(_import_paths(imported_module, paths_by_module))
        reverse: dict[str, set[str]] = defaultdict(set)
        for importer, imported_paths in dependencies.items():
            for imported_path in imported_paths:
                reverse[imported_path].add(importer)
        return cls(
            paths_by_module=paths_by_module,
            reverse_dependencies={
                path: frozenset(importers) for path, importers in reverse.items()
            },
            test_files=frozenset(path for path in python_files if _is_test_file(path)),
        )

    def affected_tests(self, changed_source: Iterable[str]) -> set[str]:
        reachable = set(changed_source)
        pending = deque(changed_source)
        while pending:
            dependency = pending.popleft()
            for importer in self.reverse_dependencies.get(dependency, ()):
                if importer in reachable:
                    continue
                reachable.add(importer)
                pending.append(importer)
        return reachable.intersection(self.test_files)


def select_affected_tests(
    repo_root: Path,
    changed_paths: Iterable[str],
) -> TestSelection:
    root = repo_root.resolve(strict=False)
    changed = tuple(sorted({_normalize_path(path) for path in changed_paths if path.strip()}))
    if not changed:
        return TestSelection((), reason="no changed files")
    full_suite_reason = _full_suite_trigger(changed)
    if full_suite_reason is not None:
        return TestSelection((), full_suite=True, reason=full_suite_reason)

    selected: set[str] = set()
    source_python: list[str] = []
    documentation_paths: list[str] = []
    source_assets: list[str] = []
    for relative_path in changed:
        absolute_path = root / relative_path
        if _is_documentation(relative_path):
            documentation_paths.append(relative_path)
            continue
        if _is_test_file(relative_path):
            if not absolute_path.is_file():
                return TestSelection(
                    (),
                    full_suite=True,
                    reason=f"changed or deleted test is unavailable: {relative_path}",
                )
            selected.add(relative_path)
            continue
        if relative_path.startswith("src/") and relative_path.endswith(".py"):
            if not absolute_path.is_file():
                return TestSelection(
                    (),
                    full_suite=True,
                    reason=f"changed or deleted source is unavailable: {relative_path}",
                )
            source_python.append(relative_path)
            continue
        if relative_path.startswith("src/"):
            source_assets.append(relative_path)
            continue
        return TestSelection(
            (),
            full_suite=True,
            reason=f"change has no safe dependency mapping: {relative_path}",
        )

    if source_python:
        try:
            graph = DependencyGraph.build(root)
        except (OSError, SyntaxError, UnicodeError) as exc:
            return TestSelection(
                (),
                full_suite=True,
                reason=f"dependency graph could not be built: {exc}",
            )
        selected.update(graph.affected_tests(source_python))
        selected.update(_mirrored_tests(root, source_python))
        architecture_test = "tests/architecture/test_architecture.py"
        if (root / architecture_test).is_file():
            selected.add(architecture_test)
        if not selected:
            return TestSelection(
                (),
                full_suite=True,
                reason="changed source has no statically reachable tests",
            )

    if source_assets:
        asset_tests = _mirrored_tests(root, source_assets)
        if not asset_tests:
            return TestSelection(
                (),
                full_suite=True,
                reason="changed source asset has no mirrored feature tests",
            )
        selected.update(asset_tests)

    if not selected and len(documentation_paths) == len(changed):
        return TestSelection((), reason="documentation-only change")
    return TestSelection(tuple(sorted(selected)))


def changed_files_since(repo_root: Path, base: str, head: str = "HEAD") -> tuple[str, ...]:
    result = subprocess.run(  # nosec B603
        ["git", "diff", "--name-only", "--diff-filter=ACMRD", f"{base}...{head}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git diff failed"
        raise RuntimeError(detail)
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def changed_worktree_files(repo_root: Path) -> tuple[str, ...]:
    result = subprocess.run(  # nosec B603
        ["git", "status", "--porcelain=v1", "-z", "-uall"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git status failed"
        raise RuntimeError(detail)
    entries = result.stdout.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise RuntimeError("malformed git status entry")
        status = entry[:2]
        paths.add(_normalize_path(entry[3:]))
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(entries) or not entries[index]:
                raise RuntimeError("malformed git rename/copy entry")
            paths.add(_normalize_path(entries[index]))
            index += 1
    return tuple(sorted(paths))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run tests affected by the transitive Python import graph.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", help="Git base revision for the change set")
    source.add_argument(
        "--worktree",
        action="store_true",
        help="Select from tracked, untracked, renamed, and deleted worktree paths",
    )
    parser.add_argument("--head", default="HEAD", help="Git head revision (default: HEAD)")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository root")
    parser.add_argument("--list", action="store_true", help="Print selection JSON without pytest")
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help=(
            "Force the full test suite regardless of the change set "
            f"(also honors {FULL_SUITE_ENV_VAR}=1). Used for zero-change verification "
            "evidence, where an empty change set must not silently skip testing."
        ),
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    repo_root = args.repo.resolve(strict=False)
    force_full_suite = args.full_suite or os.environ.get(FULL_SUITE_ENV_VAR) == "1"
    try:
        changed = (
            changed_worktree_files(repo_root)
            if args.worktree
            else changed_files_since(repo_root, args.base, args.head)
        )
        if force_full_suite:
            selection = TestSelection(
                (), full_suite=True, reason="explicit full-suite mode requested"
            )
        else:
            selection = select_affected_tests(repo_root, changed)
    except RuntimeError as exc:
        changed = ()
        selection = TestSelection((), full_suite=True, reason=f"git comparison failed: {exc}")
    _print_selection(selection, changed)
    if args.list:
        return 0
    if not selection.full_suite and not selection.tests:
        return 0
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    command = [sys.executable, "-m", "pytest", *pytest_args]
    if not selection.full_suite:
        command.extend(selection.tests)
    return subprocess.run(command, cwd=repo_root, check=False).returncode  # nosec B603


def _print_selection(selection: TestSelection, changed: Iterable[str]) -> None:
    payload = {
        "changed_files": list(changed),
        "full_suite": selection.full_suite,
        "reason": selection.reason,
        "tests": list(selection.tests),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _python_files(repo_root: Path) -> tuple[str, ...]:
    paths = [*repo_root.glob("src/**/*.py"), *repo_root.glob("tests/**/*.py")]
    return tuple(sorted(path.relative_to(repo_root).as_posix() for path in paths if path.is_file()))


def _module_name(relative_path: str) -> str | None:
    path = PurePosixPath(relative_path)
    if path.suffix != ".py":
        return None
    if path.parts[0] == "src":
        parts = list(path.with_suffix("").parts[1:])
    elif path.parts[0] == "tests":
        parts = list(path.with_suffix("").parts)
    else:
        return None
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(
    tree: ast.AST,
    current_module: str | None,
    relative_path: str,
) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _resolve_import_from(node, current_module, relative_path)
        if not module:
            continue
        imported.add(module)
        imported.update(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")
    return imported


def _resolve_import_from(
    node: ast.ImportFrom,
    current_module: str | None,
    relative_path: str,
) -> str | None:
    if node.level == 0:
        return node.module
    if current_module is None:
        return None
    current_parts = current_module.split(".")
    if not relative_path.endswith("/__init__.py"):
        current_parts.pop()
    parents_to_remove = node.level - 1
    if parents_to_remove > len(current_parts):
        return None
    base_parts = current_parts[: len(current_parts) - parents_to_remove]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _import_paths(module: str, paths_by_module: dict[str, str]) -> set[str]:
    parts = module.split(".")
    imported_paths: set[str] = set()
    for index in range(1, len(parts) + 1):
        candidate = ".".join(parts[:index])
        path = paths_by_module.get(candidate)
        if path is None:
            continue
        if index == len(parts) or path.endswith("/__init__.py"):
            imported_paths.add(path)
    return imported_paths


def _mirrored_tests(repo_root: Path, changed_paths: Iterable[str]) -> set[str]:
    selected: set[str] = set()
    for relative_path in changed_paths:
        mirror = _test_mirror(relative_path)
        if mirror is None:
            continue
        directory = repo_root / mirror
        if not directory.is_dir():
            continue
        selected.update(
            path.relative_to(repo_root).as_posix()
            for path in directory.rglob("test_*.py")
            if path.is_file()
        )
    return selected


def _test_mirror(relative_path: str) -> str | None:
    parts = PurePosixPath(relative_path).parts
    for layer in ("app", "entities", "features", "shared"):
        if layer not in parts:
            continue
        index = parts.index(layer)
        if layer == "shared":
            return "tests/shared"
        if index + 1 < len(parts):
            return f"tests/{layer}/{parts[index + 1]}"
    return None


def _full_suite_trigger(changed: Iterable[str]) -> str | None:
    for relative_path in changed:
        if relative_path in {"pyproject.toml", "uv.lock"}:
            return f"project configuration changed: {relative_path}"
        if relative_path.startswith((".github/", "config/", "scripts/")):
            return f"test infrastructure changed: {relative_path}"
    return None


def _is_documentation(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    return (
        relative_path.startswith("docs/")
        or path.name in {"README.md", "SECURITY.md", "LICENSE", "AGENTS.md"}
        or (path.suffix == ".md" and not relative_path.startswith(("src/", "tests/")))
    )


def _is_test_file(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    return path.parts[:1] == ("tests",) and path.name.startswith("test_") and path.suffix == ".py"


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


if __name__ == "__main__":
    raise SystemExit(main())
