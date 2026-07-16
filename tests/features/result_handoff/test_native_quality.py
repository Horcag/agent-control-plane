from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_control_plane.features.result_handoff import (
    NativeQualityGateRunner,
    inspect_native_quality_report,
)
from agent_control_plane.shared.config import NativeQualityGateConfig
from agent_control_plane.shared.native_quality import NativeQualityContract


def test_controller_quality_runs_only_matching_gates_and_persists_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    run_dir = tmp_path / "runs" / "job"
    contract = NativeQualityContract(
        policy="controller",
        gates=(
            NativeQualityGateConfig(
                name="python",
                command=(sys.executable, "-c", "print('python-ok')"),
                working_dir=Path("."),
                timeout_sec=10,
                include_globs=("*.py", "**/*.py"),
            ),
            NativeQualityGateConfig(
                name="frontend",
                command=(sys.executable, "-c", "raise SystemExit(9)"),
                working_dir=Path("."),
                timeout_sec=10,
                include_globs=("frontend/**",),
            ),
        ),
    )

    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=run_dir,
        checkpoint_tree_sha="tree-1",
        changed_files=("src/app.py",),
        contract=contract,
    )

    assert report["status"] == "passed"
    assert report["checkpoint_tree_sha"] == "tree-1"
    assert report["contract_sha256"] == contract.sha256
    assert [check["name"] for check in report["checks"]] == ["python"]
    assert report["checks"][0]["outcome"] == "passed"
    assert "python-ok" in report["checks"][0]["output_tail"]
    assert (run_dir / "native-quality.json").is_file()


def test_controller_quality_fails_closed_for_uncovered_or_failed_changes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runner = NativeQualityGateRunner()
    gated = NativeQualityContract(
        policy="controller",
        gates=(
            NativeQualityGateConfig(
                name="python",
                command=(sys.executable, "-c", "raise SystemExit(7)"),
                working_dir=Path("."),
                timeout_sec=10,
                include_globs=("*.py", "**/*.py"),
            ),
        ),
    )

    uncovered = runner.run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "uncovered",
        checkpoint_tree_sha="tree-docs",
        changed_files=("README.md",),
        contract=gated,
    )
    failed = runner.run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "failed",
        checkpoint_tree_sha="tree-python",
        changed_files=("src/app.py",),
        contract=gated,
    )

    assert uncovered["status"] == "failed"
    assert uncovered["reason"] == "no configured quality gate matched the changed files"
    assert failed["status"] == "failed"
    assert failed["checks"][0]["outcome"] == "failed"
    assert failed["checks"][0]["exit_code"] == 7


def test_controller_quality_timeout_is_recorded_as_failed_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    contract = NativeQualityContract(
        policy="controller",
        gates=(
            NativeQualityGateConfig(
                name="slow",
                command=(sys.executable, "-c", "import time; time.sleep(5)"),
                timeout_sec=1,
            ),
        ),
    )

    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "timeout",
        checkpoint_tree_sha="tree-timeout",
        changed_files=("src/app.py",),
        contract=contract,
    )

    assert report["status"] == "failed"
    assert report["checks"][0]["outcome"] == "timed_out"
    assert report["checks"][0]["exit_code"] is None


def test_controller_quality_inspection_rejects_contradictory_check_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    run_dir = tmp_path / "runs" / "tampered"
    contract = NativeQualityContract(
        policy="controller",
        gates=(
            NativeQualityGateConfig(
                name="passing",
                command=(sys.executable, "-c", "print('ok')"),
            ),
        ),
    )
    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=run_dir,
        checkpoint_tree_sha="tree-tampered",
        changed_files=("src/app.py",),
        contract=contract,
    )
    report["checks"][0]["exit_code"] = 9
    (run_dir / "native-quality.json").write_text(json.dumps(report), encoding="utf-8")

    inspection = inspect_native_quality_report(
        run_dir,
        checkpoint_tree_sha="tree-tampered",
        changed_files=("src/app.py",),
        contract=contract,
    )

    assert inspection["state"] == "invalid"
    assert "exit code" in (inspection["error"] or "")
