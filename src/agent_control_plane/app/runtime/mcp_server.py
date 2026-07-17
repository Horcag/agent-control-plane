from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from agent_control_plane.app.runtime.orchestrator import (
    AgentControlPlane,
    PolicyError,
    StartOptions,
)
from agent_control_plane.entities.plan import PlanExecutionSpec, PlanTaskDefinition
from agent_control_plane.features.agent_runner import SUPPORTED_BACKENDS, normalize_backend
from agent_control_plane.features.slot_lifecycle import ConfigBootstrapError, SlotError
from agent_control_plane.shared.config import default_config_path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_CONFIG_LOAD_ATTEMPTS = 2
_CONFIG_LOCK_TIMEOUT_SEC = 2.0
_CONFIG_LOCK_RETRY_SEC = 0.02


class ConfigFreshnessError(RuntimeError):
    """Raised when config freshness cannot be preserved during an MCP call."""


class ConfigFreshControl:
    """Refresh the control plane before each MCP tool invocation when config changes."""

    def __init__(self, config_path: str | None) -> None:
        requested_path = Path(config_path).expanduser() if config_path else default_config_path()
        self._config_path = requested_path.resolve(strict=False)
        self._control, self._loaded_fingerprint = self._load_stable_control()
        self._config_reloaded = False
        self._lock = RLock()
        self._attach_configured_slots_sync_guard(self._control, self._loaded_fingerprint)

    def smoke(self) -> dict[str, Any]:
        control = self._fresh_control()
        payload = control.smoke()
        current_fingerprint = _config_fingerprint(self._config_path)
        payload.update(
            {
                "config_fingerprint_loaded": self._loaded_fingerprint,
                "config_fingerprint_current": current_fingerprint,
                "reload_required": current_fingerprint != self._loaded_fingerprint,
                "config_reloaded": self._config_reloaded,
            }
        )
        return payload

    def __getattr__(self, name: str) -> Any:
        def invoke(*args: Any, **kwargs: Any) -> Any:
            return getattr(self._fresh_control(), name)(*args, **kwargs)

        return invoke

    def _fresh_control(self) -> AgentControlPlane:
        with self._lock:
            current_fingerprint = _config_fingerprint(self._config_path)
            if current_fingerprint == self._loaded_fingerprint:
                return self._control
            self._control, self._loaded_fingerprint = self._load_stable_control()
            self._attach_configured_slots_sync_guard(self._control, self._loaded_fingerprint)
            self._config_reloaded = True
            return self._control

    def _load_stable_control(self) -> tuple[AgentControlPlane, str]:
        for _ in range(_CONFIG_LOAD_ATTEMPTS):
            config_contents = self._config_path.read_bytes()
            fingerprint = _fingerprint_contents(config_contents)
            try:
                control = AgentControlPlane.from_config_path(
                    str(self._config_path),
                    config_contents=config_contents,
                )
            except Exception:
                if _config_fingerprint(self._config_path) == fingerprint:
                    raise
                continue
            if _config_fingerprint(self._config_path) == fingerprint:
                return control, fingerprint
        raise ConfigFreshnessError(
            "configuration changed while it was being loaded; retry the MCP call"
        )

    def _attach_configured_slots_sync_guard(
        self,
        control: AgentControlPlane,
        expected_fingerprint: str,
    ) -> None:
        control.slots.set_configured_slots_sync_guard(
            lambda: _configured_slots_sync_guard(self._config_path, expected_fingerprint)
        )


def _config_fingerprint(config_path: Path) -> str:
    return _fingerprint_contents(config_path.read_bytes())


def _fingerprint_contents(config_contents: bytes) -> str:
    return hashlib.sha256(config_contents).hexdigest()


@contextmanager
def _configured_slots_sync_guard(
    config_path: Path,
    expected_fingerprint: str,
) -> Iterator[None]:
    with _interprocess_config_lock(config_path):
        if _config_fingerprint(config_path) != expected_fingerprint:
            raise ConfigFreshnessError(
                "configuration changed before configured slots synchronized; retry the MCP call"
            )
        yield


@contextmanager
def _interprocess_config_lock(config_path: Path) -> Iterator[None]:
    lock_path = _config_lock_path(config_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0)
        lock_file.write(b"\0")
        lock_file.truncate(1)
        lock_file.flush()
        _acquire_config_lock(lock_file, lock_path)
        try:
            yield
        finally:
            _release_config_lock(lock_file)


def _config_lock_path(config_path: Path) -> Path:
    canonical_path = os.path.normcase(str(config_path.resolve(strict=False)))
    fingerprint = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    return (
        Path(tempfile.gettempdir()) / "agent-control-plane" / "config-locks" / f"{fingerprint}.lock"
    )


def _acquire_config_lock(lock_file: Any, lock_path: Path) -> None:
    deadline = time.monotonic() + _CONFIG_LOCK_TIMEOUT_SEC
    while True:
        try:
            if sys.platform == "win32":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise ConfigFreshnessError(
                    f"timed out after {_CONFIG_LOCK_TIMEOUT_SEC:.1f}s acquiring "
                    f"configured-slot config lock: {lock_path}"
                ) from exc
            time.sleep(_CONFIG_LOCK_RETRY_SEC)


def _release_config_lock(lock_file: Any) -> None:
    if sys.platform == "win32":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def build_server(config_path: str | None = None) -> Any:
    try:
        fast_mcp = importlib.import_module("mcp.server.fastmcp").FastMCP
    except ImportError as exc:
        raise RuntimeError(
            'The MCP server dependency is missing. Install with: python -m pip install -e ".[mcp]"'
        ) from exc

    control = ConfigFreshControl(config_path)
    mcp = fast_mcp("agent-control-plane")

    @mcp.tool()
    def agent_smoke() -> dict[str, Any]:
        """Check configuration, database initialization, route paths, and agy availability."""
        return control.smoke()

    @mcp.tool()
    def agent_start_job(
        task_id: str,
        route: str,
        backend: str | None = None,
        agy_model: str | None = None,
        codex_model: str | None = None,
        codex_reasoning_effort: str | None = None,
        codex_quality_tier: str | None = None,
        slot: str | None = None,
        workspace_path: str | None = None,
        expected_branch: str | None = None,
        timeout_sec: int | None = None,
        idle_timeout_sec: int | None = None,
        print_timeout: str | None = None,
        max_restarts: int | None = None,
        yolo: bool | None = None,
        allow_dirty: bool | None = None,
        read_only: bool = False,
        wait: bool = False,
        wait_timeout_sec: float = 25.0,
        poll_interval_sec: float = 5.0,
        lines: int = 80,
        log_cursor: int | None = None,
        log_byte_limit: int = 2048,
        plan_id: str | None = None,
        plan_task_id: str | None = None,
        workspace_access: str | None = None,
    ) -> dict[str, Any]:
        """Start an agent job and optionally wait briefly for a terminal result."""
        normalized_backend = normalize_backend(backend) if backend is not None else None
        if normalized_backend is not None and normalized_backend not in SUPPORTED_BACKENDS:
            allowed = ", ".join(SUPPORTED_BACKENDS)
            return {
                "ok": False,
                "error": f"Unsupported backend {backend!r}. Expected one of: {allowed}",
            }
        try:
            job = control.start_job(
                StartOptions(
                    task_id=task_id,
                    route=route,
                    backend=normalized_backend,
                    agy_model=agy_model,
                    codex_model=codex_model,
                    codex_reasoning_effort=codex_reasoning_effort,
                    codex_quality_tier=codex_quality_tier,
                    slot=slot,
                    workspace_path=Path(workspace_path) if workspace_path else None,
                    expected_branch=expected_branch,
                    timeout_sec=timeout_sec,
                    idle_timeout_sec=idle_timeout_sec,
                    print_timeout=print_timeout,
                    max_restarts=max_restarts,
                    yolo=yolo,
                    allow_dirty=allow_dirty,
                    read_only=read_only,
                    plan_id=plan_id,
                    plan_task_id=plan_task_id,
                    workspace_access=workspace_access,
                )
            )
        except PolicyError as exc:
            return {"ok": False, "error": str(exc)}
        response = {
            "ok": True,
            "job_id": job.job_id,
            "status": job.status,
            "run_dir": str(job.run_dir),
            "result_path": str(job.result_path),
            "backend": job.backend,
            "agy_model": job.agy_model,
            "codex_model": job.codex_model,
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "codex_quality_tier": job.codex_quality_tier,
            "workspace_access": job.workspace_access,
            "worker_pid": job.worker_pid,
            "runner_pid": job.runner_pid,
            "read_only": job.read_only,
            "slot_name": job.slot_name,
            "plan_id": plan_id,
            "plan_task_id": plan_task_id or (task_id if plan_id else None),
        }
        if wait:
            response["watch"] = control.watch_job(
                job.job_id,
                poll_interval_sec=poll_interval_sec,
                timeout_sec=wait_timeout_sec,
                log_lines=lines,
                log_cursor=log_cursor,
                log_byte_limit=log_byte_limit,
            )
        return response

    @mcp.tool()
    def agent_watch_job(
        job_id: str,
        poll_interval_sec: float = 5.0,
        timeout_sec: float = 25.0,
        lines: int = 80,
        log_cursor: int | None = None,
        log_byte_limit: int = 2048,
    ) -> dict[str, Any]:
        """Poll compact status; pass next_log_cursor back to receive only new log bytes."""
        return control.watch_job(
            job_id,
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
            log_lines=lines,
            log_cursor=log_cursor,
            log_byte_limit=log_byte_limit,
        )

    @mcp.tool()
    def agent_status_job(job_id: str) -> dict[str, Any]:
        """Return job status, PID data, paths, and recent events."""
        return control.status_job(job_id)

    @mcp.tool()
    def agent_reconcile(
        job_id: str | None = None,
        terminate_verified_runners: bool = False,
    ) -> dict[str, Any]:
        """Recover orphaned jobs and replay crash-safe terminal finalization."""
        return control.reconcile_jobs(
            job_id,
            terminate_verified_runners=terminate_verified_runners,
        )

    @mcp.tool()
    def agent_summary_job(job_id: str, lines: int = 20) -> dict[str, Any]:
        """Return compact status, guardrail state, dirty status, and a short log tail."""
        return control.summary_job(job_id, lines)

    @mcp.tool()
    def agent_analytics(
        limit: int = 100,
        model: str | None = None,
        reasoning_effort: str | None = None,
        valid_only: bool = False,
    ) -> dict[str, Any]:
        """Aggregate duration, token, cache, tool, and estimated cost metrics."""
        return control.analytics(
            limit=limit,
            model=model,
            reasoning_effort=reasoning_effort,
            valid_only=valid_only,
        )

    @mcp.tool()
    def agent_plan_create(
        plan_id: str,
        title: str,
        objective: str = "",
        tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create one durable plan and its dependency graph in a single call."""
        try:
            definitions = _plan_task_definitions(tasks or [])
            return control.create_plan(
                plan_id=plan_id,
                title=title,
                objective=objective,
                tasks=definitions,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_add_task(
        plan_id: str,
        task_id: str,
        title: str,
        depends_on: list[str] | None = None,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a logical task and its dependencies to an existing plan."""
        try:
            return control.add_plan_task(
                plan_id,
                task_id=task_id,
                title=title,
                depends_on=tuple(depends_on or ()),
                execution=_plan_execution_spec(execution),
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_bind_job(plan_id: str, task_id: str, job_id: str) -> dict[str, Any]:
        """Bind an already-created job to a logical plan task."""
        try:
            return control.bind_plan_job(plan_id, task_id, job_id)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_snapshot(
        plan_id: str,
        since: int | None = None,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        """Return compact plan state; pass cursor as since to receive only new events."""
        try:
            return control.plan_snapshot(
                plan_id,
                since=since,
                event_limit=event_limit,
                item_limit=item_limit,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_watch(
        plan_id: str,
        since: int,
        poll_interval_sec: float = 5.0,
        timeout_sec: float = 25.0,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        """Long-poll until the plan cursor advances, without returning worker logs."""
        try:
            return control.watch_plan(
                plan_id,
                since=since,
                poll_interval_sec=poll_interval_sec,
                timeout_sec=timeout_sec,
                event_limit=event_limit,
                item_limit=item_limit,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_accept_task(
        plan_id: str,
        task_id: str,
        accepted_sha: str | None = None,
    ) -> dict[str, Any]:
        """Record root acceptance and unlock tasks that depend on this one."""
        try:
            return control.accept_plan_task(
                plan_id,
                task_id,
                accepted_sha=accepted_sha,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_reject_task(plan_id: str, task_id: str) -> dict[str, Any]:
        """Record root rejection without unlocking dependent tasks."""
        try:
            return control.reject_plan_task(plan_id, task_id)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_dispatch(plan_id: str, max_jobs: int = 1) -> dict[str, Any]:
        """Claim and start ready executable plan tasks in one durable dispatch pass."""
        try:
            return control.dispatch_plan(plan_id, max_jobs=max_jobs)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_run_until_review(
        plan_id: str,
        max_jobs: int = 1,
        poll_interval_sec: float = 5.0,
        timeout_sec: float | None = 25.0,
    ) -> dict[str, Any]:
        """Dispatch, watch, and reconcile until root review or another safe stop boundary."""
        try:
            return control.run_plan_until_review(
                plan_id,
                max_jobs=max_jobs,
                poll_interval_sec=poll_interval_sec,
                timeout_sec=timeout_sec,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_retry_task(
        plan_id: str,
        task_id: str,
        brief_override: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly make a failed task eligible for a new dispatch attempt."""
        try:
            return control.retry_plan_task(
                plan_id,
                task_id,
                brief_override=brief_override,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_cancel(plan_id: str) -> dict[str, Any]:
        """Stop future plan dispatch and cooperatively cancel unfinished jobs."""
        try:
            return {"ok": True, "cancellation": control.cancel_plan(plan_id)}
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_archive(plan_id: str) -> dict[str, Any]:
        """Mark one terminal, fully reviewed plan as retention-eligible."""
        try:
            return {"ok": True, "plan": control.archive_plan(plan_id)}
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_plan_list(
        limit: int = 20,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List recent durable plans with compact progress counts."""
        return control.list_plans(limit, include_archived=include_archived)

    @mcp.tool()
    def agent_retention_gc(
        older_than_days: int = 30,
        limit: int = 500,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Dry-run or prune only archived and reviewed state beyond retention."""
        try:
            return control.collect_garbage(
                older_than_days=older_than_days,
                limit=limit,
                apply=apply,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_review_inbox_list(
        review_status: str | None = "pending",
        limit: int = 50,
        sync_subagents: bool = False,
        since_hours: float = 72.0,
        max_files: int = 500,
        parent_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """List durable handoffs; optionally import completed Codex subagents first."""
        try:
            return {
                "ok": True,
                "items": control.list_review_inbox(
                    review_status=review_status,
                    parent_thread_id=parent_thread_id,
                    limit=limit,
                    sync_subagents=sync_subagents,
                    since_hours=since_hours,
                    max_files=max_files,
                ),
            }
        except (PolicyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_review_inbox_get(item_id: str) -> dict[str, Any]:
        """Return one durable job or Codex subagent handoff."""
        try:
            return {"ok": True, "item": control.get_review_inbox_item(item_id)}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_review_inbox_resolve(item_id: str, decision: str) -> dict[str, Any]:
        """Resolve an inbox item without implicitly accepting a plan task."""
        try:
            return {
                "ok": True,
                "item": control.resolve_review_inbox_item(item_id, decision),
            }
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_accept_handoff(
        plan_id: str,
        task_id: str,
        review_span_id: str,
        accepted_sha: str | None = None,
        attempt_no: int | None = None,
        defects_found: int = 0,
        false_positives: int = 0,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Atomically accept one inbox item, plan task, and root review outcome."""
        try:
            return {
                "ok": True,
                "acceptance": control.accept_handoff(
                    plan_id,
                    task_id,
                    review_span_id=review_span_id,
                    accepted_sha=accepted_sha,
                    attempt_no=attempt_no,
                    defects_found=defects_found,
                    false_positives=false_positives,
                    notes=notes,
                ),
            }
        except (KeyError, PolicyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_sync_subagent_results(
        since_hours: float = 72.0,
        max_files: int = 500,
        parent_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Import completed in-scope Codex subagent rollouts into the review inbox."""
        try:
            return {
                "ok": True,
                **control.sync_subagent_results(
                    since_hours=since_hours,
                    max_files=max_files,
                    parent_thread_id=parent_thread_id,
                ),
            }
        except (PolicyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_tail_job(job_id: str, lines: int = 80) -> str:
        """Return the end of the active attempt log."""
        return control.tail_job(job_id, lines)

    @mcp.tool()
    def agent_result_job(job_id: str) -> str:
        """Return the task result file content, or a not-ready message."""
        return control.result_job(job_id)

    @mcp.tool()
    def agent_cancel_job(job_id: str) -> dict[str, Any]:
        """Request cooperative cancellation for a running job."""
        job = control.cancel_job(job_id)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "cancel_requested": job.cancel_requested,
        }

    @mcp.tool()
    def agent_archive_jobs(
        older_than_days: int = 14,
        limit: int = 50,
        apply: bool = False,
    ) -> list[dict[str, Any]]:
        """List or archive terminal job run directories older than a threshold. Dry-run by default."""
        return control.archive_jobs(
            older_than_days=older_than_days,
            limit=limit,
            apply=apply,
        )

    @mcp.tool()
    def agent_slots_sync() -> list[dict[str, Any]]:
        """Register configured slots in SQLite and return their current state."""
        return control.sync_slots()

    @mcp.tool()
    def agent_slots_list() -> list[dict[str, Any]]:
        """Return slot usage, active job, and git state."""
        return control.list_slots()

    @mcp.tool()
    def agent_slots_create(
        name: str,
        route: str | None = None,
        branch: str | None = None,
        start_point: str | None = None,
    ) -> dict[str, Any]:
        """Create a configured slot or a dynamic route slot."""
        try:
            return {
                "ok": True,
                "slot": control.create_slot(
                    name,
                    route=route,
                    branch=branch,
                    start_point=start_point,
                ),
            }
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_bootstrap(
        name: str,
        route: str | None = None,
        repo_path: str | None = None,
        required_branch: str | None = None,
        slot_path: str | None = None,
        branch: str | None = None,
        start_point: str | None = None,
        create: bool = True,
        ensure_ide: bool = True,
        remove_slot_modules: bool = True,
    ) -> dict[str, Any]:
        """Add missing route/slot config, create the slot, and update IDEA/VCS mappings."""
        try:
            return {
                "ok": True,
                "bootstrap": control.bootstrap_slot(
                    name,
                    route=route or _infer_route_from_slot_name(name),
                    repo_path=Path(repo_path) if repo_path else None,
                    required_branch=required_branch,
                    slot_path=Path(slot_path) if slot_path else None,
                    branch=branch,
                    start_point=start_point,
                    create=create,
                    ensure_ide=ensure_ide,
                    remove_slot_modules=remove_slot_modules,
                ),
            }
        except (ConfigBootstrapError, SlotError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_delete(name: str, force: bool = False) -> dict[str, Any]:
        """Delete a slot worktree. Dirty or active slots require force."""
        try:
            return {"ok": True, "slot": control.delete_slot(name, force=force)}
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_checkout(
        name: str,
        branch: str,
        start_point: str | None = None,
    ) -> dict[str, Any]:
        """Checkout a clean inactive slot to a target branch."""
        try:
            return {
                "ok": True,
                "slot": control.checkout_slot(name, branch=branch, start_point=start_point),
            }
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_ensure_module(name: str) -> dict[str, Any]:
        """Ensure a configured slot is registered and marked loaded in IDEA workspace state."""
        try:
            return {"ok": True, "module": control.ensure_slot_ide_module(name)}
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_ensure_root_module(
        remove_slot_modules: bool = False,
    ) -> dict[str, Any]:
        """Ensure managed IDEA modules, SDK roots, and module-scoped duplicate analysis."""
        try:
            return {
                "ok": True,
                "module": control.ensure_slot_root_ide_module(
                    remove_slot_modules=remove_slot_modules,
                ),
            }
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_unload_module(name: str) -> dict[str, Any]:
        """Mark a configured slot module as unloaded without deleting its module entry."""
        try:
            return {"ok": True, "module": control.unload_slot_ide_module(name)}
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_unload_root_module() -> dict[str, Any]:
        """Mark the slot_root IDEA module as unloaded without deleting its module entry."""
        try:
            return {"ok": True, "module": control.unload_slot_root_ide_module()}
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_remove_module(name: str) -> dict[str, Any]:
        """Remove a configured legacy slot module from IDEA project and workspace state."""
        try:
            return {"ok": True, "module": control.remove_slot_ide_module(name)}
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_prepare(name: str) -> dict[str, Any]:
        """Run configured slot preparation commands when markers are missing."""
        try:
            return {"ok": True, "preparation": control.prepare_slot(name)}
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_checkpoint(name: str, job_id: str) -> dict[str, Any]:
        """Checkpoint a terminal job's dirty slot, persist review metadata, and release it."""
        try:
            return {"ok": True, **control.checkpoint_slot(name, job_id=job_id)}
        except (KeyError, PolicyError, SlotError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    def agent_slots_cleanup(
        max_per_route: int,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """List or apply least-recently-used slot cleanup above a per-route limit."""
        try:
            return {
                "ok": True,
                "apply": apply,
                "decisions": control.cleanup_slots(
                    max_per_route=max_per_route,
                    apply=apply,
                    force=force,
                ),
            }
        except SlotError as exc:
            return {"ok": False, "error": str(exc)}

    return mcp


def _infer_route_from_slot_name(slot_name: str) -> str:
    prefix, separator, suffix = slot_name.rpartition("-")
    if separator and prefix and suffix.isdigit():
        return prefix
    return slot_name


def _plan_task_definitions(payload: list[dict[str, Any]]) -> tuple[PlanTaskDefinition, ...]:
    definitions = []
    for item in payload:
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(
            isinstance(value, str) for value in depends_on
        ):
            raise ValueError("Plan task depends_on must be an array of task IDs")
        definitions.append(
            PlanTaskDefinition(
                task_id=str(item.get("task_id", "")),
                title=str(item.get("title", "")),
                depends_on=tuple(depends_on),
                execution=_plan_execution_spec(item.get("execution")),
            )
        )
    return tuple(definitions)


def _plan_execution_spec(payload: Any) -> PlanExecutionSpec | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Plan task execution must be an object")
    read_only = payload.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError("Plan task execution read_only must be a boolean")
    return PlanExecutionSpec(
        route=str(payload.get("route", "")),
        brief=str(payload.get("brief", "")),
        slot=_optional_text(payload.get("slot")),
        backend=_optional_text(payload.get("backend")),
        workspace_access=_optional_text(payload.get("workspace_access")),
        read_only=read_only,
        codex_quality_tier=_optional_text(payload.get("codex_quality_tier")),
        codex_model=_optional_text(payload.get("codex_model")),
        codex_reasoning_effort=_optional_text(payload.get("codex_reasoning_effort")),
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Control Plane MCP server.")
    parser.add_argument("--config", help="Path to workspaces.toml")
    args = parser.parse_args(argv)
    build_server(args.config).run()


if __name__ == "__main__":
    main()
