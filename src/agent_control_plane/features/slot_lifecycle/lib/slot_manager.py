from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.entities.slot import SlotRecord, SlotStore, SlotStoreError
from agent_control_plane.features.slot_lifecycle.lib.ide_modules import (
    ensure_slot_ide_module,
    ensure_slot_ide_vcs_mappings,
    ensure_slot_root_ide_module,
    remove_slot_ide_module,
    unload_slot_ide_module,
    unload_slot_root_ide_module,
)
from agent_control_plane.features.slot_lifecycle.lib.slot_prepare import (
    SlotPrepareError,
    prepare_workspace_slot,
)
from agent_control_plane.features.slot_lifecycle.lib.worktree_manager import (
    WorktreeError,
    WorktreeSpec,
    create_worktree,
    remove_worktree,
)
from agent_control_plane.shared.config import ControlConfig, SlotConfig, SlotPrepareCommand
from agent_control_plane.shared.git_tools import (
    GitError,
    is_git_workspace,
    run_git,
    workspace_state,
)
from agent_control_plane.shared.path_rules import is_same_or_child


class SlotError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlotStatus:
    name: str
    route: str
    path: Path
    status: str
    configured: bool
    exists: bool
    is_git_workspace: bool
    branch: str | None
    dirty: str
    active_job_id: str | None
    use_count: int
    last_used_at: str | None
    note: str | None
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "route": self.route,
            "path": str(self.path),
            "status": self.status,
            "configured": self.configured,
            "exists": self.exists,
            "is_git_workspace": self.is_git_workspace,
            "branch": self.branch,
            "dirty": self.dirty,
            "active_job_id": self.active_job_id,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at,
            "note": self.note,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class CleanupDecision:
    name: str
    route: str
    path: Path
    action: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "route": self.route,
            "path": str(self.path),
            "action": self.action,
            "reason": self.reason,
        }


class SlotManager:
    def __init__(self, config: ControlConfig, store: SlotStore) -> None:
        self._config = config
        self._store = store

    def sync_configured_slots(self) -> list[SlotStatus]:
        for slot in self._config.slots.values():
            self._ensure_slot_path_allowed(slot.path)
            self._store.register_slot(
                slot.name,
                slot.route,
                slot.path,
                note="configured in workspaces.toml",
            )
        return self.list_slots(sync=False)

    def list_slots(
        self,
        *,
        sync: bool = True,
        include_deleted: bool = False,
    ) -> list[SlotStatus]:
        if sync:
            self.sync_configured_slots()
        records = {record.name: record for record in self._store.list_slots()}
        statuses: list[SlotStatus] = []
        for name, record in sorted(records.items()):
            if record.status == "deleted" and not include_deleted:
                continue
            statuses.append(self.inspect_slot(name, sync=False))
        return statuses

    def inspect_slot(self, name: str, *, sync: bool = True) -> SlotStatus:
        if sync:
            self.sync_configured_slots()
        configured = self._config.slots.get(name)
        record = self._store.get_slot(name)
        if record is not None:
            route = record.route
            path = record.path
            status = record.status
            active_job_id = record.active_job_id
            use_count = record.use_count
            last_used_at = record.last_used_at
            note = record.note
        elif configured is not None:
            route = configured.route
            path = configured.path
            status = "unregistered"
            active_job_id = None
            use_count = 0
            last_used_at = None
            note = None
        else:
            raise SlotError(f"Unknown slot: {name}")

        exists = path.exists()
        is_git = False
        branch: str | None = None
        dirty = ""
        problems: list[str] = []

        if not is_same_or_child(path, self._config.slot_root):
            problems.append(f"path is outside slot_root: {self._config.slot_root}")
        if route not in self._config.routes:
            problems.append(f"unknown route: {route}")
        if status == "deleted":
            problems.append("slot is marked deleted")
        if not exists:
            problems.append("path does not exist")
        elif not path.is_dir():
            problems.append("path is not a directory")
        else:
            is_git = is_git_workspace(path)
            if not is_git:
                problems.append("path is not a git workspace")
            else:
                try:
                    state = workspace_state(path)
                    branch = state.branch
                    dirty = state.porcelain
                except GitError as exc:
                    problems.append(f"git status failed: {exc}")

        display_status = status
        if dirty and status == "available" and active_job_id is None:
            display_status = "dirty"

        return SlotStatus(
            name=name,
            route=route,
            path=path,
            status=display_status,
            configured=configured is not None,
            exists=exists,
            is_git_workspace=is_git,
            branch=branch,
            dirty=dirty,
            active_job_id=active_job_id,
            use_count=use_count,
            last_used_at=last_used_at,
            note=note,
            problems=tuple(problems),
        )

    def create_slot(
        self,
        name: str,
        *,
        route: str | None = None,
        branch: str | None = None,
        start_point: str | None = None,
    ) -> SlotStatus:
        configured = self._config.slots.get(name)
        slot_route: str
        if configured:
            slot_route = configured.route
            path = configured.path
        else:
            if route is None:
                raise SlotError("Dynamic slot creation requires --route")
            if route not in self._config.routes:
                raise SlotError(f"Unknown route: {route}")
            slot_route = route
            path = self._config.slot_root / _safe_slot_name(name)

        self._ensure_slot_path_allowed(path)
        route_key: str = str(slot_route)
        route_config = self._config.routes[route_key]
        worktree_base = route_config.worktree_base
        slot_branch = branch or f"slot/{_safe_slot_name(name)}"
        slot_start_point = start_point or f"origin/{route_config.required_branch}"

        if path.exists() and is_git_workspace(path):
            record = self._store.register_slot(
                name,
                route_key,
                path,
                note="existing git workspace",
            )
            self._store.mark_available(record.name, note="existing git workspace")
            return self.inspect_slot(name)

        try:
            if _branch_exists(worktree_base, slot_branch):
                run_git(worktree_base, "worktree", "add", str(path), slot_branch)
            else:
                create_worktree(
                    WorktreeSpec(
                        base_repo=worktree_base,
                        worktree_root=self._config.slot_root,
                        worktree_path=path,
                        branch=slot_branch,
                        start_point=slot_start_point,
                    )
                )
        except (GitError, WorktreeError) as exc:
            raise SlotError(f"Could not create slot {name}: {exc}") from exc

        self._store.register_slot(name, route_key, path, note="created")
        self._store.mark_available(name, note="created")
        return self.inspect_slot(name)

    def delete_slot(self, name: str, *, force: bool = False) -> SlotStatus:
        status = self.inspect_slot(name)
        if status.active_job_id and not force:
            raise SlotError(f"Slot {name} is active for job {status.active_job_id}")
        self._ensure_slot_path_allowed(status.path)

        if status.exists and status.is_git_workspace:
            if status.dirty and not force:
                raise SlotError(f"Refusing to delete dirty slot {name}")
            try:
                remove_worktree(
                    self._route_worktree_base(status.route),
                    status.path,
                    allow_dirty=force,
                )
            except WorktreeError as exc:
                raise SlotError(f"Could not delete slot {name}: {exc}") from exc
        elif status.exists:
            if not status.path.is_dir():
                raise SlotError(f"Refusing to delete non-directory slot path: {status.path}")
            if any(status.path.iterdir()):
                raise SlotError(f"Refusing to delete non-git non-empty slot path: {status.path}")
            status.path.rmdir()

        self._store.mark_deleted(name, note="deleted")
        return self.inspect_slot(name)

    def checkout_slot(
        self,
        name: str,
        *,
        branch: str,
        start_point: str | None = None,
    ) -> SlotStatus:
        status = self.inspect_slot(name)
        if status.active_job_id:
            raise SlotError(f"Slot {name} is active for job {status.active_job_id}")
        if not status.exists or not status.is_git_workspace:
            raise SlotError(f"Slot {name} is not an existing git workspace")
        if status.dirty:
            raise SlotError(f"Refusing to checkout dirty slot {name}")

        try:
            if _local_branch_exists(status.path, branch):
                run_git(status.path, "checkout", branch)
            elif start_point:
                run_git(status.path, "checkout", "-b", branch, start_point)
            else:
                run_git(status.path, "checkout", branch)
        except GitError as exc:
            raise SlotError(f"Could not checkout {branch} in slot {name}: {exc}") from exc

        return self.inspect_slot(name)

    def ensure_ide_module(self, name: str) -> dict[str, object]:
        status = self.inspect_slot(name)
        configured = self._config.slots.get(name)
        if configured is None:
            raise SlotError(f"Slot {name} is not configured in workspaces.toml")
        if status.route != configured.route:
            raise SlotError(f"Slot {name} route mismatch: {status.route!r} != {configured.route!r}")
        if status.path.resolve(strict=False) != configured.path.resolve(strict=False):
            raise SlotError(f"Slot {name} path mismatch: {status.path} != {configured.path}")
        return ensure_slot_ide_module(self._config, configured).as_dict()

    def ensure_ide_root_module(
        self,
        *,
        remove_configured_slot_modules: bool = False,
    ) -> dict[str, object]:
        dedicated_slots = self._dedicated_ide_slots()
        dedicated_slot_names = {slot.name for slot in dedicated_slots}
        result: dict[str, object] = {
            "root_module": ensure_slot_root_ide_module(self._config).as_dict(),
            "vcs_mappings": ensure_slot_ide_vcs_mappings(self._config),
            "dedicated_slot_modules": [
                ensure_slot_ide_module(self._config, slot).as_dict() for slot in dedicated_slots
            ],
            "removed_slot_modules": [],
        }
        if remove_configured_slot_modules:
            result["removed_slot_modules"] = [
                remove_slot_ide_module(self._config, name).as_dict()
                for name in sorted(self._config.slots)
                if name not in dedicated_slot_names
            ]
        return result

    def _dedicated_ide_slots(self) -> list[SlotConfig]:
        return [
            slot
            for slot in sorted(self._config.slots.values(), key=lambda item: item.name)
            if self._route_requires_dedicated_ide_module(slot.route)
        ]

    def _route_requires_dedicated_ide_module(self, route_name: str) -> bool:
        route = self._config.routes.get(route_name)
        return bool(route and route.ide_sdk_name)

    def unload_ide_root_module(self) -> dict[str, object]:
        return unload_slot_root_ide_module(self._config).as_dict()

    def unload_ide_module(self, name: str) -> dict[str, object]:
        if name not in self._config.slots:
            raise SlotError(f"Slot {name} is not configured in workspaces.toml")
        return unload_slot_ide_module(self._config, name).as_dict()

    def _prepare_commands_for_route(self, route: str) -> tuple[SlotPrepareCommand, ...]:
        return tuple(
            command
            for command in self._config.slot_prepare
            if not command.routes or route in command.routes
        )

    def remove_ide_module(self, name: str) -> dict[str, object]:
        if name not in self._config.slots:
            raise SlotError(f"Slot {name} is not configured in workspaces.toml")
        return remove_slot_ide_module(self._config, name).as_dict()

    def prepare_slot(self, name: str) -> list[dict[str, Any]]:
        status = self.inspect_slot(name)
        if not status.exists or not status.is_git_workspace:
            raise SlotError(f"Slot {name} is not an existing git workspace")
        try:
            return prepare_workspace_slot(
                slot_path=status.path,
                commands=self._prepare_commands_for_route(status.route),
                forbidden_status_globs=self._config.defaults.forbidden_status_globs,
            )
        except SlotPrepareError as exc:
            raise SlotError(f"Could not prepare slot {name}: {exc}") from exc

    def acquire_for_job(self, name: str, *, job_id: str, route: str) -> SlotRecord:
        status = self.inspect_slot(name)
        if status.route != route:
            raise SlotError(f"Slot {name} belongs to route {status.route!r}, not {route!r}")
        if status.active_job_id:
            raise SlotError(f"Slot {name} is already active for job {status.active_job_id}")
        if status.status == "deleted":
            raise SlotError(f"Slot {name} is marked deleted")
        if status.status != "available":
            raise SlotError(f"Slot {name} is {status.status!r}, not available")
        if status.dirty:
            raise SlotError(f"Slot {name} is dirty:\n{status.dirty}")
        if not status.exists or not status.is_git_workspace:
            raise SlotError(f"Slot {name} is not an existing git workspace")
        return self._store.acquire_slot(name, job_id)

    def release_for_job(
        self,
        name: str,
        *,
        job_id: str,
        status: str = "available",
        note: str | None = None,
    ) -> SlotRecord | None:
        try:
            return self._store.release_slot(name, job_id, status=status, note=note)
        except SlotStoreError:
            return None

    def cleanup(
        self,
        *,
        max_per_route: int,
        apply: bool = False,
        force: bool = False,
    ) -> list[CleanupDecision]:
        if max_per_route < 0:
            raise SlotError("max_per_route must be non-negative")
        statuses = self.list_slots()
        by_route: dict[str, list[SlotStatus]] = {}
        for status in statuses:
            if status.status == "deleted":
                continue
            by_route.setdefault(status.route, []).append(status)

        decisions: list[CleanupDecision] = []
        for _route, route_slots in sorted(by_route.items()):
            removable = sorted(route_slots, key=_cleanup_sort_key)
            excess = max(0, len(route_slots) - max_per_route)
            for status in removable[:excess]:
                if status.active_job_id:
                    decisions.append(_decision(status, "skip", "slot is active"))
                    continue
                if status.dirty and not force:
                    decisions.append(_decision(status, "skip", "slot is dirty"))
                    continue
                if not status.exists:
                    decisions.append(_decision(status, "skip", "slot path is missing"))
                    continue
                if not status.is_git_workspace:
                    decisions.append(_decision(status, "skip", "slot is not a git workspace"))
                    continue
                if not apply:
                    decisions.append(_decision(status, "would_delete", "exceeds route slot limit"))
                    continue
                self.delete_slot(status.name, force=force)
                decisions.append(_decision(status, "deleted", "exceeds route slot limit"))
        return decisions

    def _ensure_slot_path_allowed(self, path: Path) -> None:
        if not is_same_or_child(path, self._config.slot_root):
            raise SlotError(f"Slot path is outside slot_root: {path}")

    def _route_worktree_base(self, route: str) -> Path:
        route_config = self._config.routes.get(route)
        if route_config is None:
            return self._config.worktree_base
        return route_config.worktree_base


def _safe_slot_name(name: str) -> str:
    chars = [char.lower() if char.isalnum() else "-" for char in name]
    safe = "".join(chars).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    if not safe:
        raise SlotError("Slot name must contain at least one alphanumeric character")
    return safe


def _branch_exists(repo: Path, branch: str) -> bool:
    return _local_branch_exists(repo, branch)


def _local_branch_exists(repo: Path, branch: str) -> bool:
    try:
        run_git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    except GitError:
        return False
    return True


def _cleanup_sort_key(status: SlotStatus) -> tuple[int, str, int, str]:
    return (
        0 if status.last_used_at is None else 1,
        status.last_used_at or "",
        status.use_count,
        status.name,
    )


def _decision(status: SlotStatus, action: str, reason: str) -> CleanupDecision:
    return CleanupDecision(
        name=status.name,
        route=status.route,
        path=status.path,
        action=action,
        reason=reason,
    )
