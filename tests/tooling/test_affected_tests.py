from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.run_affected_tests import changed_worktree_files, select_affected_tests

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_affected_tests.py"


def test_source_change_follows_transitive_import_graph(tmp_path: Path) -> None:
    _write(tmp_path, "src/example/__init__.py", "")
    _write(tmp_path, "src/example/core.py", "VALUE = 1\n")
    _write(tmp_path, "src/example/service.py", "from example.core import VALUE\n")
    _write(
        tmp_path,
        "tests/features/example/test_service.py",
        "from example.service import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
    )
    _write(tmp_path, "tests/shared/test_other.py", "def test_other():\n    assert True\n")
    _write(tmp_path, "tests/architecture/test_architecture.py", "def test_architecture(): pass\n")

    selection = select_affected_tests(tmp_path, ["src/example/core.py"])

    assert not selection.full_suite
    assert selection.tests == (
        "tests/architecture/test_architecture.py",
        "tests/features/example/test_service.py",
    )


def test_changed_test_selects_only_itself(tmp_path: Path) -> None:
    _write(tmp_path, "tests/shared/test_config.py", "def test_config(): pass\n")
    _write(tmp_path, "tests/shared/test_other.py", "def test_other(): pass\n")

    selection = select_affected_tests(tmp_path, ["tests/shared/test_config.py"])

    assert not selection.full_suite
    assert selection.tests == ("tests/shared/test_config.py",)


def test_deleted_source_file_falls_back_to_full_suite(tmp_path: Path) -> None:
    _write(tmp_path, "tests/shared/test_config.py", "def test_config(): pass\n")

    selection = select_affected_tests(tmp_path, ["src/example/deleted.py"])

    assert selection.full_suite
    assert "deleted" in (selection.reason or "")


def test_feature_asset_selects_mirrored_feature_tests(tmp_path: Path) -> None:
    _write(tmp_path, "src/example/features/widget/template.js", "export const value = 1;\n")
    _write(tmp_path, "tests/features/widget/test_template.py", "def test_template(): pass\n")
    _write(tmp_path, "tests/features/other/test_other.py", "def test_other(): pass\n")

    selection = select_affected_tests(
        tmp_path,
        ["src/example/features/widget/template.js"],
    )

    assert not selection.full_suite
    assert selection.tests == ("tests/features/widget/test_template.py",)


def test_docs_only_change_requires_no_tests(tmp_path: Path) -> None:
    _write(tmp_path, "docs/usage.md", "Usage\n")
    _write(tmp_path, "tests/shared/test_config.py", "def test_config(): pass\n")

    selection = select_affected_tests(tmp_path, ["docs/usage.md"])

    assert not selection.full_suite
    assert selection.tests == ()
    assert selection.reason == "documentation-only change"


def test_selector_or_project_configuration_change_runs_full_suite(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/run_affected_tests.py", "")
    _write(tmp_path, "tests/tooling/test_affected_tests.py", "def test_selector(): pass\n")

    selector = select_affected_tests(tmp_path, ["scripts/run_affected_tests.py"])
    project = select_affected_tests(tmp_path, ["pyproject.toml"])

    assert selector.full_suite
    assert project.full_suite


def test_changed_worktree_files_includes_tracked_untracked_and_deleted_paths(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/example/core.py", "VALUE = 1\n")
    _write(tmp_path, "src/example/deleted.py", "VALUE = 2\n")
    _git(tmp_path, "init")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=ACP Test",
        "-c",
        "user.email=acp-test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    (tmp_path / "src/example/core.py").write_text("VALUE = 3\n", encoding="utf-8")
    (tmp_path / "src/example/deleted.py").unlink()
    _write(tmp_path, "tests/test_new.py", "def test_new():\n    assert True\n")

    changed = changed_worktree_files(tmp_path)

    assert changed == (
        "src/example/core.py",
        "src/example/deleted.py",
        "tests/test_new.py",
    )


def test_full_suite_flag_forces_full_suite_despite_empty_change_set(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "ACP Test")
    _git(tmp_path, "config", "user.email", "acp-test@example.invalid")
    _write(tmp_path, "base.txt", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--repo",
            str(tmp_path),
            "--worktree",
            "--full-suite",
            "--list",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["changed_files"] == []
    assert payload["full_suite"] is True
    assert payload["reason"] == "explicit full-suite mode requested"


def test_full_suite_env_var_forces_full_suite_despite_empty_change_set(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "ACP Test")
    _git(tmp_path, "config", "user.email", "acp-test@example.invalid")
    _write(tmp_path, "base.txt", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(tmp_path), "--worktree", "--list"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "ACP_QUALITY_FULL_SUITE": "1"},
    )

    payload = json.loads(result.stdout)
    assert payload["full_suite"] is True


def test_empty_change_set_without_full_suite_flag_skips_everything(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "ACP Test")
    _git(tmp_path, "config", "user.email", "acp-test@example.invalid")
    _write(tmp_path, "base.txt", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(tmp_path), "--worktree", "--list"],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["changed_files"] == []
    assert payload["full_suite"] is False


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
