from __future__ import annotations

import json
import os
import sqlite3
import subprocess  # nosec B404
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ELECTRON_COMMAND = ("cmd", "/c", "npx", "--no-install", "electron")
ACTIVE_ACCOUNT_PREFIX = "active_cloud_account."
AGY_TARGET = "agy"


class AntigravityManagerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudAccount:
    id: str
    email: str
    status: str | None
    status_reason: str | None
    is_active: bool
    last_used: int
    proxy_url: str | None


@dataclass(frozen=True)
class ManagerState:
    database_path: Path
    accounts: tuple[CloudAccount, ...]
    active_targets: dict[str, str]
    auto_switch_enabled: bool

    def account_by_id(self, account_id: str | None) -> CloudAccount | None:
        if account_id is None:
            return None
        for account in self.accounts:
            if account.id == account_id:
                return account
        return None

    def account_by_email(self, email: str | None) -> CloudAccount | None:
        if email is None:
            return None
        normalized = email.lower()
        for account in self.accounts:
            if account.email.lower() == normalized:
                return account
        return None

    def active_account(self, target: str) -> CloudAccount | None:
        return self.account_by_id(self.active_targets.get(target))

    def as_dict(self) -> dict[str, Any]:
        return {
            "database_path": str(self.database_path),
            "auto_switch_enabled": self.auto_switch_enabled,
            "active_targets": {
                target: self._target_payload(target, account_id)
                for target, account_id in sorted(self.active_targets.items())
            },
            "accounts": [
                {
                    "id": account.id,
                    "email": account.email,
                    "status": account.status,
                    "status_reason": account.status_reason,
                    "is_active": account.is_active,
                    "last_used": account.last_used,
                    "proxy_url": account.proxy_url,
                }
                for account in self.accounts
            ],
        }

    def _target_payload(self, target: str, account_id: str) -> dict[str, Any]:
        account = self.account_by_id(account_id)
        return {
            "account_id": account_id,
            "email": account.email if account else None,
            "status": account.status if account else None,
            "found": account is not None,
        }


@dataclass(frozen=True)
class SwitchAgyResult:
    changed: bool
    account_id: str
    email: str
    previous_account_id: str | None
    previous_email: str | None
    strategy: str
    refreshed: bool
    credential_written: bool
    dry_run: bool
    helper_output: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "account_id": self.account_id,
            "email": self.email,
            "previous_account_id": self.previous_account_id,
            "previous_email": self.previous_email,
            "strategy": self.strategy,
            "refreshed": self.refreshed,
            "credential_written": self.credential_written,
            "dry_run": self.dry_run,
            "helper_output": self.helper_output,
        }


class AntigravityManagerAdapter:
    def __init__(
        self,
        *,
        database_path: Path | None = None,
        manager_user_data: Path | None = None,
        manager_install_root: Path | None = None,
        electron_command: tuple[str, ...] = DEFAULT_ELECTRON_COMMAND,
        helper_script: Path | None = None,
        helper_payload_dir: Path | None = None,
    ) -> None:
        self.database_path = database_path or default_manager_database_path()
        self.manager_user_data = manager_user_data or default_manager_user_data_path()
        self.manager_install_root = manager_install_root or default_manager_install_root()
        self.electron_command = electron_command
        self.helper_script = helper_script or Path(__file__).with_name(
            "antigravity_manager_helper.js"
        )
        self.helper_payload_dir = helper_payload_dir or Path.cwd() / "runs" / ".manager-helper"

    def load_state(self) -> ManagerState:
        if not self.database_path.exists():
            raise AntigravityManagerError(
                f"Manager cloud accounts DB not found: {self.database_path}"
            )
        with self._connect() as db:
            accounts = tuple(
                CloudAccount(
                    id=row["id"],
                    email=row["email"],
                    status=row["status"],
                    status_reason=row["status_reason"],
                    is_active=bool(row["is_active"]),
                    last_used=int(row["last_used"]),
                    proxy_url=row["proxy_url"],
                )
                for row in db.execute(
                    """
                    select id, email, status, status_reason, is_active, last_used, proxy_url
                    from accounts
                    order by email
                    """
                ).fetchall()
            )
            settings = {
                row["key"]: _decode_setting_value(row["value"])
                for row in db.execute("select key, value from settings").fetchall()
            }
        active_targets = {
            key[len(ACTIVE_ACCOUNT_PREFIX) :]: value
            for key, value in settings.items()
            if key.startswith(ACTIVE_ACCOUNT_PREFIX) and isinstance(value, str)
        }
        return ManagerState(
            database_path=self.database_path,
            accounts=accounts,
            active_targets=active_targets,
            auto_switch_enabled=bool(settings.get("auto_switch_enabled", False)),
        )

    def switch_agy(
        self,
        *,
        account_id: str | None = None,
        email: str | None = None,
        strategy: str = "best",
        dry_run: bool = True,
        refresh: bool = True,
        avoid_current: bool = False,
        timeout_sec: int = 90,
    ) -> SwitchAgyResult:
        state = self.load_state()
        previous = state.active_account(AGY_TARGET)
        account = self._select_account(
            state,
            account_id=account_id,
            email=email,
            strategy=strategy,
            avoid_current=avoid_current,
        )
        row = self._account_secret_row(account.id)
        helper_payload = {
            "action": "write-agy-token",
            "dryRun": dry_run,
            "refresh": refresh,
            "managerUserData": str(self.manager_user_data),
            "managerInstallRoot": str(self.manager_install_root),
            "account": {
                "id": account.id,
                "email": account.email,
                "tokenJson": row["token_json"],
                "proxyUrl": row["proxy_url"],
            },
        }
        helper_output = self._run_helper(helper_payload, timeout_sec=timeout_sec)
        if not dry_run:
            self._mark_agy_active(
                account.id,
                encrypted_token_json=helper_output.get("encryptedTokenJson"),
            )
        return SwitchAgyResult(
            changed=previous is None or previous.id != account.id,
            account_id=account.id,
            email=account.email,
            previous_account_id=previous.id if previous else None,
            previous_email=previous.email if previous else None,
            strategy=strategy,
            refreshed=bool(helper_output.get("refreshed")),
            credential_written=bool(helper_output.get("credentialWritten")),
            dry_run=dry_run,
            helper_output=_sanitize_helper_output(helper_output),
        )

    def _select_account(
        self,
        state: ManagerState,
        *,
        account_id: str | None,
        email: str | None,
        strategy: str,
        avoid_current: bool = False,
    ) -> CloudAccount:
        if account_id and email:
            raise AntigravityManagerError("Pass either account_id or email, not both")
        if account_id:
            account = state.account_by_id(account_id)
            if not account:
                raise AntigravityManagerError(f"Manager cloud account not found: {account_id}")
            return account
        if email:
            account = state.account_by_email(email)
            if not account:
                raise AntigravityManagerError(f"Manager cloud account email not found: {email}")
            return account

        current_id = state.active_targets.get(AGY_TARGET)
        if not avoid_current and strategy == "best":
            current = _healthy(state.active_account(AGY_TARGET))
            if current is not None:
                return current
        strategies = {
            "best": ("ide-active", "classic-active", "global-active", "first-active"),
            "ide-active": ("ide-active",),
            "classic-active": ("classic-active",),
            "global-active": ("global-active",),
            "first-active": ("first-active",),
        }
        selected_strategies = strategies.get(strategy)
        if selected_strategies is None:
            raise AntigravityManagerError(
                "Unknown agy switch strategy. Expected one of: " + ", ".join(sorted(strategies))
            )
        for name in selected_strategies:
            account = self._candidate_for_strategy(state, name, current_id)
            if account is not None:
                return account
        raise AntigravityManagerError(
            f"No active Manager account candidate found for strategy {strategy!r}"
        )

    @staticmethod
    def _candidate_for_strategy(
        state: ManagerState,
        strategy: str,
        current_id: str | None,
    ) -> CloudAccount | None:
        if strategy == "ide-active":
            return _healthy_different(state.active_account("ide"), current_id)
        if strategy == "classic-active":
            return _healthy_different(state.active_account("classic"), current_id)
        if strategy == "global-active":
            candidates = sorted(state.accounts, key=lambda account: account.last_used, reverse=True)
            for account in candidates:
                if account.is_active:
                    return _healthy_different(account, current_id)
            return None
        if strategy == "first-active":
            candidates = sorted(state.accounts, key=lambda account: account.last_used, reverse=True)
            for account in candidates:
                candidate = _healthy_different(account, current_id)
                if candidate is not None:
                    return candidate
            return None
        raise AssertionError(f"Unhandled strategy: {strategy}")

    def _account_secret_row(self, account_id: str) -> sqlite3.Row:
        with self._connect() as db:
            row = db.execute(
                "select id, token_json, proxy_url from accounts where id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise AntigravityManagerError(f"Manager cloud account not found: {account_id}")
        return row

    def _mark_agy_active(
        self,
        account_id: str,
        *,
        encrypted_token_json: str | None,
    ) -> None:
        now = int(time.time())
        with self._connect() as db:
            if encrypted_token_json:
                db.execute(
                    "update accounts set token_json = ?, last_used = ? where id = ?",
                    (encrypted_token_json, now, account_id),
                )
            else:
                db.execute("update accounts set last_used = ? where id = ?", (now, account_id))
            db.execute(
                """
                insert into settings (key, value)
                values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                (f"{ACTIVE_ACCOUNT_PREFIX}{AGY_TARGET}", json.dumps(account_id)),
            )

    def _run_helper(self, payload: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
        if not self.helper_script.exists():
            raise AntigravityManagerError(f"Manager helper script not found: {self.helper_script}")
        self.helper_payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = self.helper_payload_dir / f"payload-{os.getpid()}-{time.time_ns()}.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        command = [*self.electron_command, str(self.helper_script), str(payload_path)]
        try:
            proc = subprocess.run(  # nosec B603
                command,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_sec,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AntigravityManagerError(
                f"Electron command not found for Manager helper: {self.electron_command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AntigravityManagerError(
                f"Manager helper timed out after {timeout_sec} seconds"
            ) from exc
        finally:
            with suppress(OSError):
                payload_path.unlink()
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise AntigravityManagerError(
                f"Manager helper failed with exit code {proc.returncode}: {detail}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AntigravityManagerError(
                f"Manager helper returned invalid JSON: {proc.stdout[:500]}"
            ) from exc
        if not data.get("ok"):
            raise AntigravityManagerError(str(data.get("error") or "Manager helper failed"))
        return data

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def is_agy_quota_failure(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return (
        "resource_exhausted" in normalized
        or "quota exceeded" in normalized
        or "quota has been exceeded" in normalized
        or "quota reached" in normalized
        or ("429" in normalized and "quota" in normalized)
    )


def default_manager_database_path() -> Path:
    return Path.home() / ".antigravity-agent" / "cloud_accounts.db"


def default_manager_user_data_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Antigravity Manager"
    return Path.home() / "AppData" / "Roaming" / "Antigravity Manager"


def default_manager_install_root() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "antigravity_manager"
    return Path.home() / "AppData" / "Local" / "antigravity_manager"


def _decode_setting_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _healthy_different(account: CloudAccount | None, current_id: str | None) -> CloudAccount | None:
    if account is not None and account.id == current_id:
        return None
    return _healthy(account)


def _healthy(account: CloudAccount | None) -> CloudAccount | None:
    if account is None:
        return None
    if account.status not in (None, "active"):
        return None
    return account


def _sanitize_helper_output(output: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in output.items()
        if key not in {"encryptedTokenJson"} and not key.lower().endswith("token")
    }
