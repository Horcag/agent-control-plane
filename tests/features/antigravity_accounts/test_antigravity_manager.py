from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.antigravity_accounts.lib.antigravity_manager import (
    AntigravityManagerAdapter,
    AntigravityManagerError,
    is_agy_quota_failure,
)


class AntigravityManagerAdapterTest(unittest.TestCase):
    def test_load_state_reports_active_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = _manager_db(Path(temp))
            _insert_account(db_path, "agy-id", "old@example.test", is_active=False)
            _insert_account(db_path, "ide-id", "fresh@example.test", is_active=True)
            _set_active_target(db_path, "agy", "agy-id")
            _set_active_target(db_path, "ide", "ide-id")

            state = AntigravityManagerAdapter(database_path=db_path).load_state()

            agy_account = state.active_account("agy")
            ide_account = state.active_account("ide")
            assert agy_account is not None
            assert ide_account is not None
            self.assertEqual(agy_account.email, "old@example.test")
            self.assertEqual(ide_account.email, "fresh@example.test")
            self.assertTrue(state.as_dict()["active_targets"]["agy"]["found"])

    def test_best_strategy_prefers_ide_active_account_different_from_agy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = _manager_db(Path(temp))
            _insert_account(db_path, "agy-id", "old@example.test", is_active=False)
            _insert_account(db_path, "ide-id", "fresh@example.test", is_active=True)
            _set_active_target(db_path, "agy", "agy-id")
            _set_active_target(db_path, "ide", "ide-id")

            adapter = AntigravityManagerAdapter(database_path=db_path)
            account = adapter._select_account(
                adapter.load_state(),
                account_id=None,
                email=None,
                strategy="best",
                avoid_current=True,
            )

            self.assertEqual(account.email, "fresh@example.test")

    def test_best_strategy_keeps_current_agy_unless_avoiding_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = _manager_db(Path(temp))
            _insert_account(db_path, "agy-id", "current@example.test", is_active=True)
            _insert_account(db_path, "ide-id", "fresh@example.test", is_active=False)
            _set_active_target(db_path, "agy", "agy-id")
            _set_active_target(db_path, "ide", "ide-id")

            adapter = AntigravityManagerAdapter(database_path=db_path)
            state = adapter.load_state()
            normal = adapter._select_account(
                state,
                account_id=None,
                email=None,
                strategy="best",
            )
            recovery = adapter._select_account(
                state,
                account_id=None,
                email=None,
                strategy="best",
                avoid_current=True,
            )

            self.assertEqual(normal.email, "current@example.test")
            self.assertEqual(recovery.email, "fresh@example.test")

    def test_unknown_strategy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = _manager_db(Path(temp))
            _insert_account(db_path, "agy-id", "old@example.test")
            adapter = AntigravityManagerAdapter(database_path=db_path)

            with self.assertRaises(AntigravityManagerError):
                adapter._select_account(
                    adapter.load_state(),
                    account_id=None,
                    email=None,
                    strategy="random",
                )

    def test_helper_does_not_embed_oauth_credentials(self) -> None:
        helper_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "agent_control_plane"
            / "features"
            / "antigravity_accounts"
            / "lib"
            / "antigravity_manager_helper.js"
        )
        helper_text = helper_path.read_text(encoding="utf-8")

        banned_markers = (
            "GOC" + "SPX-",
            "apps.google" + "usercontent.com",
            "107100" + "6060591",
        )
        for marker in banned_markers:
            self.assertNotIn(marker, helper_text)

    def test_helper_accepts_manager_prerelease_install_directories(self) -> None:
        helper_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "agent_control_plane"
            / "features"
            / "antigravity_accounts"
            / "lib"
            / "antigravity_manager_helper.js"
        )

        helper_text = helper_path.read_text(encoding="utf-8")

        self.assertIn(r"^app-\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$", helper_text)

    def test_quota_failure_detection_is_specific(self) -> None:
        self.assertTrue(is_agy_quota_failure("RESOURCE_EXHAUSTED 429 quota exceeded"))
        self.assertTrue(is_agy_quota_failure("status=429; quota has been exceeded"))
        self.assertTrue(
            is_agy_quota_failure(
                "Individual quota reached. Please upgrade your subscription. Resets in 17m13s."
            )
        )
        self.assertFalse(is_agy_quota_failure("agy exited without writing result.md"))


def _manager_db(root: Path) -> Path:
    db_path = root / "cloud_accounts.db"
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            create table accounts (
                id text primary key,
                provider text not null,
                email text not null,
                name text,
                avatar_url text,
                token_json text not null,
                quota_json text,
                device_profile_json text,
                device_history_json text,
                created_at integer not null,
                last_used integer not null,
                status text,
                status_reason text,
                is_active integer,
                proxy_url text
            );
            create table settings (
                key text primary key,
                value text not null
            );
            """
        )
        db.commit()
    finally:
        db.close()
    return db_path


def _insert_account(
    db_path: Path,
    account_id: str,
    email: str,
    *,
    is_active: bool = False,
    status: str = "active",
) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """
            insert into accounts (
                id, provider, email, token_json, created_at, last_used,
                status, is_active
            )
            values (?, 'google', ?, '{}', 1, 2, ?, ?)
            """,
            (account_id, email, status, int(is_active)),
        )
        db.commit()
    finally:
        db.close()


def _set_active_target(db_path: Path, target: str, account_id: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "insert into settings (key, value) values (?, ?)",
            (f"active_cloud_account.{target}", json.dumps(account_id)),
        )
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    unittest.main()
