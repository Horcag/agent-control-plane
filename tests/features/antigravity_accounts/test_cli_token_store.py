from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

import agent_control_plane.features.antigravity_accounts.lib.cli_token_store as cli_token_store
from agent_control_plane.features.antigravity_accounts.lib.cli_token_store import (
    CliTokenStoreError,
    manager_switch_lock,
    write_cli_tokens,
)


def test_write_cli_tokens_writes_all_targets_and_verifies_posix_mode(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"

    verified = write_cli_tokens((left, right), _payload())

    assert verified == (str(left), str(right))
    assert left.read_text(encoding="utf-8") == _payload()
    assert right.read_text(encoding="utf-8") == _payload()
    if os.name == "posix":
        assert stat.S_IMODE(left.stat().st_mode) == 0o600
        assert stat.S_IMODE(right.stat().st_mode) == 0o600


def test_write_cli_tokens_overwrite_uses_private_posix_mode(tmp_path: Path) -> None:
    target = tmp_path / "credential.json"
    target.write_text("old", encoding="utf-8")
    if os.name == "posix":
        target.chmod(0o644)

    write_cli_tokens((target,), _payload())

    assert target.read_text(encoding="utf-8") == _payload()
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_cli_tokens_preflights_all_paths_before_mutating(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    first.write_text("original", encoding="utf-8")
    missing_parent = tmp_path / "missing" / "second.json"

    with pytest.raises(CliTokenStoreError) as excinfo:
        write_cli_tokens((first, missing_parent), _payload())

    assert first.read_text(encoding="utf-8") == "original"
    assert "missing" in str(excinfo.value).lower()
    assert "ACCESS_SECRET" not in str(excinfo.value)


def test_write_cli_tokens_rejects_symlink_target_before_mutation(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    first = tmp_path / "first.json"
    real.write_text("real", encoding="utf-8")
    first.write_text("first", encoding="utf-8")
    link.symlink_to(real)

    with pytest.raises(CliTokenStoreError) as excinfo:
        write_cli_tokens((first, link), _payload())

    assert first.read_text(encoding="utf-8") == "first"
    assert "symlink" in str(excinfo.value).lower()
    assert real.read_text(encoding="utf-8") == "real"


def test_write_cli_tokens_validates_required_credential_shape_without_secret_leak(
    tmp_path: Path,
) -> None:
    target = tmp_path / "credential.json"
    payload = _payload(access_token="ACCESS_SECRET").replace('"refresh_token":', '"missing":')

    with pytest.raises(CliTokenStoreError) as excinfo:
        write_cli_tokens((target,), payload)

    message = str(excinfo.value)
    assert "refresh_token" in message
    assert "ACCESS_SECRET" not in message
    assert not target.exists()


def test_write_cli_tokens_rolls_back_prior_target_when_later_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first-original", encoding="utf-8")
    second.write_text("second-original", encoding="utf-8")
    original_replace = os.replace
    calls = 0

    def flaky_replace(
        src: str | bytes | os.PathLike[str], dst: str | bytes | os.PathLike[str]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replace failure")
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    with pytest.raises(CliTokenStoreError) as excinfo:
        write_cli_tokens((first, second), _payload())

    assert first.read_text(encoding="utf-8") == "first-original"
    assert second.read_text(encoding="utf-8") == "second-original"
    assert "ACCESS_SECRET" not in str(excinfo.value)


def test_write_cli_tokens_refuses_pre_replace_drift_without_clobbering_foreign_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "credential.json"
    target.write_text("original", encoding="utf-8")
    original_write_tempfile = cli_token_store._write_tempfile

    def drifting_tempfile(parent: Path, payload_bytes: bytes) -> Path:
        temp_path = original_write_tempfile(parent, payload_bytes)
        target.write_text("foreign-edit", encoding="utf-8")
        return temp_path

    monkeypatch.setattr(cli_token_store, "_write_tempfile", drifting_tempfile)

    with pytest.raises(CliTokenStoreError) as excinfo:
        write_cli_tokens((target,), _payload())

    assert target.read_text(encoding="utf-8") == "foreign-edit"
    assert "changed before replace" in str(excinfo.value)
    assert "ACCESS_SECRET" not in str(excinfo.value)


def test_manager_switch_lock_uses_directory_and_rejects_competing_writer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "manager.db"
    lock_dir = tmp_path / "acp-cli-switch.lock"
    contender_result: list[str] = []
    ready = threading.Event()
    release = threading.Event()

    def contender() -> None:
        ready.wait(timeout=2)
        try:
            with manager_switch_lock(database, timeout_sec=0.05):
                contender_result.append("acquired")
        except CliTokenStoreError as exc:
            contender_result.append(str(exc))
        finally:
            release.set()

    thread = threading.Thread(target=contender)
    thread.start()
    with manager_switch_lock(database, timeout_sec=1):
        ready.set()
        assert release.wait(timeout=2)
    thread.join(timeout=2)

    assert not lock_dir.exists()
    assert contender_result
    assert "lock" in contender_result[0].lower()


def test_manager_switch_lock_times_out_on_unknown_existing_lock(tmp_path: Path) -> None:
    database = tmp_path / "manager.db"
    lock_dir = tmp_path / "acp-cli-switch.lock"
    lock_dir.mkdir()

    with (
        pytest.raises(CliTokenStoreError) as excinfo,
        manager_switch_lock(database, timeout_sec=0.01),
    ):
        raise AssertionError("lock should not be acquired")

    assert lock_dir.exists()
    message = str(excinfo.value).lower()
    assert "stale lock" in message
    assert str(lock_dir) in str(excinfo.value)


def _payload(*, access_token: str = "ACCESS_SECRET") -> str:
    return (
        "{"
        f'"token": {{"access_token": "{access_token}", "refresh_token": "REFRESH_SECRET", '
        '"expiry": "2026-09-03T12:00:00.000Z"}, '
        '"auth_method": "consumer"'
        "}"
    )
