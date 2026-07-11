from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agent_control_plane.app.runtime.orchestrator import (
    AgentControlPlane,
    PolicyError,
    StartOptions,
)
from agent_control_plane.features.agent_runner import SUPPORTED_BACKENDS, normalize_backend
from agent_control_plane.features.slot_lifecycle import ConfigBootstrapError, SlotError


def build_server(config_path: str | None = None) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            'The MCP server dependency is missing. Install with: python -m pip install -e ".[mcp]"'
        ) from exc

    control = AgentControlPlane.from_config_path(config_path)
    mcp = FastMCP("agent-control-plane")

    @mcp.tool()
    def agent_smoke() -> dict[str, Any]:
        """Check configuration, database initialization, route paths, and agy availability."""
        return control.smoke()

    @mcp.tool()
    def agent_start_job(
        task_id: str,
        route: str,
        backend: str | None = None,
        codex_model: str | None = None,
        codex_reasoning_effort: str | None = None,
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
                    codex_model=codex_model,
                    codex_reasoning_effort=codex_reasoning_effort,
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
            "codex_model": job.codex_model,
            "codex_reasoning_effort": job.codex_reasoning_effort,
            "worker_pid": job.worker_pid,
            "runner_pid": job.runner_pid,
            "read_only": job.read_only,
            "slot_name": job.slot_name,
        }
        if wait:
            response["watch"] = control.watch_job(
                job.job_id,
                poll_interval_sec=poll_interval_sec,
                timeout_sec=wait_timeout_sec,
                log_lines=lines,
            )
        return response

    @mcp.tool()
    def agent_watch_job(
        job_id: str,
        poll_interval_sec: float = 5.0,
        timeout_sec: float = 25.0,
        lines: int = 80,
    ) -> dict[str, Any]:
        """Poll a job until it reaches terminal status or timeout. Keep timeout below MCP limits."""
        return control.watch_job(
            job_id,
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
            log_lines=lines,
        )

    @mcp.tool()
    def agent_status_job(job_id: str) -> dict[str, Any]:
        """Return job status, PID data, paths, and recent events."""
        return control.status_job(job_id)

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Control Plane MCP server.")
    parser.add_argument("--config", help="Path to workspaces.toml")
    args = parser.parse_args(argv)
    build_server(args.config).run()


if __name__ == "__main__":
    main()
