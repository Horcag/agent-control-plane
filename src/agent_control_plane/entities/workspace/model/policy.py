from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.config import ControlConfig, RouteConfig
from agent_control_plane.shared.git_tools import GitError, workspace_state
from agent_control_plane.shared.path_rules import is_child


@dataclass(frozen=True)
class StartRequest:
    task_id: str
    route: str
    workspace_path: Path | None = None
    expected_branch: str | None = None
    allow_dirty: bool = False


@dataclass(frozen=True)
class PolicyCheck:
    ok: bool
    reasons: tuple[str, ...]
    route: RouteConfig | None
    workspace_path: Path | None
    expected_branch: str | None
    actual_branch: str | None
    dirty: str
    brief_path: Path
    result_path: Path


class WorkspacePolicy:
    def __init__(self, config: ControlConfig) -> None:
        self._config = config

    def check_start(self, request: StartRequest) -> PolicyCheck:
        reasons: list[str] = []
        task_dir = self._config.coordination_root / "tasks" / request.task_id
        brief_path = task_dir / "brief.md"
        result_path = task_dir / "result.md"
        route = self._config.routes.get(request.route)

        if route is None:
            reasons.append(f"Unknown route: {request.route}")
            return self._blocked(reasons, None, None, None, brief_path, result_path)

        workspace_path = (request.workspace_path or route.path).resolve(strict=False)
        expected_branch = request.expected_branch or route.required_branch

        if not brief_path.exists():
            reasons.append(f"Task brief not found: {brief_path}")
        if not workspace_path.exists():
            reasons.append(f"Workspace path not found: {workspace_path}")
        if not self._workspace_allowed(workspace_path, route):
            reasons.append(
                f"Workspace {workspace_path} is not the route path and is not under "
                f"the allowed worktree root {route.worktree_root}"
            )

        actual_branch: str | None = None
        dirty = ""
        if workspace_path.exists():
            try:
                state = workspace_state(workspace_path)
                actual_branch = state.branch
                dirty = state.porcelain
            except GitError as exc:
                reasons.append(f"Git workspace check failed: {exc}")

        if actual_branch is not None and actual_branch != expected_branch:
            reasons.append(
                f"Wrong branch in {workspace_path}: expected {expected_branch!r}, "
                f"got {actual_branch!r}"
            )
        if dirty and not request.allow_dirty:
            reasons.append("Workspace is dirty and allow_dirty is false")

        return PolicyCheck(
            ok=not reasons,
            reasons=tuple(reasons),
            route=route,
            workspace_path=workspace_path,
            expected_branch=expected_branch,
            actual_branch=actual_branch,
            dirty=dirty,
            brief_path=brief_path,
            result_path=result_path,
        )

    def _workspace_allowed(self, workspace_path: Path, route: RouteConfig) -> bool:
        if workspace_path.resolve(strict=False) == route.path.resolve(strict=False):
            return True
        if self._configured_slot_allowed(workspace_path, route):
            return True
        return route.worktree_root is not None and is_child(workspace_path, route.worktree_root)

    def _configured_slot_allowed(self, workspace_path: Path, route: RouteConfig) -> bool:
        resolved_workspace = workspace_path.resolve(strict=False)
        for slot in self._config.slots.values():
            if slot.route != route.name:
                continue
            if resolved_workspace == slot.path.resolve(strict=False):
                return True
        return False

    @staticmethod
    def _blocked(
        reasons: list[str],
        route: RouteConfig | None,
        workspace_path: Path | None,
        expected_branch: str | None,
        brief_path: Path,
        result_path: Path,
    ) -> PolicyCheck:
        return PolicyCheck(
            ok=False,
            reasons=tuple(reasons),
            route=route,
            workspace_path=workspace_path,
            expected_branch=expected_branch,
            actual_branch=None,
            dirty="",
            brief_path=brief_path,
            result_path=result_path,
        )
