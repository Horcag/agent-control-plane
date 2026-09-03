"""Opt-in, CLI-only Manager integration. Credentials never leave the private helper pipe."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess  # nosec B404
import tempfile
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_control_plane.features.antigravity_accounts.lib.cli_token_store import (
    manager_switch_lock,
    write_cli_tokens,
)

CONFIG_ENV = "AGENT_CONTROL_PLANE_MANAGER_CLI_CONFIG"


def configured_cli_switcher() -> ManagerCliSwitcher | None:
    path = Path(
        os.environ.get(
            CONFIG_ENV, str(Path.home() / ".config/agent-control-plane/manager-cli.json")
        )
    )
    if not path.exists():
        return None
    return ManagerCliSwitcher(json.loads(path.read_text(encoding="utf-8")))


def credential_fingerprint(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["token"]["refresh_token"]
        if not isinstance(value, str) or not value:
            return None
        return hashlib.sha256(value.encode()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def select_cli_account(
    accounts: list[dict[str, Any]], *, model: str, excluded: set[str]
) -> dict[str, Any]:
    candidates = []
    for account in accounts:
        if (
            account["id"] in excluded
            or account.get("status") not in (None, "active")
            or account.get("forbidden")
        ):
            continue
        quota = account.get("models", {}).get(model, {})
        remaining = quota.get("percentage")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        reset_passed = False
        if remaining == 0:
            with suppress(KeyError, AttributeError, ValueError, TypeError):
                reset_passed = (
                    datetime.fromisoformat(quota["resetTime"].replace("Z", "+00:00")).timestamp()
                    <= time.time()
                )
        if math.isfinite(remaining) and (0 < remaining <= 100 or (remaining == 0 and reset_passed)):
            # A passed reset permits a bounded provider probe, not a claim of refreshed quota.
            candidates.append(account)
    if not candidates:
        raise ValueError(
            f"No available CLI account for model {model}; cached quota may need refresh"
        )
    return sorted(candidates, key=lambda a: (-a["models"][model]["percentage"], a["id"]))[0]


class ManagerCliSwitcher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.database = Path(config["database_path"])
        self.user_data = Path(config["manager_user_data"])
        self.targets = tuple(Path(p) for p in config["token_paths"])
        self.command = tuple(config["electron_command"])
        if (
            not self.targets
            or not self.command
            or not all(isinstance(p, str) and p for p in self.command)
        ):
            raise ValueError("Manager CLI requires explicit token_paths and electron_command")
        if not all(p.is_absolute() for p in (self.database, self.user_data, *self.targets)):
            raise ValueError("Manager CLI paths must be absolute")
        self.auto_switch = config.get("auto_switch_on_quota") is True

    def _helper_path(self, path: Path) -> str:
        if self.config.get("helper_windows") and os.name != "nt":
            result = subprocess.run(
                ["wslpath", "-w", str(path)], capture_output=True, text=True, check=True
            )  # nosec B603 B607
            return result.stdout.strip()
        return str(path)

    def _helper(self, payload: dict[str, Any]) -> dict[str, Any]:
        script = Path(__file__).with_name("antigravity_manager_helper.js")
        payload["managerUserData"] = self._helper_path(self.user_data)
        # Input contains Manager-encrypted records only. A file avoids Electron GUI stdin loss
        # across WSL interop. Plain credentials return solely through the private output pipe.
        try:
            with tempfile.TemporaryDirectory(prefix="acp-cli-helper-") as temp:
                payload_path = Path(temp) / "input.json"
                with payload_path.open("x", encoding="utf-8") as stream:
                    os.chmod(payload_path, 0o600)
                    json.dump(payload, stream)
                result = subprocess.run(  # nosec B603
                    [*self.command, self._helper_path(script), self._helper_path(payload_path)],
                    stdin=subprocess.DEVNULL,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Manager CLI helper unavailable or timed out") from exc
        if result.returncode:
            raise ValueError(f"Manager CLI helper failed (exit {result.returncode})")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise ValueError("Manager CLI helper returned invalid response") from None
        if not isinstance(data, dict) or not data.get("ok"):
            # Error code is an allowlist, never arbitrary provider/helper output.
            code = data.get("code") if isinstance(data, dict) else None
            if code == "TOKEN_EXPIRED":
                raise ValueError(
                    "Manager account token expired; refresh it in Manager before switching"
                )
            raise ValueError("Manager CLI helper could not decrypt or prepare the account")
        return data

    def _rows(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in db.execute(
                    "select id, email, status, token_json, quota_json from accounts"
                )
            ]

    def _manager_auto_switch_enabled(self) -> bool:
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "select value from settings where key = 'auto_switch_enabled'"
            ).fetchone()
        return bool(json.loads(row[0])) if row else False

    def _inspect(self) -> list[dict[str, Any]]:
        return self._helper({"action": "inspect-cli-accounts", "accounts": self._rows()})[
            "accounts"
        ]

    def _current(self, accounts: list[dict[str, Any]]) -> str | None:
        fingerprints = {credential_fingerprint(p) for p in self.targets}
        if len(fingerprints) != 1 or None in fingerprints:
            return None
        return next((a["id"] for a in accounts if a["fingerprint"] in fingerprints), None)

    def current_account_id(self) -> str | None:
        return self._current(self._inspect())

    def prepare_attempt(self, model: str | None) -> str:
        if not model:
            raise ValueError(
                "An explicit AGY model is required for automatic CLI account preparation"
            )
        result = self.switch(model=model, dry_run=False, ensure_current=True)
        return str(result["account_id"])

    def _desired_account(self) -> str | None:
        path = self.database.with_name("acp-cli-selected.json")
        return json.loads(path.read_text(encoding="utf-8"))["account_id"] if path.exists() else None

    def _remember_selected(self, account_id: str) -> None:
        path = self.database.with_name("acp-cli-selected.json")
        fd, name = tempfile.mkstemp(prefix=".acp-selected-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"account_id": account_id}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def snapshot(self, model: str | None = None) -> dict[str, Any]:
        accounts = self._inspect()
        current = self._current(accounts)
        public = []
        for account in accounts:
            item = {k: v for k, v in account.items() if k != "fingerprint"}
            if model:
                item["models"] = {model: item["models"][model]} if model in item["models"] else {}
            public.append(item)
        return {
            "current_account_id": current,
            "selected_account_id": self._desired_account(),
            "targets_agree": current is not None,
            "auto_switch_on_quota": self.auto_switch,
            "manager_auto_switch_enabled": self._manager_auto_switch_enabled(),
            "quota_source": "manager_cache",
            "quota_freshness": "unknown",
            "cooldown_account_ids": sorted(self._cooldowns(accounts, model, None, persist=False))
            if model
            else [],
            "accounts": public,
            "token_paths": [str(p) for p in self.targets],
        }

    def _cooldowns(
        self,
        accounts: list[dict[str, Any]],
        model: str,
        failed_account_id: str | None,
        *,
        persist: bool,
    ) -> set[str]:
        # Atomic JSON replacement under the switch lock avoids SQLite locking across Win32/WSL.
        path = self.database.with_name("acp-cli-quota.json")
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        now = time.time()
        records = [r for r in records if r["until"] > now]
        if failed_account_id and persist:
            account = next((a for a in accounts if a["id"] == failed_account_id), {})
            reset = account.get("models", {}).get(model, {}).get("resetTime")
            until = now + 300
            try:
                parsed = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
                if parsed > now:
                    until = parsed
            except (AttributeError, TypeError, ValueError):
                pass
            records = [
                r for r in records if (r["account_id"], r["model"]) != (failed_account_id, model)
            ]
            records.append({"account_id": failed_account_id, "model": model, "until": until})
            fd, name = tempfile.mkstemp(prefix=".acp-quota-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(records, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
        return {r["account_id"] for r in records if r["model"] == model}

    def switch(
        self,
        *,
        model: str | None = None,
        account_id: str | None = None,
        dry_run: bool = True,
        excluded: set[str] | None = None,
        failed_account_id: str | None = None,
        ensure_current: bool = False,
    ) -> dict[str, Any]:
        if ensure_current and dry_run:
            raise ValueError("Attempt preparation requires apply mode")
        with manager_switch_lock(self.database):
            if not dry_run and self._manager_auto_switch_enabled():
                raise ValueError(
                    "Disable Manager auto-switch before enabling the CLI switcher; both write the same CLI files"
                )
            accounts = self._inspect()
            previous = self._current(accounts)
            avoid = set(excluded or ())
            if failed_account_id:
                avoid.add(failed_account_id)
            if model:
                avoid.update(
                    self._cooldowns(accounts, model, failed_account_id, persist=not dry_run)
                )
            if ensure_current:
                desired = self._desired_account() or previous
                eligible = [a for a in accounts if a["id"] == desired]
                try:
                    chosen_current = select_cli_account(eligible, model=model or "", excluded=avoid)
                    account_id = chosen_current["id"]
                except ValueError:
                    account_id = None
                if account_id and previous == account_id:
                    self._remember_selected(account_id)
                    return {
                        "account_id": account_id,
                        "verified": True,
                        "changed": False,
                        "dry_run": False,
                    }
            # A peer has already switched the shared CLI files: reuse its verified account.
            if (
                failed_account_id
                and previous
                and previous != failed_account_id
                and previous == self._desired_account()
                and previous not in avoid
            ):
                return {
                    "changed": False,
                    "verified": True,
                    "account_id": previous,
                    "previous_account_id": failed_account_id,
                    "reused_peer_switch": True,
                    "dry_run": dry_run,
                }
            if account_id:
                chosen = next((a for a in accounts if a["id"] == account_id), None)
                if (
                    chosen is None
                    or chosen.get("status") not in (None, "active")
                    or chosen.get("forbidden")
                ):
                    raise ValueError("Requested CLI account is missing or unavailable")
            else:
                if not model:
                    raise ValueError("Pass model for quota selection or an explicit account_id")
                if previous:
                    avoid.add(previous)
                chosen = select_cli_account(accounts, model=model, excluded=avoid)
            output = {
                "changed": chosen["id"] != previous,
                "account_id": chosen["id"],
                "previous_account_id": previous,
                "dry_run": dry_run,
                "verified": False,
            }
            if dry_run:
                return output
            before = [credential_fingerprint(p) for p in self.targets]
            row = next((r for r in self._rows() if r["id"] == chosen["id"]), None)
            if row is None:
                raise ValueError("Selected CLI account disappeared")
            prepared = self._helper({"action": "prepare-cli-token", "account": row})
            if before != [credential_fingerprint(p) for p in self.targets]:
                raise ValueError("CLI identity changed concurrently; inspect and retry")
            paths = write_cli_tokens(self.targets, prepared["credential"])
            if self._current(self._inspect()) != chosen["id"]:
                raise ValueError("CLI identity read-back failed; inspect before retrying")
            # Never mutate Manager's active target/keyring: it owns IDE state; files prove CLI state.
            self._remember_selected(chosen["id"])
            return {**output, "verified": True, "written_paths": list(paths)}
