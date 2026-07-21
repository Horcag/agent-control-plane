from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from agent_control_plane.features.result_handoff import (
    NativeQualityGateRunner,
    inspect_native_quality_report,
)
from agent_control_plane.features.result_handoff.lib.native_quality import (
    _resolve_gate_executable,
)
from agent_control_plane.shared.config import NativeQualityGateConfig
from agent_control_plane.shared.native_quality import (
    NativeQualityContract,
    expand_native_quality_command,
    selected_native_quality_gates,
)


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
                run_on="controller",
            ),
            NativeQualityGateConfig(
                name="worker-only",
                command=(sys.executable, "-c", "raise SystemExit(8)"),
                run_on="worker",
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


def test_quality_gate_stage_selection_and_python_placeholder_are_deterministic() -> None:
    worker = NativeQualityGateConfig(
        name="worker",
        command=("ruff", "check", "{changed_python_files}"),
        include_globs=("*.py", "**/*.py"),
        run_on="worker",
    )
    controller = NativeQualityGateConfig(
        name="controller",
        command=("python", "-m", "pytest"),
        run_on="controller",
    )
    shared = NativeQualityGateConfig(
        name="shared",
        command=("ruff", "format", "--check", "{changed_python_files}"),
        include_globs=("*.py", "**/*.py"),
        run_on="both",
    )
    contract = NativeQualityContract(
        policy="controller",
        max_parallel=2,
        gates=(worker, controller, shared),
    )
    changed_files = ("README.md", "tests\\test_z.py", "src/a.py", "src/a.py")

    worker_gates = selected_native_quality_gates(
        contract,
        changed_files,
        stage="worker",
    )
    controller_gates = selected_native_quality_gates(
        contract,
        changed_files,
        stage="controller",
    )

    assert [gate.name for gate in worker_gates] == ["worker", "shared"]
    assert [gate.name for gate in controller_gates] == ["controller", "shared"]
    assert expand_native_quality_command(worker, changed_files) == (
        "ruff",
        "check",
        "./src/a.py",
        "./tests/test_z.py",
    )


def test_controller_gate_mode_selection_is_deterministic() -> None:
    controller = NativeQualityGateConfig(
        name="controller",
        command=("python", "-m", "pytest"),
        run_on="controller",
    )
    shared = NativeQualityGateConfig(
        name="shared",
        command=("ruff", "check", "."),
        run_on="both",
    )
    contract = NativeQualityContract(policy="controller", gates=(controller, shared))

    assert [
        gate.name
        for gate in selected_native_quality_gates(
            contract, ("src/app.py",), stage="controller", controller_gate_mode="full"
        )
    ] == ["controller", "shared"]
    assert [
        gate.name
        for gate in selected_native_quality_gates(
            contract, ("src/app.py",), stage="controller", controller_gate_mode="focused"
        )
    ] == ["shared"]
    assert (
        selected_native_quality_gates(
            contract, ("src/app.py",), stage="controller", controller_gate_mode="none"
        )
        == ()
    )


def test_controller_quality_inspection_rejects_gate_mode_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    contract = NativeQualityContract(
        policy="controller",
        gates=(NativeQualityGateConfig(name="shared", command=(sys.executable, "-c", "pass")),),
    )
    runner = NativeQualityGateRunner()
    runner.run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "mode",
        checkpoint_tree_sha="tree-mode",
        changed_files=("src/app.py",),
        contract=contract,
        controller_gate_mode="focused",
    )

    inspection = inspect_native_quality_report(
        tmp_path / "runs" / "mode",
        checkpoint_tree_sha="tree-mode",
        changed_files=("src/app.py",),
        contract=contract,
        controller_gate_mode="full",
    )

    assert inspection["state"] == "invalid"
    assert "gate mode" in inspection["error"]


def test_controller_quality_inspection_accepts_legacy_v2_only_for_full_mode(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    contract = NativeQualityContract(
        policy="controller",
        gates=(NativeQualityGateConfig(name="shared", command=(sys.executable, "-c", "pass")),),
    )
    run_dir = tmp_path / "runs" / "legacy"
    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=run_dir,
        checkpoint_tree_sha="tree-legacy",
        changed_files=("src/app.py",),
        contract=contract,
    )
    report["schema_version"] = 2
    del report["controller_gate_mode"]
    (run_dir / "native-quality.json").write_text(json.dumps(report), encoding="utf-8")

    full = inspect_native_quality_report(
        run_dir,
        checkpoint_tree_sha="tree-legacy",
        changed_files=("src/app.py",),
        contract=contract,
    )
    focused = inspect_native_quality_report(
        run_dir,
        checkpoint_tree_sha="tree-legacy",
        changed_files=("src/app.py",),
        contract=contract,
        controller_gate_mode="focused",
    )

    assert full["state"] == "valid"
    assert focused["state"] == "invalid"
    assert "legacy" in focused["error"]


def test_native_quality_contract_v2_round_trips_and_reads_v1_defaults() -> None:
    contract = NativeQualityContract(
        policy="controller",
        max_parallel=2,
        gates=(
            NativeQualityGateConfig(
                name="tests",
                command=("python", "-m", "pytest"),
                run_on="controller",
            ),
        ),
    )

    payload = contract.as_dict()
    restored = NativeQualityContract.from_dict(payload)
    legacy = NativeQualityContract.from_dict(
        {
            "schema_version": 1,
            "policy": "controller",
            "gates": [
                {
                    "name": "tests",
                    "command": ["python", "-m", "pytest"],
                    "working_dir": ".",
                    "timeout_sec": 300,
                    "include_globs": [],
                }
            ],
        }
    )

    assert payload["schema_version"] == 2
    assert payload["max_parallel"] == 2
    assert payload["gates"][0]["run_on"] == "controller"
    assert restored == contract
    assert legacy.max_parallel == 1
    assert legacy.gates[0].run_on == "both"


def test_controller_quality_uses_bounded_parallelism_and_preserves_gate_order(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    barrier_script = (
        "import sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text('ready'); end=time.monotonic()+2; "
        'exec("while not Path(sys.argv[2]).exists() and time.monotonic() < end:\\n'
        ' time.sleep(0.01)"); '
        "raise SystemExit(0 if Path(sys.argv[2]).exists() else 9)"
    )
    contract = NativeQualityContract(
        policy="controller",
        max_parallel=2,
        gates=(
            NativeQualityGateConfig(
                name="first",
                command=(sys.executable, "-c", barrier_script, "first.ready", "second.ready"),
                timeout_sec=5,
                run_on="controller",
            ),
            NativeQualityGateConfig(
                name="second",
                command=(sys.executable, "-c", barrier_script, "second.ready", "first.ready"),
                timeout_sec=5,
                run_on="controller",
            ),
        ),
    )

    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "parallel",
        checkpoint_tree_sha="tree-parallel",
        changed_files=("src/app.py",),
        contract=contract,
    )

    assert report["status"] == "passed"
    assert report["max_parallel"] == 2
    assert [check["name"] for check in report["checks"]] == ["first", "second"]


def test_controller_quality_expands_only_non_deleted_command_files(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "kept.py").write_text("print('ok')\n", encoding="utf-8")
    contract = NativeQualityContract(
        policy="controller",
        gates=(
            NativeQualityGateConfig(
                name="python-files",
                command=(
                    sys.executable,
                    "-c",
                    "import sys; print(*sys.argv[1:])",
                    "{changed_python_files}",
                ),
                include_globs=("*.py", "**/*.py"),
                run_on="controller",
            ),
        ),
    )

    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "expanded",
        checkpoint_tree_sha="tree-expanded",
        changed_files=("src/deleted.py", "src/kept.py", "README.md"),
        command_files=("src/kept.py", "README.md"),
        contract=contract,
    )

    assert report["status"] == "passed"
    assert report["command_files"] == ["src/kept.py", "README.md"]
    assert report["checks"][0]["command"][-1] == "./src/kept.py"
    assert "deleted.py" not in report["checks"][0]["command_display"]


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


def test_controller_quality_inspection_rejects_boolean_parallelism(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    run_dir = tmp_path / "runs" / "tampered-parallelism"
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
        checkpoint_tree_sha="tree-parallelism",
        changed_files=("src/app.py",),
        contract=contract,
    )
    report["max_parallel"] = True
    (run_dir / "native-quality.json").write_text(json.dumps(report), encoding="utf-8")

    inspection = inspect_native_quality_report(
        run_dir,
        checkpoint_tree_sha="tree-parallelism",
        changed_files=("src/app.py",),
        contract=contract,
    )

    assert inspection["state"] == "invalid"
    assert "parallelism" in (inspection["error"] or "")


def test_resolve_gate_executable_absolutizes_relative_path_with_directory(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / ".venv" / "Scripts"
    tool_dir.mkdir(parents=True)
    executable = tool_dir / "python.exe"
    executable.write_text("", encoding="utf-8")

    resolved = _resolve_gate_executable(tmp_path, (".venv/Scripts/python.exe", "-c", "print('ok')"))

    assert resolved[0] == str(executable.resolve(strict=False))
    assert resolved[1:] == ("-c", "print('ok')")


def test_resolve_gate_executable_leaves_bare_names_and_absolute_paths_unchanged(
    tmp_path: Path,
) -> None:
    absolute = tmp_path / "tool.exe"
    absolute.write_text("", encoding="utf-8")

    assert _resolve_gate_executable(tmp_path, ("git", "status")) == ("git", "status")
    assert _resolve_gate_executable(tmp_path, (str(absolute), "--flag")) == (
        str(absolute),
        "--flag",
    )


def test_resolve_gate_executable_leaves_missing_relative_path_unchanged(
    tmp_path: Path,
) -> None:
    command = (".venv/Scripts/python.exe", "-c", "print('ok')")

    assert _resolve_gate_executable(tmp_path, command) == command


def test_controller_quality_runs_workspace_relative_executable_gate(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    tools_dir = workspace / "tools"
    tools_dir.mkdir()
    relative_python = "tools/py" + (".exe" if sys.platform == "win32" else "")
    copied_python = workspace / relative_python
    base_executable = Path(getattr(sys, "_base_executable", sys.executable))
    shutil.copy2(base_executable, copied_python)
    if sys.platform == "win32":
        # The interpreter dynamically loads its runtime DLLs from its own
        # directory, so those need to sit next to the copied executable too.
        for dll in base_executable.parent.glob("*.dll"):
            shutil.copy2(dll, tools_dir / dll.name)
        # A venv-style pyvenv.cfg above the executable's directory lets it
        # locate the real stdlib without copying the whole interpreter home.
        (workspace / "pyvenv.cfg").write_text(
            f"home = {base_executable.parent}\n", encoding="utf-8"
        )
    contract = NativeQualityContract(
        policy="controller",
        gates=(
            NativeQualityGateConfig(
                name="relative-python",
                command=(relative_python, "-c", "print('ok')"),
                working_dir=Path("."),
                timeout_sec=10,
            ),
        ),
    )

    report = NativeQualityGateRunner().run(
        workspace_path=workspace,
        run_dir=tmp_path / "runs" / "relative-exe",
        checkpoint_tree_sha="tree-relative",
        changed_files=("src/app.py",),
        contract=contract,
    )

    assert report["checks"][0]["outcome"] == "passed"
    assert report["status"] == "passed"
