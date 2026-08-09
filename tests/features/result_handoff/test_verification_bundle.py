from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.features.result_handoff import (
    build_verification_bundle,
    parse_result_report,
)
from agent_control_plane.features.result_handoff.lib.verification_bundle import (
    _assess_worker_quality,
    _controller_blocked_reason,
    normalize_repo_path,
)
from agent_control_plane.shared.config import NativeQualityGateConfig
from agent_control_plane.shared.native_quality import (
    NativeQualityContract,
    expand_native_quality_command,
    format_gate_command,
)


def test_result_report_parser_separates_claims_and_flags_missing_sections() -> None:
    complete = parse_result_report(
        """Status: completed

Changed files:
- src/app.py
- tests/test_app.py

What changed:
- Added the bounded behavior.

Verification performed:
- pytest -q tests/test_app.py (passed)
- ruff check src/app.py (passed)

Not verified / remaining risks:
- Sonar was unavailable.
"""
    )
    incomplete = parse_result_report("Status: completed\nChanged files: none\n")

    assert complete["format_valid"] is True
    assert complete["missing_sections"] == []
    assert complete["changed_files_claimed"] == ["src/app.py", "tests/test_app.py"]
    assert complete["verification_claims"] == [
        "pytest -q tests/test_app.py (passed)",
        "ruff check src/app.py (passed)",
    ]
    assert complete["claims_trust"] == "worker_reported"
    assert incomplete["format_valid"] is False
    assert incomplete["missing_sections"] == [
        "what_changed",
        "verification_performed",
        "remaining_risks",
    ]


def test_result_report_parser_normalizes_generated_git_status_file_list() -> None:
    report = parse_result_report(
        """Status: blocked

Changed files: M src/app.py; ?? tests/test_app.py

What changed: preserved

Verification performed: none

Not verified / remaining risks: interrupted
"""
    )

    assert report["changed_files_claimed"] == ["src/app.py", "tests/test_app.py"]


def test_normalize_repo_path() -> None:
    assert normalize_repo_path("./src/app.py") == "src/app.py"
    assert normalize_repo_path(".\\src\\app.py") == "src/app.py"
    assert normalize_repo_path("src/./app.py//") == "src/app.py"
    assert normalize_repo_path("Foo.py") == "Foo.py"
    assert normalize_repo_path("foo.py") == "foo.py"
    assert normalize_repo_path("") == ""
    assert normalize_repo_path("./") == ""
    assert normalize_repo_path(".") == ""


def test_result_report_parser_normalizes_paths_with_dot_slash_and_backslashes() -> None:
    report = parse_result_report(
        """Status: completed

Changed files:
- ./src/app.py
- .\\tests\\test_app.py

What changed: updated

Verification performed: pytest

Not verified / remaining risks: none
"""
    )
    assert report["changed_files_claimed"] == ["src/app.py", "tests/test_app.py"]


def test_assess_worker_quality_normalizes_dot_slash_and_backslash_paths() -> None:
    gate = NativeQualityGateConfig(
        name="ruff-changed",
        command=(".venv/Scripts/python.exe", "-m", "ruff", "check", "{changed_python_files}"),
        working_dir=Path("."),
        timeout_sec=30,
        include_globs=("*.py", "**/*.py"),
        run_on="both",
    )
    contract = NativeQualityContract(policy="worker", gates=(gate,))

    # Worker paths with ./ match bare checkpoint paths
    worker_verif_dot_slash = {
        "state": "valid",
        "payload": {
            "schema_version": 1,
            "status": "completed",
            "changed_files": [{"change": "modified", "path": "./src/app.py"}],
            "checks": [
                {
                    "command": ".venv/Scripts/python.exe -m ruff check 'src/app.py'",
                    "cwd": ".",
                    "exit_code": 0,
                    "outcome": "passed",
                }
            ],
            "unverified": [],
        },
    }
    res_dot = _assess_worker_quality(
        worker_verif_dot_slash,
        workspace_path=None,
        changed_paths=("src/app.py",),
        command_paths=("src/app.py",),
        checkpoint_paths=("src/app.py",),
        contract=contract,
        required=True,
    )
    assert res_dot["status"] == "passed"
    assert res_dot["changed_files_missing"] == []
    assert res_dot["changed_files_unobserved"] == []

    # Worker paths with backslashes match bare checkpoint paths
    worker_verif_backslash = {
        "state": "valid",
        "payload": {
            "schema_version": 1,
            "status": "completed",
            "changed_files": [{"change": "modified", "path": ".\\src\\app.py"}],
            "checks": [
                {
                    "command": ".venv/Scripts/python.exe -m ruff check 'src/app.py'",
                    "cwd": ".",
                    "exit_code": 0,
                    "outcome": "passed",
                }
            ],
            "unverified": [],
        },
    }
    res_slash = _assess_worker_quality(
        worker_verif_backslash,
        workspace_path=None,
        changed_paths=("src/app.py",),
        command_paths=("src/app.py",),
        checkpoint_paths=("src/app.py",),
        contract=contract,
        required=True,
    )
    assert res_slash["status"] == "passed"
    assert res_slash["changed_files_missing"] == []
    assert res_slash["changed_files_unobserved"] == []


def test_assess_worker_quality_flags_genuine_mismatches_and_case_differences() -> None:
    gate = NativeQualityGateConfig(
        name="ruff-changed",
        command=(".venv/Scripts/python.exe", "-m", "ruff", "check", "{changed_python_files}"),
        working_dir=Path("."),
        timeout_sec=30,
        include_globs=("*.py", "**/*.py"),
        run_on="both",
    )
    contract = NativeQualityContract(policy="worker", gates=(gate,))

    # Genuine mismatch (worker claims extra file and misses expected file)
    worker_verif_mismatch = {
        "state": "valid",
        "payload": {
            "schema_version": 1,
            "status": "completed",
            "changed_files": [{"change": "modified", "path": "./src/other.py"}],
            "checks": [],
            "unverified": [],
        },
    }
    res_mismatch = _assess_worker_quality(
        worker_verif_mismatch,
        workspace_path=None,
        changed_paths=("src/app.py",),
        command_paths=("src/app.py",),
        checkpoint_paths=("src/app.py",),
        contract=contract,
        required=True,
    )
    assert res_mismatch["status"] == "failed"
    assert res_mismatch["changed_files_missing"] == ["src/app.py"]
    assert res_mismatch["changed_files_unobserved"] == ["src/other.py"]

    # Case difference must count as mismatch
    worker_verif_case = {
        "state": "valid",
        "payload": {
            "schema_version": 1,
            "status": "completed",
            "changed_files": [{"change": "modified", "path": "./src/App.py"}],
            "checks": [],
            "unverified": [],
        },
    }
    res_case = _assess_worker_quality(
        worker_verif_case,
        workspace_path=None,
        changed_paths=("src/app.py",),
        command_paths=("src/app.py",),
        checkpoint_paths=("src/app.py",),
        contract=contract,
        required=True,
    )
    assert res_case["status"] == "failed"
    assert res_case["changed_files_missing"] == ["src/app.py"]
    assert res_case["changed_files_unobserved"] == ["src/App.py"]


def test_build_verification_bundle_normalizes_comparison_sites(tmp_path: Path) -> None:
    result_md = tmp_path / "result.md"
    result_md.write_text(
        """Status: completed

Changed files:
- ./src/app.py

What changed: updated

Verification performed: passed

Not verified / remaining risks: none
""",
        encoding="utf-8",
    )

    verif_json = tmp_path / "verification.json"
    verif_json.write_text(
        """{
  "schema_version": 1,
  "status": "completed",
  "changed_files": [{"change": "modified", "path": ".\\\\src\\\\app.py"}],
  "checks": [{"command": "pytest", "cwd": ".", "outcome": "passed", "exit_code": 0, "summary": "1 passed"}],
  "unverified": []
}""",
        encoding="utf-8",
    )

    class DummyCheckpoint:
        workspace_path = tmp_path
        ref_name = "refs/acp/checkpoints/test"
        commit_sha = "1234567890abcdef1234567890abcdef12345678"

    with (
        patch(
            "agent_control_plane.features.result_handoff.lib.verification_bundle.verify_slot_checkpoint"
        ),
        patch(
            "agent_control_plane.features.result_handoff.lib.verification_bundle.checkpoint_changed_files",
            return_value=[{"path": "src/app.py", "status": "M"}],
        ),
        patch(
            "agent_control_plane.features.result_handoff.lib.verification_bundle.checkpoint_temporary_patch_artifacts",
            return_value=(),
        ),
    ):
        bundle = build_verification_bundle(
            result_md,
            workspace_path=tmp_path,
            checkpoint=DummyCheckpoint(),  # type: ignore[arg-type]
        )

    assert bundle["changed_file_claims_not_observed"] == []
    assert bundle["changed_files_not_claimed"] == []
    assert bundle["worker_changed_files_not_observed"] == []
    assert bundle["actual_changed_files_missing_from_worker_bundle"] == []
    assert bundle["worker_quality"]["changed_files_missing"] == []
    assert bundle["worker_quality"]["changed_files_unobserved"] == []


def test_controller_blocked_reason_names_a_timed_out_gate_as_unresolved_not_a_defect() -> None:
    controller_quality = {
        "state": "valid",
        "payload": {
            "status": "timed_out",
            "checks": [
                {"name": "affected-tests", "outcome": "timed_out"},
                {"name": "ruff-changed", "outcome": "passed"},
            ],
        },
    }

    reason = _controller_blocked_reason(controller_quality)

    assert "affected-tests" in reason
    assert "timed out" in reason
    assert "not a confirmed defect" in reason


def test_controller_blocked_reason_names_a_failed_gate_as_a_failure() -> None:
    controller_quality = {
        "state": "valid",
        "payload": {
            "status": "failed",
            "checks": [{"name": "mypy-linux", "outcome": "failed"}],
        },
    }

    reason = _controller_blocked_reason(controller_quality)

    assert reason == "mypy-linux failed"


def test_controller_blocked_reason_reports_missing_evidence_state() -> None:
    controller_quality = {"state": "missing", "error": "controller quality report is missing"}

    reason = _controller_blocked_reason(controller_quality)

    assert reason == "controller quality evidence is missing: controller quality report is missing"


def test_build_verification_bundle_reads_a_timed_out_gate_as_unresolved_not_failed(
    tmp_path: Path,
) -> None:
    result_md = tmp_path / "result.md"
    result_md.write_text(
        """Status: completed

Changed files:
- src/app.py

What changed: updated

Verification performed: passed

Not verified / remaining risks: none
""",
        encoding="utf-8",
    )
    verif_json = tmp_path / "verification.json"
    verif_json.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [{"change": "modified", "path": "src/app.py"}],
                "checks": [
                    {
                        "command": "pytest",
                        "cwd": ".",
                        "outcome": "passed",
                        "exit_code": 0,
                        "summary": "1 passed",
                    }
                ],
                "unverified": [],
            }
        ),
        encoding="utf-8",
    )

    gate = NativeQualityGateConfig(
        name="affected-tests",
        command=("pytest",),
        timeout_sec=1200,
        run_on="controller",
    )
    contract = NativeQualityContract(policy="controller", gates=(gate,))
    command = expand_native_quality_command(gate, ("src/app.py",))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "native-quality.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "status": "timed_out",
                "reason": "one or more controller quality gates timed out",
                "checkpoint_tree_sha": "tree-timeout",
                "contract_sha256": contract.sha256,
                "controller_gate_mode": "full",
                "changed_files": ["src/app.py"],
                "command_files": ["src/app.py"],
                "max_parallel": contract.max_parallel,
                "checks": [
                    {
                        "name": "affected-tests",
                        "command": list(command),
                        "command_display": format_gate_command(gate, ("src/app.py",)),
                        "cwd": str((tmp_path / gate.working_dir).resolve()),
                        "outcome": "timed_out",
                        "exit_code": None,
                        "duration_ms": 1_200_000,
                        "output_tail": "",
                    }
                ],
                "claims_trust": "controller_executed",
            }
        ),
        encoding="utf-8",
    )

    class DummyCheckpoint:
        workspace_path = tmp_path
        ref_name = "refs/acp/checkpoints/test"
        commit_sha = "1234567890abcdef1234567890abcdef12345678"
        tree_sha = "tree-timeout"

    with (
        patch(
            "agent_control_plane.features.result_handoff.lib.verification_bundle.verify_slot_checkpoint"
        ),
        patch(
            "agent_control_plane.features.result_handoff.lib.verification_bundle.checkpoint_changed_files",
            return_value=[{"path": "src/app.py", "status": "M"}],
        ),
        patch(
            "agent_control_plane.features.result_handoff.lib.verification_bundle.checkpoint_temporary_patch_artifacts",
            return_value=(),
        ),
    ):
        bundle = build_verification_bundle(
            result_md,
            workspace_path=tmp_path,
            checkpoint=DummyCheckpoint(),  # type: ignore[arg-type]
            run_dir=run_dir,
            quality_contract=contract,
            controller_gate_mode="full",
        )

    assert bundle["review_ready"] is False
    assert bundle["controller_quality"]["payload"]["status"] == "timed_out"
    assert bundle["review_blocked_reason"] is not None
    assert "affected-tests" in bundle["review_blocked_reason"]
    assert "timed out" in bundle["review_blocked_reason"]
    assert "not a confirmed defect" in bundle["review_blocked_reason"]
