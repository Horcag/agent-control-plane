from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent_control_plane.app.runtime.cli import main
from agent_control_plane.app.runtime.mcp_collections import compact_slots_page


@pytest.mark.parametrize("action", ["claim-direct", "release-direct"])
def test_direct_session_cli_routes_exact_reviewed_payload(tmp_path: Path, action: str) -> None:
    payload = {"slot": "app-1", "chatId": "owner", "path": str(tmp_path)}
    payload_path = tmp_path / "claim.json"
    payload_path.write_text(json.dumps(payload))
    control = Mock()
    control.claim_direct_session.return_value = {"status": "direct_owned"}
    control.release_direct_session.return_value = {"status": "available"}
    with patch(
        "agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path",
        return_value=control,
    ):
        assert main(["slots", action, "app-1", "--claim-file", str(payload_path)]) == 0
    if action == "claim-direct":
        control.claim_direct_session.assert_called_once_with("app-1", claim=payload)
        control.release_direct_session.assert_not_called()
    else:
        control.release_direct_session.assert_called_once_with("app-1", expected_claim=payload)
        control.claim_direct_session.assert_not_called()


def test_direct_session_cli_missing_payload_is_reported(tmp_path: Path) -> None:
    with patch("agent_control_plane.app.runtime.cli.AgentControlPlane.from_config_path") as control:
        assert (
            main(["slots", "release-direct", "app-1", "--claim-file", str(tmp_path / "absent")])
            == 2
        )
    control.return_value.release_direct_session.assert_not_called()


def test_compact_mcp_inventory_preserves_bounded_direct_owner_evidence() -> None:
    result = compact_slots_page(
        [
            {
                "name": "app-1",
                "status": "direct_owned",
                "active_job_id": None,
                "direct_owner": {
                    "chatId": "owner",
                    "task": "task",
                    "branch": "branch",
                    "claim_path": "/claim",
                },
                "live_process_cwds": list(range(30)),
            }
        ],
        offset=0,
        limit=20,
    )
    row = result["items"][0]
    assert row["direct_owner"]["chatId"] == "owner"
    assert row["status"] == "direct_owned"
    assert len(row["live_process_cwds"]) == 16
    assert row["live_process_cwds_total_count"] == 30
