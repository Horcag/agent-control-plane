from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_control_plane.app.runtime.cli import _install_brief_file, main
from agent_control_plane.app.runtime.orchestrator import PolicyError


def test_install_brief_file_writes_to_the_conventional_path(tmp_path: Path) -> None:
    coordination_root = tmp_path / ".agent-work"
    source = tmp_path / "source-brief.md"
    source.write_text("# Brief\n", encoding="utf-8")

    _install_brief_file(
        coordination_root=coordination_root,
        task_id="task-1",
        brief_file=source,
        overwrite=False,
    )

    conventional_path = coordination_root / "tasks" / "task-1" / "brief.md"
    assert conventional_path.read_text(encoding="utf-8") == "# Brief\n"


def test_install_brief_file_is_a_noop_when_existing_content_is_identical(
    tmp_path: Path,
) -> None:
    coordination_root = tmp_path / ".agent-work"
    conventional_path = coordination_root / "tasks" / "task-1" / "brief.md"
    conventional_path.parent.mkdir(parents=True)
    conventional_path.write_text("# Brief\n", encoding="utf-8")
    source = tmp_path / "source-brief.md"
    source.write_text("# Brief\n", encoding="utf-8")

    _install_brief_file(
        coordination_root=coordination_root,
        task_id="task-1",
        brief_file=source,
        overwrite=False,
    )

    assert conventional_path.read_text(encoding="utf-8") == "# Brief\n"


def test_install_brief_file_refuses_to_silently_clobber_a_differing_brief(
    tmp_path: Path,
) -> None:
    coordination_root = tmp_path / ".agent-work"
    conventional_path = coordination_root / "tasks" / "task-1" / "brief.md"
    conventional_path.parent.mkdir(parents=True)
    conventional_path.write_text("# Original\n", encoding="utf-8")
    source = tmp_path / "source-brief.md"
    source.write_text("# Replacement\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        _install_brief_file(
            coordination_root=coordination_root,
            task_id="task-1",
            brief_file=source,
            overwrite=False,
        )

    assert str(conventional_path) in str(excinfo.value)
    assert str(source) in str(excinfo.value)
    assert conventional_path.read_text(encoding="utf-8") == "# Original\n"


def test_install_brief_file_overwrite_flag_replaces_a_differing_brief(
    tmp_path: Path,
) -> None:
    coordination_root = tmp_path / ".agent-work"
    conventional_path = coordination_root / "tasks" / "task-1" / "brief.md"
    conventional_path.parent.mkdir(parents=True)
    conventional_path.write_text("# Original\n", encoding="utf-8")
    source = tmp_path / "source-brief.md"
    source.write_text("# Replacement\n", encoding="utf-8")

    _install_brief_file(
        coordination_root=coordination_root,
        task_id="task-1",
        brief_file=source,
        overwrite=True,
    )

    assert conventional_path.read_text(encoding="utf-8") == "# Replacement\n"


def test_cli_start_brief_file_installs_before_launch(tmp_path: Path) -> None:
    coordination_root = tmp_path / ".agent-work"
    source = tmp_path / "source-brief.md"
    source.write_text("# Brief\n", encoding="utf-8")

    mock_control = MagicMock()
    mock_control.config.coordination_root = coordination_root
    job = MagicMock()
    job.job_id = "job-1"
    job.status = "running"
    job.alerts = None
    mock_control.start_job.return_value = job

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ):
        exit_code = main(
            [
                "start",
                "--task-id",
                "task-1",
                "--route",
                "dev",
                "--brief-file",
                str(source),
                "--config",
                str(tmp_path / "workspaces.toml"),
            ]
        )

    assert exit_code == 0
    conventional_path = coordination_root / "tasks" / "task-1" / "brief.md"
    assert conventional_path.read_text(encoding="utf-8") == "# Brief\n"
    mock_control.start_job.assert_called_once()


def test_cli_start_brief_file_conflict_blocks_launch_before_start_job(
    tmp_path: Path,
) -> None:
    coordination_root = tmp_path / ".agent-work"
    conventional_path = coordination_root / "tasks" / "task-1" / "brief.md"
    conventional_path.parent.mkdir(parents=True)
    conventional_path.write_text("# Original\n", encoding="utf-8")
    source = tmp_path / "source-brief.md"
    source.write_text("# Replacement\n", encoding="utf-8")

    mock_control = MagicMock()
    mock_control.config.coordination_root = coordination_root

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ):
        exit_code = main(
            [
                "start",
                "--task-id",
                "task-1",
                "--route",
                "dev",
                "--brief-file",
                str(source),
                "--config",
                str(tmp_path / "workspaces.toml"),
            ]
        )

    assert exit_code == 2
    mock_control.start_job.assert_not_called()
    assert conventional_path.read_text(encoding="utf-8") == "# Original\n"


def test_cli_retired_config_blocks_brief_installation_before_creating_task_directory(
    tmp_path: Path,
) -> None:
    coordination_root = tmp_path / ".agent-work"
    source = tmp_path / "source-brief.md"
    source.write_text("# Brief\n", encoding="utf-8")
    mock_control = MagicMock()
    mock_control.config.coordination_root = coordination_root
    mock_control.ensure_route_admitted.side_effect = PolicyError("configuration is retired")

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ):
        exit_code = main(
            [
                "start",
                "--task-id",
                "task-1",
                "--route",
                "dev",
                "--brief-file",
                str(source),
                "--config",
                str(tmp_path / "workspaces.toml"),
            ]
        )

    assert exit_code == 2
    assert not (coordination_root / "tasks" / "task-1").exists()
    mock_control.ensure_route_admitted.assert_called_once_with("dev")
    mock_control.start_job.assert_not_called()


def test_cli_start_wait_returns_off_contract_exit_code_and_reports_supervision(
    tmp_path: Path,
) -> None:
    mock_control = MagicMock()
    job = MagicMock()
    job.job_id = "job-blocked"
    job.status = "queued"
    job.alerts = None
    mock_control.start_job.return_value = job
    mock_control.watch_job.return_value = {
        "status": "blocked",
        "expected_result_status": "completed",
        "finalization_status": "completed",
        "settled": True,
        "on_contract": False,
        "timed_out": False,
    }
    mock_control.supervision_for.return_value = {
        "supervised": False,
        "watch_command": "agent-control watch --events job-blocked",
    }

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ):
        exit_code = main(
            [
                "start",
                "--task-id",
                "task-blocked",
                "--route",
                "dev",
                "--wait",
                "--poll-interval-sec",
                "0",
                "--wait-timeout-sec",
                "1",
                "--config",
                str(tmp_path / "workspaces.toml"),
            ]
        )

    assert exit_code == 1
    mock_control.watch_job.assert_called_once()


def test_cli_reconcile_returns_nonzero_when_recovery_has_errors(tmp_path: Path) -> None:
    mock_control = MagicMock()
    mock_control.reconcile_jobs.return_value = {
        "reconciled_orphaned_jobs": [],
        "reconciled_terminal_jobs": [],
        "live_jobs": [],
        "live_runner_conflicts": [],
        "runner_identity_conflicts": [],
        "terminated_orphan_runners": [],
        "worker_identity_conflicts": [],
        "errors": ["job-1: checkpoint verification failed"],
    }

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ):
        exit_code = main(
            [
                "reconcile",
                "--job-id",
                "job-1",
                "--config",
                str(tmp_path / "workspaces.toml"),
            ]
        )

    assert exit_code == 1
