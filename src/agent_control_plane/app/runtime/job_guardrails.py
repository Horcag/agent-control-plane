from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.entities.job import JobRecord
from agent_control_plane.entities.workspace import (
    ForbiddenStatusEntry,
    find_forbidden_status_entries,
    find_new_forbidden_status_entries,
)
from agent_control_plane.features.agent_runner import (
    CLAUDE_BACKEND,
    CODEX_BACKEND,
    inspect_result,
    normalize_backend,
)
from agent_control_plane.features.slot_lifecycle import RouteRootGuard, RouteRootSnapshot
from agent_control_plane.shared.config import RouteConfig
from agent_control_plane.shared.git_tools import (
    GitError,
    compact_status_preview,
    diff_patch,
    head_commit,
    workspace_snapshot,
    workspace_state,
)

# 500 killed legitimate Claude implementation jobs three times on 2026-07-20/21:
# a single Write of one large test file crosses it between 2s guardrail polls,
# faster than any incremental-commit discipline can reset the baseline.
CODEX_DIRTY_DIFF_MAX_CHANGED_LINES = 1200
ROUTE_ROOT_INDEX_GRACE_SEC = 15.0


@dataclass(frozen=True)
class GuardrailBaseline:
    entries: tuple[ForbiddenStatusEntry, ...]
    fingerprints: dict[tuple[str, str, str], str]
    diff_changed_lines: int = 0


@dataclass(frozen=True)
class WorkspaceDirtyBaseline:
    path: Path
    guard: RouteRootGuard


class JobGuardrails:
    """Capture and evaluate workspace safety boundaries during job execution."""

    def __init__(self, forbidden_status_globs: tuple[str, ...]) -> None:
        self._forbidden_status_globs = forbidden_status_globs

    def workspace_baseline(self, job: JobRecord) -> GuardrailBaseline:
        try:
            state = workspace_state(job.workspace_path)
        except GitError:
            return GuardrailBaseline(entries=(), fingerprints={})
        entries = tuple(
            find_forbidden_status_entries(state.porcelain, self._forbidden_status_globs)
        )
        try:
            baseline_patch = diff_patch(job.workspace_path) if state.dirty else ""
        except GitError:
            baseline_patch = ""
        return GuardrailBaseline(
            entries=entries,
            fingerprints={
                self._forbidden_entry_key(entry): self._status_path_fingerprint(
                    job.workspace_path,
                    entry.path,
                )
                for entry in entries
            },
            diff_changed_lines=self._diff_changed_line_count(baseline_patch),
        )

    def route_root_baseline(
        self,
        job: JobRecord,
        route_config: RouteConfig | None,
    ) -> WorkspaceDirtyBaseline | None:
        if not job.slot_name or route_config is None or not route_config.monitor_route_root:
            return None
        route_root = route_config.path.resolve(strict=False)
        if route_root == job.workspace_path.resolve(strict=False):
            return None
        try:
            snapshot = self.route_root_snapshot(route_root)
            if not snapshot.stable:
                snapshot = self.route_root_snapshot(route_root)
        except GitError:
            return WorkspaceDirtyBaseline(
                path=route_root,
                guard=RouteRootGuard(head=None, entries={}),
            )
        return WorkspaceDirtyBaseline(
            path=route_root,
            guard=RouteRootGuard(head=snapshot.head, entries=dict(snapshot.entries)),
        )

    def route_root_snapshot(self, route_root: Path) -> RouteRootSnapshot:
        git_snapshot = workspace_snapshot(route_root)
        entries = {
            path: (status, self._status_path_fingerprint(route_root, path))
            for status, path in _status_entries(git_snapshot.porcelain)
        }
        stable = git_snapshot.stable
        if stable:
            stable = head_commit(route_root) == git_snapshot.head
        return RouteRootSnapshot(
            head=git_snapshot.head,
            entries=entries,
            porcelain=git_snapshot.porcelain,
            stable=stable,
        )

    def route_root_violation(
        self,
        job: JobRecord,
        baseline: WorkspaceDirtyBaseline | None,
    ) -> str | None:
        if baseline is None:
            return None
        try:
            snapshot = self.route_root_snapshot(baseline.path)
        except GitError as exc:
            return f"Route root guardrail could not inspect git status: {exc}"
        changed = baseline.guard.evaluate(
            snapshot,
            now=time.monotonic(),
            staged_grace_sec=ROUTE_ROOT_INDEX_GRACE_SEC,
        )
        if not changed:
            return None
        status_path = job.run_dir / "route-root-guardrail-status.txt"
        status_path.write_text(snapshot.porcelain, encoding="utf-8")
        try:
            patch = diff_patch(baseline.path)
        except GitError as exc:
            patch = f"Could not capture route root git diff: {exc}\n"
        (job.run_dir / "route-root-guardrail.patch").write_text(patch, encoding="utf-8")
        preview = "; ".join(changed[:8])
        if len(changed) > 8:
            preview += f"; ... ({len(changed) - 8} more)"
        return (
            "Slot job modified route root outside assigned workspace. "
            f"Assigned workspace: {job.workspace_path}; route root: {baseline.path}; "
            f"changed route-root paths: {preview}. Preserved status in {status_path}"
        )

    def workspace_violation(
        self,
        job: JobRecord,
        baseline: GuardrailBaseline,
    ) -> str | None:
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return f"Guardrail could not inspect git status: {exc}"
        if job.read_only and state.dirty:
            return f"Read-only job modified workspace: {state.porcelain}"
        current_entries = find_forbidden_status_entries(
            state.porcelain,
            self._forbidden_status_globs,
        )
        entries = find_new_forbidden_status_entries(
            state.porcelain,
            self._forbidden_status_globs,
            list(baseline.entries),
        )
        entries.extend(self._changed_baseline_entries(job, baseline, current_entries))
        entries = self._dedupe_entries(entries)
        if not entries:
            return None
        details = "; ".join(
            f"{entry.status} {entry.path} matched {entry.matched_glob}" for entry in entries
        )
        return f"Forbidden workspace change detected: {details}"

    def codex_dirty_diff_violation(
        self,
        job: JobRecord,
        baseline: GuardrailBaseline,
    ) -> str | None:
        if normalize_backend(job.backend) not in {CODEX_BACKEND, CLAUDE_BACKEND}:
            return None
        if inspect_result(job.result_path, 0.0).done:
            return None
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return f"Codex dirty diff guardrail could not inspect git status: {exc}"
        if not state.dirty:
            return None
        try:
            patch = diff_patch(job.workspace_path)
        except GitError as exc:
            return f"Codex dirty diff guardrail could not inspect git diff: {exc}"
        changed_lines = self._diff_changed_line_count(patch)
        growth = max(0, changed_lines - baseline.diff_changed_lines)
        if growth <= CODEX_DIRTY_DIFF_MAX_CHANGED_LINES:
            return None
        return (
            "Codex dirty diff exceeded "
            f"{CODEX_DIRTY_DIFF_MAX_CHANGED_LINES} changed-line growth "
            f"without a valid result (baseline {baseline.diff_changed_lines}, "
            f"current {changed_lines}, growth {growth}). "
            f"Dirty status: {compact_status_preview(state.porcelain)}"
        )

    @staticmethod
    def preserve_dirty_state(job: JobRecord, *, prefix: str) -> str | None:
        try:
            state = workspace_state(job.workspace_path)
        except GitError as exc:
            return f"Could not inspect workspace after failure: {exc}"
        if not state.dirty:
            return None
        dirty_status = job.run_dir / f"{prefix}-status.txt"
        dirty_status.write_text(state.porcelain, encoding="utf-8")
        try:
            patch = diff_patch(job.workspace_path)
        except GitError as exc:
            patch = f"Could not capture git diff: {exc}\n"
        (job.run_dir / f"{prefix}.patch").write_text(patch, encoding="utf-8")
        return f"Workspace is dirty. Preserved status in {dirty_status}"

    def _changed_baseline_entries(
        self,
        job: JobRecord,
        baseline: GuardrailBaseline,
        current_entries: list[ForbiddenStatusEntry],
    ) -> list[ForbiddenStatusEntry]:
        changed: list[ForbiddenStatusEntry] = []
        for entry in current_entries:
            key = self._forbidden_entry_key(entry)
            baseline_fingerprint = baseline.fingerprints.get(key)
            if baseline_fingerprint is None:
                continue
            current_fingerprint = self._status_path_fingerprint(job.workspace_path, entry.path)
            if current_fingerprint != baseline_fingerprint:
                changed.append(entry)
        return changed

    @classmethod
    def _dedupe_entries(
        cls,
        entries: list[ForbiddenStatusEntry],
    ) -> list[ForbiddenStatusEntry]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[ForbiddenStatusEntry] = []
        for entry in entries:
            key = cls._forbidden_entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return deduped

    @staticmethod
    def _forbidden_entry_key(entry: ForbiddenStatusEntry) -> tuple[str, str, str]:
        return (
            entry.status,
            _normalize_status_path(entry.path),
            _normalize_status_path(entry.matched_glob),
        )

    @staticmethod
    def _status_path_fingerprint(workspace_path: Path, status_path: str) -> str:
        path = workspace_path / Path(status_path)
        try:
            if not path.exists():
                return "missing"
            if path.is_dir():
                return "directory"
            if not path.is_file():
                return "other"
            digest = hashlib.sha256()
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
            return f"file:{digest.hexdigest()}"
        except OSError as exc:
            return f"error:{type(exc).__name__}:{exc}"

    @staticmethod
    def _diff_changed_line_count(patch: str) -> int:
        return sum(
            1
            for line in patch.splitlines()
            if (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
        )


def _status_entries(porcelain: str) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", maxsplit=1)[1].strip()
        if len(path) >= 2 and path[0] == path[-1] == '"':
            path = path[1:-1]
        if path:
            entries.append((line[:2], path))
    return tuple(entries)


def _normalize_status_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")
