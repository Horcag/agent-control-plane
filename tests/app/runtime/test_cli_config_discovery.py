from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_control_plane.app.runtime.cli import main
from agent_control_plane.shared.config import default_config_path


def test_cli_explicit_config_used_unchanged_and_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    explicit_cfg = tmp_path / "custom_workspaces.toml"
    explicit_cfg.write_text("[control]\n", encoding="utf-8")

    mock_control = MagicMock()
    mock_control.store.list_jobs.return_value = []

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ) as mock_from_config:
        exit_code = main(["list", "--config", str(explicit_cfg)])
        assert exit_code == 0
        mock_from_config.assert_called_once_with(str(explicit_cfg))

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.strip() == "[]"


def test_cli_omitted_config_discovers_config_and_prints_stderr_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work_dir = tmp_path / "project_root" / "subdir"
    work_dir.mkdir(parents=True)
    discovered_cfg = tmp_path / "project_root" / ".agent-work" / "workspaces.toml"
    discovered_cfg.parent.mkdir(parents=True)
    discovered_cfg.write_text("[control]\n", encoding="utf-8")

    monkeypatch.chdir(work_dir)

    mock_control = MagicMock()
    mock_control.store.list_jobs.return_value = []

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ) as mock_from_config:
        exit_code = main(["list"])
        assert exit_code == 0
        mock_from_config.assert_called_once_with(discovered_cfg)

    captured = capsys.readouterr()
    assert str(discovered_cfg) in captured.err
    assert str(work_dir) in captured.err
    assert "Using discovered config" in captured.err
    assert captured.out.strip() == "[]"


def test_cli_statuses_alone_works_with_no_config_present(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["statuses", "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert '"terminal_statuses"' in captured.out


def test_cli_statuses_accepts_and_ignores_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["statuses", "--json"])
    assert exit_code == 0
    without_config = capsys.readouterr().out

    exit_code = main(["statuses", "--json", "--config", "/does/not/exist.toml"])
    assert exit_code == 0
    with_nonexistent_config = capsys.readouterr()

    assert with_nonexistent_config.err == ""
    assert with_nonexistent_config.out == without_config


def test_cli_omitted_config_fallback_to_default_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_dir = tmp_path / "empty_dir"
    clean_dir.mkdir()

    monkeypatch.chdir(clean_dir)
    monkeypatch.setattr(
        "agent_control_plane.shared.config.known_configs_path",
        lambda: tmp_path / "nonexistent_known.json",
    )

    mock_control = MagicMock()
    mock_control.store.list_jobs.return_value = []

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=mock_control,
    ) as mock_from_config:
        exit_code = main(["list"])
        assert exit_code == 0
        mock_from_config.assert_called_once_with(default_config_path())

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.strip() == "[]"
