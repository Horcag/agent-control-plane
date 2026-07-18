from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

from agent_control_plane.app.runtime.cli import main
from agent_control_plane.app.runtime.orchestrator import PolicyError


def test_demo_run_show_and_accept_are_offline_and_durable(tmp_path: Path, capsys) -> None:
    output = tmp_path / "offline-demo"

    assert main(["demo", "run", "--output", str(output)]) == 0
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["status"] == "completed"
    assert (output / "manifest.json").is_file()

    assert main(["demo", "show", str(output)]) == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["job"]["status"] == "completed"
    assert show_payload["attempt_count"] == 2
    assert show_payload["slot"]["status"] == "available"
    assert show_payload["inbox"]["review_status"] == "pending"
    assert show_payload["inbox"]["review_ready"] is True

    assert main(["demo", "accept", str(output)]) == 0
    accepted_payload = json.loads(capsys.readouterr().out)
    assert accepted_payload["status"] == "accepted"
    with sqlite3.connect(output / "runs" / "jobs.sqlite3") as database:
        assert database.execute("select status from review_spans").fetchone() == ("completed",)

    assert main(["demo", "accept", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"


def test_demo_run_refuses_non_empty_directory_and_file(tmp_path: Path, capsys) -> None:
    output = tmp_path / "occupied"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("do not touch", encoding="utf-8")

    assert main(["demo", "run", "--output", str(output)]) == 2
    assert marker.read_text(encoding="utf-8") == "do not touch"
    assert "must be empty" in capsys.readouterr().err

    file_output = tmp_path / "occupied-file"
    file_output.write_text("do not touch", encoding="utf-8")
    assert main(["demo", "run", "--output", str(file_output)]) == 2
    assert file_output.read_text(encoding="utf-8") == "do not touch"
    assert "must be a directory" in capsys.readouterr().err


def test_demo_show_and_accept_reject_invalid_roots(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"
    assert main(["demo", "show", str(missing)]) == 2
    assert "root is unavailable" in capsys.readouterr().err

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "manifest.json").write_text("[]", encoding="utf-8")
    assert main(["demo", "show", str(malformed)]) == 2
    assert "manifest is invalid" in capsys.readouterr().err
    assert main(["demo", "accept", str(malformed)]) == 2
    assert "manifest is invalid" in capsys.readouterr().err


def test_model_catalog_command_prints_payload_from_selected_config(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "custom-workspaces.toml"
    payload = {
        "status": "loaded",
        "source": "custom-models.json",
        "version": "cache-v1",
        "models": [],
    }
    control = Mock()
    control.model_catalog_inspection.return_value = payload

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=control,
    ) as from_config_path:
        assert main(["model-catalog", "--config", str(config_path)]) == 0

    assert json.loads(capsys.readouterr().out) == payload
    from_config_path.assert_called_once_with(str(config_path))
    control.model_catalog_inspection.assert_called_once_with()


def test_model_routing_explain_command_prints_json_and_reports_unknown_policy(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "custom-workspaces.toml"
    payload = {"route": "main", "policy": "adaptive", "selection_source": "history"}
    control = Mock()
    control.model_routing_explain.return_value = payload

    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=control,
    ) as from_config_path:
        assert (
            main(
                [
                    "model-routing-explain",
                    "adaptive",
                    "--route",
                    "main",
                    "--config",
                    str(config_path),
                ]
            )
            == 0
        )

        assert json.loads(capsys.readouterr().out) == payload
        from_config_path.assert_called_once_with(str(config_path))
        control.model_routing_explain.assert_called_once_with("adaptive", "main")

        control.model_routing_explain.side_effect = PolicyError(
            "Unsupported Codex routing policy 'missing'"
        )
        assert (
            main(
                [
                    "model-routing-explain",
                    "missing",
                    "--route",
                    "main",
                    "--config",
                    str(config_path),
                ]
            )
            == 2
        )
    assert "Unsupported Codex routing policy 'missing'" in capsys.readouterr().err


def test_smoke_cli_preserves_structured_payload_and_exit_status(tmp_path: Path, capsys) -> None:
    control = Mock()
    control.smoke.side_effect = [
        {"status": "failed", "failures": [{"code": "smoke_failure"}]},
        {"status": "passed", "failures": []},
    ]
    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=control,
    ):
        assert main(["smoke", "--config", str(tmp_path / "config.toml")]) == 1
        failed_payload = json.loads(capsys.readouterr().out)
        assert failed_payload["failures"][0]["code"] == "smoke_failure"
        assert main(["smoke", "--config", str(tmp_path / "config.toml")]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "passed"
