from pathlib import Path

import pytest

from agent_control_plane.features.antigravity_accounts.lib.manager_cli import (
    credential_fingerprint,
    select_cli_account,
)


def account(key, remaining=50, status="active"):
    return {
        "id": key,
        "status": status,
        "models": {"gemini-test": {"percentage": remaining}},
        "forbidden": False,
    }


def test_selects_requested_model_and_excludes_exhausted_accounts():
    rows = [
        account("old", 100),
        account("empty", 0),
        account("next", 50),
        account("bad", 90, "expired"),
    ]
    assert select_cli_account(rows, model="gemini-test", excluded={"old"})["id"] == "next"


def test_unknown_model_never_means_available():
    with pytest.raises(ValueError, match="available"):
        select_cli_account([account("one")], model="unknown", excluded=set())


def test_exhausted_pool_fails_without_cycling():
    with pytest.raises(ValueError, match="available"):
        select_cli_account([account("one")], model="gemini-test", excluded={"one"})


def test_forbidden_account_is_not_selected():
    row = account("one")
    row["forbidden"] = True
    with pytest.raises(ValueError, match="available"):
        select_cli_account([row], model="gemini-test", excluded=set())


def test_identity_uses_refresh_token_not_access_token_expiry(tmp_path: Path):
    p = tmp_path / "token"
    p.write_text('{"token":{"refresh_token":"identity", "access_token":"one"}}')
    before = credential_fingerprint(p)
    p.write_text('{"token":{"refresh_token":"identity", "access_token":"two"}}')
    assert credential_fingerprint(p) == before


@pytest.fixture
def switcher(tmp_path, monkeypatch):
    import json

    from agent_control_plane.features.antigravity_accounts.lib.manager_cli import ManagerCliSwitcher

    paths = [tmp_path / "windows-token", tmp_path / "wsl-token"]
    old = json.dumps(
        {
            "token": {
                "access_token": "a",
                "refresh_token": "old",
                "expiry": "2026-09-04T00:00:00Z",
            },
            "auth_method": "consumer",
        }
    )
    for p in paths:
        p.write_text(old)
    instance = ManagerCliSwitcher(
        {
            "database_path": str(tmp_path / "manager.db"),
            "manager_user_data": str(tmp_path),
            "token_paths": [str(p) for p in paths],
            "electron_command": ["fake-electron"],
        }
    )
    import hashlib

    rows = [account("old"), account("next", 90)]
    for row in rows:
        row["fingerprint"] = hashlib.sha256(row["id"].encode()).hexdigest()
        row["email"] = row["id"] + "@example.test"
    monkeypatch.setattr(instance, "_inspect", lambda: rows)
    monkeypatch.setattr(instance, "_manager_auto_switch_enabled", lambda: False)
    monkeypatch.setattr(instance, "_rows", lambda: rows)
    new = old.replace('"old"', '"next"')
    monkeypatch.setattr(instance, "_helper", lambda payload: {"ok": True, "credential": new})
    return instance


def test_switch_reads_back_both_targets_without_mutating_manager_db(switcher):
    result = switcher.switch(model="gemini-test", dry_run=False)
    assert result["account_id"] == "next"
    assert result["verified"] is True
    assert switcher.current_account_id() == "next"
    assert not switcher.database.exists()


def test_dry_run_does_not_write_credentials(switcher):
    before = [p.read_bytes() for p in switcher.targets]
    result = switcher.switch(model="gemini-test")
    assert result["account_id"] == "next" and not result["verified"]
    assert [p.read_bytes() for p in switcher.targets] == before


def test_snapshot_never_exposes_refresh_fingerprint_or_credentials(switcher):
    import json

    snapshot = switcher.snapshot("gemini-test")
    assert snapshot["current_account_id"] == "old"
    assert "fingerprint" not in json.dumps(snapshot)
    assert "refresh_token" not in json.dumps(snapshot)


def test_peer_switch_is_reused_without_rewriting_token(switcher, monkeypatch):
    switcher.switch(model="gemini-test", dry_run=False)
    monkeypatch.setattr(
        switcher, "_helper", lambda p: pytest.fail("must not prepare another token")
    )
    result = switcher.switch(model="gemini-test", failed_account_id="old", dry_run=False)
    assert result["account_id"] == "next" and result["reused_peer_switch"]


def test_failed_accounts_are_shared_across_switcher_calls(switcher):
    switcher.switch(model="gemini-test", failed_account_id="old", dry_run=False)
    with pytest.raises(ValueError, match="No available"):
        switcher.switch(model="gemini-test", failed_account_id="next", dry_run=False)


def test_concurrent_identity_change_before_write_fails_closed(switcher, monkeypatch):
    def helper(payload):
        switcher.targets[0].write_text('{"token":{"refresh_token":"foreign"}}')
        return {"credential": "sensitive"}

    monkeypatch.setattr(switcher, "_helper", helper)
    before_second = switcher.targets[1].read_bytes()
    with pytest.raises(ValueError, match="concurrently"):
        switcher.switch(model="gemini-test", dry_run=False)
    assert switcher.targets[1].read_bytes() == before_second


def test_apply_refuses_competing_manager_auto_switch(switcher, monkeypatch):
    monkeypatch.setattr(switcher, "_manager_auto_switch_enabled", lambda: True)
    before = [p.read_bytes() for p in switcher.targets]
    with pytest.raises(ValueError, match="Disable Manager"):
        switcher.switch(model="gemini-test", dry_run=False)
    assert [p.read_bytes() for p in switcher.targets] == before


def test_prelaunch_repairs_old_cli_process_overwriting_only_one_target(switcher):
    old = switcher.targets[0].read_bytes()
    switcher.switch(model="gemini-test", dry_run=False)
    switcher.targets[0].write_bytes(old)
    assert switcher.current_account_id() is None
    assert switcher.prepare_attempt("gemini-test") == "next"
    assert switcher.current_account_id() == "next"


def test_prelaunch_retains_healthy_account_without_rotation(switcher):
    assert switcher.prepare_attempt("gemini-test") == "old"
    assert switcher.prepare_attempt("gemini-test") == "old"


def test_passed_reset_allows_bounded_probe_without_inventing_fresh_percentage():
    row = account("reset", 0)
    row["models"]["gemini-test"]["resetTime"] = "2020-01-01T00:00:00Z"
    assert (
        select_cli_account([row], model="gemini-test", excluded=set())["models"]["gemini-test"][
            "percentage"
        ]
        == 0
    )
    with pytest.raises(ValueError, match="No available"):
        select_cli_account([row], model="gemini-test", excluded={"reset"})
