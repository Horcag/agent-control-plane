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
