from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from agent_control_plane.app.runtime.cli_commands import add_demo_parser, add_plan_parser
from agent_control_plane.app.runtime.demo import (
    OfflineDemoError,
    accept_demo,
    run_demo,
    show_demo,
)
from agent_control_plane.app.runtime.orchestrator import (
    AgentControlPlane,
    PolicyError,
    StartOptions,
)
from agent_control_plane.app.runtime.plan_cli import handle_plan_command
from agent_control_plane.app.runtime.review_cli import add_review_parser, handle_review_command
from agent_control_plane.entities.job import TERMINAL_STATUSES
from agent_control_plane.features.agent_runner import (
    SUPPORTED_BACKENDS,
    WRAPPER_RELATIVE_PATH,
    AgyLauncherError,
    build_agy_launch,
    configure_managed_project,
    generate_project_wrapper,
    migrate_project_wrapper,
    restore_managed_config,
    rollback_project_wrapper,
    validate_configured_project,
    validate_migration_binding,
    validate_migration_path,
    validate_recovery_binding,
)
from agent_control_plane.features.antigravity_accounts import AntigravityManagerError
from agent_control_plane.features.job_watch import (
    DEFAULT_STALE_AFTER_SEC,
    RESUMED,
    STALE,
    START,
    TERMINAL,
    TRANSITION,
    WATCH_ERROR,
    EmptySelectionError,
    WatchEvent,
    WatchEventStream,
    WatchSelection,
)
from agent_control_plane.features.slot_lifecycle import ConfigBootstrapError, SlotError
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.config import (
    default_config_path,
    ensure_mcp_server,
    resolve_config_for,
    wire_mcp_servers,
)

# The only values a job may declare as expected_result_status (see
# entities/job/model/store.py:_validate_controller_contract). A terminal status outside this
# set can never satisfy is_on_contract, no matter what a job expects.
_ON_CONTRACT_CAPABLE_STATUSES = frozenset({"completed", "partial", "blocked"})

_WATCH_EVENT_KIND_LABELS = {
    START: "START",
    TRANSITION: "TRANSITION",
    TERMINAL: "TERMINAL",
    STALE: "STALE",
    RESUMED: "RESUMED",
    WATCH_ERROR: "WATCH-ERROR",
}

_WATCH_ERROR_MAX_LEN = 240


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    migration_project: Path | None = None
    migration_config: Path | None = None
    if args.command == "agy-wrapper":
        try:
            migration_project = validate_migration_path(Path(args.project), "project path")
            if args.config:
                migration_config = validate_migration_path(Path(args.config), "config path")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "statuses":
        _print_statuses(json_output=args.json)
        return 0
    if args.command == "demo":
        try:
            if args.demo_command == "run":
                _print_json(run_demo(Path(args.output), no_failure=args.no_failure))
                return 0
            if args.demo_command == "show":
                _print_json(show_demo(Path(args.root)))
                return 0
            if args.demo_command == "accept":
                _print_json(accept_demo(Path(args.root)))
                return 0
        except OfflineDemoError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "mcp":
        if args.mcp_command == "ensure":
            try:
                payload = ensure_mcp_server(
                    cwd=args.cwd,
                    config_path=args.config,
                    print_url=args.print_url,
                    no_start=args.no_start,
                    timeout_sec=args.timeout,
                )
                if args.print_url:
                    print(payload["url"])
                else:
                    _print_json(payload)
                return 0
            except (RuntimeError, ValueError, OSError, FileNotFoundError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
        if args.mcp_command == "wire":
            try:
                payload = wire_mcp_servers(
                    cwd=args.cwd,
                    config_path=args.config,
                    apply=args.apply,
                    print_output=args.print_output,
                )
                _print_json(payload)
                return 0
            except (RuntimeError, ValueError, OSError, FileNotFoundError) as exc:
                print(str(exc), file=sys.stderr)
                return 2

    if args.config:
        config_path: Path | str = args.config
    else:
        cwd = Path.cwd()
        config_path = resolve_config_for(cwd)
        if config_path.resolve(strict=False) != default_config_path().resolve(strict=False):
            print(f"Using discovered config {config_path} for {cwd}", file=sys.stderr)

    if args.command == "agy-wrapper" and migration_config is None:
        try:
            migration_config = validate_migration_path(Path(config_path), "config path")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.command == "agy-wrapper" and args.agy_wrapper_command == "restore-config":
        if migration_project is None or migration_config is None:
            print("missing validated recovery paths", file=sys.stderr)
            return 2
        try:
            project, recovery_config = validate_recovery_binding(
                project_path=migration_project,
                config_path=migration_config,
                expected_current_sha256=args.expected_current_sha256,
            )
            _print_json(
                restore_managed_config(
                    config_path=recovery_config,
                    project_path=project,
                    backup_path=Path(args.backup),
                    expected_current_sha256=args.expected_current_sha256,
                ).as_dict()
            )
            return 0
        except (AgyLauncherError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        control = AgentControlPlane.from_config_path(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "agy-wrapper":
            if migration_project is None:
                print("missing validated project path", file=sys.stderr)
                return 2
            project = migration_project
            wrapper = project / WRAPPER_RELATIVE_PATH
            configured_project_paths = tuple(
                path
                for route in control.config.routes.values()
                for path in (route.path, route.worktree_base)
            )
            config_path = migration_config or control.config.config_path
            if args.agy_wrapper_command == "configure":
                project, config_path = validate_migration_binding(
                    project_path=project,
                    config_path=config_path,
                    expected_config_sha256=args.expected_config_sha256,
                    expected_agy_command=args.expected_agy_command,
                    configured_project_paths=configured_project_paths,
                    actual_agy_command=control.config.agy_command,
                )
                _print_json(
                    configure_managed_project(
                        config_path=config_path,
                        project_path=project,
                        expected_config_sha256=args.expected_config_sha256,
                        adapter=validate_migration_path(Path(args.adapter), "adapter path"),
                        apply=args.apply,
                    ).as_dict()
                )
                return 0
            if args.agy_wrapper_command == "migrate":
                project, config_path = validate_migration_binding(
                    project_path=project,
                    config_path=config_path,
                    expected_config_sha256=args.expected_config_sha256,
                    expected_agy_command=args.expected_agy_command,
                    configured_project_paths=configured_project_paths,
                    actual_agy_command=control.config.agy_command,
                )
                if args.launch_mode != control.config.agy_launch_mode:
                    raise ValueError("--launch-mode must match [control].agy_launch_mode")
                configured_adapter = control.config.agy_proxy_launcher
                if args.launch_mode == "managed":
                    if args.adapter is None:
                        raise ValueError("managed migration requires --adapter")
                    if (
                        configured_adapter is None
                        or Path(args.adapter).expanduser() != configured_adapter
                    ):
                        raise ValueError("--adapter must match [control].agy_proxy_launcher")
                elif args.adapter is not None:
                    raise ValueError("--adapter is only valid with --launch-mode managed")
                wrapper = project / WRAPPER_RELATIVE_PATH
                launch = build_agy_launch(
                    agy_command=control.config.agy_command,
                    mode=control.config.agy_launch_mode,
                    adapter=control.config.agy_proxy_launcher,
                    job_id="agy-wrapper-migration",
                    attempt_ref="manual",
                )
                _print_json(
                    migrate_project_wrapper(
                        wrapper=wrapper,
                        project_path=project,
                        config_path=config_path,
                        expected_sha256=args.expected_sha256,
                        expected_config_sha256=args.expected_config_sha256,
                        launcher=Path(launch.executable),
                        apply=args.apply,
                    ).as_dict()
                )
                return 0
            if args.agy_wrapper_command == "generate":
                project, config_path = validate_migration_binding(
                    project_path=project,
                    config_path=config_path,
                    expected_config_sha256=args.expected_config_sha256,
                    expected_agy_command=args.expected_agy_command,
                    configured_project_paths=configured_project_paths,
                    actual_agy_command=control.config.agy_command,
                )
                if control.config.agy_launch_mode != "managed":
                    raise ValueError(
                        "wrapper generation requires [control].agy_launch_mode = managed"
                    )
                launch = build_agy_launch(
                    agy_command=control.config.agy_command,
                    mode=control.config.agy_launch_mode,
                    adapter=control.config.agy_proxy_launcher,
                    job_id="agy-wrapper-generation",
                    attempt_ref="manual",
                )
                _print_json(
                    generate_project_wrapper(
                        wrapper=wrapper,
                        project_path=project,
                        config_path=config_path,
                        expected_config_sha256=args.expected_config_sha256,
                        launcher=Path(launch.executable),
                        apply=args.apply,
                    ).as_dict()
                )
                return 0
            if args.agy_wrapper_command == "rollback":
                validate_configured_project(project, configured_project_paths)
                _print_json(
                    rollback_project_wrapper(
                        wrapper=wrapper,
                        backup_path=Path(args.backup),
                        expected_current_sha256=args.expected_current_sha256,
                    ).as_dict()
                )
                return 0
        if args.command == "smoke":
            payload = control.smoke()
            _print_json(payload)
            return 1 if payload.get("status") == "failed" else 0
        if args.command == "model-catalog":
            payload = control.model_catalog_inspection()
            _print_json(payload)
            if getattr(args, "check", False):
                has_warning = any(a.get("severity") == "warning" for a in payload.get("alerts", []))
                if has_warning:
                    return 1
            return 0
        if args.command == "model-routing-explain":
            _print_json(control.model_routing_explain(args.policy, args.route))
            return 0
        if args.command == "reconcile":
            payload = control.reconcile_jobs(
                args.job_id,
                terminate_verified_runners=args.terminate_verified_runners,
            )
            _print_json(payload)
            unresolved = (
                "errors",
                "live_runner_conflicts",
                "runner_identity_conflicts",
                "worker_identity_conflicts",
            )
            return 1 if any(payload.get(key) for key in unresolved) else 0
        if args.command == "plan":
            _print_json(handle_plan_command(control, args))
            return 0
        if args.command == "gc":
            _print_json(
                control.collect_garbage(
                    older_than_days=args.older_than_days,
                    limit=args.limit,
                    apply=args.apply,
                )
            )
            return 0
        if args.command == "start":
            control.ensure_route_admitted(args.route)
            if args.brief_file:
                _install_brief_file(
                    coordination_root=control.config.coordination_root,
                    task_id=args.task_id,
                    brief_file=Path(args.brief_file),
                    overwrite=args.overwrite_brief,
                )
            job = control.start_job(
                StartOptions(
                    task_id=args.task_id,
                    route=args.route,
                    backend=args.backend,
                    agy_model=args.agy_model,
                    codex_model=args.codex_model,
                    codex_reasoning_effort=args.codex_reasoning_effort,
                    claude_model=args.claude_model,
                    claude_reasoning_effort=args.claude_reasoning_effort,
                    codex_quality_tier=args.codex_quality_tier,
                    codex_premium_override_reason=args.codex_premium_override_reason,
                    codex_tool_call_budget=args.codex_tool_call_budget,
                    slot=args.slot,
                    workspace_path=Path(args.workspace_path) if args.workspace_path else None,
                    expected_branch=args.expected_branch,
                    timeout_sec=args.timeout_sec,
                    idle_timeout_sec=args.idle_timeout_sec,
                    print_timeout=args.print_timeout,
                    max_restarts=args.max_restarts,
                    yolo=args.yolo,
                    allow_dirty=args.allow_dirty,
                    read_only=args.read_only,
                    plan_id=args.plan_id,
                    plan_task_id=args.plan_task_id,
                    workspace_access=args.workspace_access,
                    expected_result_status=args.expected_result_status,
                    controller_gate_mode=args.controller_gate_mode,
                )
            )
            payload = _job_payload(job)
            payload["plan_id"] = args.plan_id
            payload["plan_task_id"] = args.plan_task_id or (args.task_id if args.plan_id else None)
            if args.wait:
                if args.live:
                    payload["watch"] = _watch_job_live(
                        control,
                        job.job_id,
                        poll_interval_sec=args.poll_interval_sec,
                        timeout_sec=args.wait_timeout_sec,
                        log_lines=args.lines,
                    )
                else:
                    payload["watch"] = control.watch_job(
                        job.job_id,
                        poll_interval_sec=args.poll_interval_sec,
                        timeout_sec=args.wait_timeout_sec,
                        log_lines=args.lines,
                        include_details=True,
                    )
            supervision = control.supervision_for([job.job_id])
            if args.wait and (payload.get("watch") or {}).get("settled"):
                supervision = {"supervised": True}
            payload["supervision"] = supervision
            _print_json(payload)
            if not supervision.get("supervised"):
                # stderr, so piping start's JSON stays clean while the operator still sees it.
                print("not supervised - watch it with:", file=sys.stderr)
                print(f"  {supervision['watch_command']}", file=sys.stderr)
            if args.wait:
                return _exit_code_for_watch_payload(payload["watch"])
            return 0
        if args.command == "run-job":
            job = control.run_job(args.job_id, args.worker_instance_id)
            _print_json({"job_id": job.job_id, "status": job.status, "last_error": job.last_error})
            return 0
        if args.command == "status":
            _print_json(control.status_job(args.job_id))
            return 0
        if args.command == "summary":
            _print_json(control.summary_job(args.job_id, args.lines))
            return 0
        if args.command == "analytics":
            _print_json(
                control.analytics(
                    limit=args.limit,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    backend=args.backend,
                    valid_only=args.valid_only,
                )
            )
            return 0
        if args.command == "review":
            _print_json(
                handle_review_command(
                    args,
                    database_path=control.config.database_path,
                )
            )
            return 0
        if args.command == "inbox":
            if args.inbox_command == "list":
                review_status = None if args.status == "all" else args.status
                _print_json(
                    control.list_review_inbox(
                        review_status=review_status,
                        parent_thread_id=args.parent_thread_id,
                        limit=args.limit,
                        sync_subagents=args.sync_subagents,
                        since_hours=args.since_hours,
                        max_files=args.max_files,
                    )
                )
                return 0
            if args.inbox_command in ("show", "get"):
                _print_json(control.get_review_inbox_item(args.item_id))
                return 0
            if args.inbox_command == "resolve":
                _print_json(
                    control.resolve_review_inbox_item(
                        args.item_id,
                        args.decision,
                    )
                )
                return 0
            if args.inbox_command == "requalify":
                _print_json(control.requalify_review_inbox_item(args.item_id))
                return 0
            if args.inbox_command == "sync-subagents":
                _print_json(
                    control.sync_subagent_results(
                        since_hours=args.since_hours,
                        max_files=args.max_files,
                        parent_thread_id=args.parent_thread_id,
                    )
                )
                return 0
        if args.command == "accept-handoff":
            _print_json(
                control.accept_handoff(
                    args.plan_id,
                    args.task_id,
                    review_span_id=args.review_span_id,
                    accepted_sha=args.accepted_sha,
                    attempt_no=args.attempt,
                    defects_found=args.defects_found,
                    false_positives=args.false_positives,
                    notes=args.notes,
                )
            )
            return 0
        if args.command == "watch":
            return _handle_watch_command(control, args)
        if args.command == "tail":
            print(control.tail_job(args.job_id, args.lines))
            return 0
        if args.command == "result":
            print(control.result_job(args.job_id))
            return 0
        if args.command == "cancel":
            job = control.cancel_job(args.job_id)
            _print_json({"job_id": job.job_id, "status": job.status})
            return 0
        if args.command == "list":
            _print_json(
                [
                    {
                        "job_id": job.job_id,
                        "task_id": job.task_id,
                        "status": job.status,
                        "backend": job.backend,
                        "workspace_access": job.workspace_access,
                        "archived_at": job.archived_at,
                        "updated_at": job.updated_at,
                    }
                    for job in control.store.list_jobs(args.limit)
                ]
            )
            return 0
        if args.command == "archive":
            _print_json(
                control.archive_jobs(
                    older_than_days=args.older_than_days,
                    limit=args.limit,
                    apply=args.apply,
                )
            )
            return 0
        if args.command == "slots":
            if args.slot_command == "sync":
                _print_json(control.sync_slots())
                return 0
            if args.slot_command == "list":
                _print_json(
                    control.list_slots(
                        route=args.route,
                        all_routes=args.all_routes,
                        include_deleted=args.include_deleted,
                        include_stale=args.include_stale,
                    )
                )
                return 0
            if args.slot_command == "create":
                _print_json(
                    control.create_slot(
                        args.name,
                        route=args.route,
                        branch=args.branch,
                        start_point=args.start_point,
                    )
                )
                return 0
            if args.slot_command == "bootstrap":
                route = args.route or _infer_route_from_slot_name(args.name)
                _print_json(
                    control.bootstrap_slot(
                        args.name,
                        route=route,
                        repo_path=Path(args.repo_path) if args.repo_path else None,
                        required_branch=args.required_branch,
                        slot_path=Path(args.slot_path) if args.slot_path else None,
                        branch=args.branch,
                        start_point=args.start_point,
                        create=not args.no_create,
                        ensure_ide=not args.skip_ide,
                        remove_slot_modules=not args.keep_slot_modules,
                    )
                )
                return 0
            if args.slot_command == "delete":
                _print_json(control.delete_slot(args.name, force=args.force))
                return 0
            if args.slot_command == "checkout":
                _print_json(
                    control.checkout_slot(
                        args.name,
                        branch=args.branch,
                        start_point=args.start_point,
                    )
                )
                return 0
            if args.slot_command == "ensure-module":
                _print_json(control.ensure_slot_ide_module(args.name))
                return 0
            if args.slot_command == "ensure-root-module":
                _print_json(
                    control.ensure_slot_root_ide_module(
                        remove_slot_modules=args.remove_slot_modules,
                    )
                )
                return 0
            if args.slot_command == "unload-module":
                _print_json(control.unload_slot_ide_module(args.name))
                return 0
            if args.slot_command == "unload-root-module":
                _print_json(control.unload_slot_root_ide_module())
                return 0
            if args.slot_command == "remove-module":
                _print_json(control.remove_slot_ide_module(args.name))
                return 0
            if args.slot_command == "prepare":
                _print_json(control.prepare_slot(args.name))
                return 0
            if args.slot_command == "checkpoint":
                _print_json(control.checkpoint_slot(args.name, job_id=args.job_id))
                return 0
            if args.slot_command == "cleanup":
                _print_json(
                    control.cleanup_slots(
                        max_per_route=args.max_per_route,
                        apply=args.apply,
                        force=args.force,
                        route=args.route,
                        all_routes=args.all_routes,
                    )
                )
                return 0
        if args.command == "manager":
            if args.manager_command == "accounts":
                _print_json(control.manager_accounts(model=args.model))
                return 0
            if args.manager_command == "switch-agy":
                _print_json(
                    control.switch_agy_account(
                        account_id=args.account_id,
                        email=args.email,
                        strategy=args.strategy,
                        model=args.model,
                        dry_run=not args.apply,
                    )
                )
                return 0
    except PolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SlotError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ConfigBootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AntigravityManagerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control background agent jobs.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="Path to workspaces.toml")

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_demo_parser(subparsers)

    agy_wrapper = subparsers.add_parser(
        "agy-wrapper",
        parents=[common],
        help="Preview, migrate, or recover the ACP-owned per-project AGY wrapper",
    )
    agy_wrapper_subparsers = agy_wrapper.add_subparsers(dest="agy_wrapper_command", required=True)
    agy_migrate = agy_wrapper_subparsers.add_parser(
        "migrate",
        parents=[common],
        help="Preview or replace one exact legacy/owned project wrapper",
    )
    agy_migrate.add_argument("--project", required=True, help="Absolute project root")
    agy_migrate.add_argument(
        "--expected-sha256",
        required=True,
        help="Current wrapper SHA-256 from a prior inspection",
    )
    agy_migrate.add_argument(
        "--expected-config-sha256",
        required=True,
        help="Current workspaces.toml SHA-256 from a prior inspection",
    )
    agy_migrate.add_argument(
        "--expected-agy-command",
        required=True,
        help="Exact configured control.agy_command expected by this migration",
    )
    agy_migrate.add_argument(
        "--launch-mode",
        required=True,
        choices=("managed", "unmanaged"),
        help="Explicit launch mode; it must match [control].agy_launch_mode",
    )
    agy_migrate.add_argument(
        "--adapter",
        help="Explicit managed adapter path; it must match [control].agy_proxy_launcher",
    )
    agy_migrate.add_argument(
        "--apply",
        action="store_true",
        help="Perform the per-file atomic replacement; default prints a dry-run receipt",
    )
    agy_configure = agy_wrapper_subparsers.add_parser(
        "configure",
        parents=[common],
        help="Preview or narrowly configure managed AGY launch in one workspace TOML",
    )
    agy_configure.add_argument("--project", required=True, help="Absolute configured project root")
    agy_configure.add_argument("--expected-config-sha256", required=True)
    agy_configure.add_argument("--expected-agy-command", required=True)
    agy_configure.add_argument("--adapter", required=True, help="Absolute managed adapter path")
    agy_configure.add_argument("--apply", action="store_true")
    agy_generate = agy_wrapper_subparsers.add_parser(
        "generate",
        parents=[common],
        help="Generate an ACP-owned wrapper only for an already managed configured project",
    )
    agy_generate.add_argument("--project", required=True, help="Absolute configured project root")
    agy_generate.add_argument("--expected-config-sha256", required=True)
    agy_generate.add_argument("--expected-agy-command", required=True)
    agy_generate.add_argument("--apply", action="store_true")
    agy_rollback = agy_wrapper_subparsers.add_parser(
        "rollback",
        parents=[common],
        help="Restore one migration backup only if the wrapper has its expected current hash",
    )
    agy_rollback.add_argument("--project", required=True, help="Absolute project root")
    agy_rollback.add_argument("--backup", required=True, help="Backup path printed by migrate")
    agy_rollback.add_argument(
        "--expected-current-sha256",
        required=True,
        help="Wrapper SHA-256 printed by migrate after replacement",
    )
    agy_restore_config = agy_wrapper_subparsers.add_parser(
        "restore-config",
        parents=[common],
        help="Restore a guarded ACP config backup only when its current hash still matches",
    )
    agy_restore_config.add_argument(
        "--project", required=True, help="Absolute configured project root"
    )
    agy_restore_config.add_argument(
        "--backup", required=True, help="Backup path printed by configure"
    )
    agy_restore_config.add_argument("--expected-current-sha256", required=True)

    mcp_parser = subparsers.add_parser("mcp", help="Manage MCP server")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", required=True)

    mcp_ensure = mcp_subparsers.add_parser(
        "ensure",
        parents=[common],
        help="Ensure MCP server is running for a workspace config",
    )
    mcp_ensure.add_argument("--cwd", help="Path to working directory")
    mcp_ensure.add_argument(
        "--print-url",
        action="store_true",
        help="Print derived MCP server URL",
    )
    mcp_ensure.add_argument(
        "--no-start",
        action="store_true",
        help="Only report status without starting server",
    )
    mcp_ensure.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Timeout in seconds to wait for server start",
    )

    mcp_wire = mcp_subparsers.add_parser(
        "wire",
        parents=[common],
        help="Emit or update .mcp.json in client repositories for a workspace config",
    )
    mcp_wire.add_argument("--cwd", help="Path to working directory")
    mcp_wire.add_argument(
        "--apply",
        action="store_true",
        help="Actually write .mcp.json files to client repositories (default is dry-run)",
    )
    mcp_wire.add_argument(
        "--print",
        action="store_true",
        dest="print_output",
        help="Print derived .mcp.json changes",
    )

    statuses_parser = subparsers.add_parser(
        "statuses",
        parents=[common],
        help="Print the job status vocabulary: terminal statuses and which are on-contract-capable",
    )
    statuses_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    subparsers.add_parser("smoke", parents=[common], help="Check config and local prerequisites")
    cat_parser = subparsers.add_parser(
        "model-catalog",
        parents=[common],
        help="Inspect the bounded Codex model catalog",
    )
    cat_parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when any alert of severity 'warning' is present",
    )
    routing_explain = subparsers.add_parser(
        "model-routing-explain",
        parents=[common],
        help="Explain the selected model for a named route and routing policy",
    )
    routing_explain.add_argument("policy", help="Configured Codex routing policy name")
    routing_explain.add_argument("--route", required=True, help="Configured workspace route")

    reconcile = subparsers.add_parser(
        "reconcile",
        parents=[common],
        help="Replay terminal finalization and recover orphaned jobs",
    )
    reconcile.add_argument("--job-id", help="Reconcile one job instead of all candidates")
    reconcile.add_argument(
        "--terminate-verified-runners",
        action="store_true",
        help="Terminate only runner PIDs whose durable process identity still matches",
    )

    start = subparsers.add_parser(
        "start",
        parents=[common],
        help=(
            "Start a background agent job; choose --codex-quality-tier semantically for "
            "automatic routing"
        ),
    )
    start.add_argument("--task-id", required=True)
    start.add_argument("--route", required=True)
    start.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    start.add_argument("--agy-model", help="Antigravity model to use when --backend=agy")
    start.add_argument(
        "--codex-model",
        help="Fixed Codex model; without effort uses the configured default and disables policy routing",
    )
    start.add_argument("--codex-premium-override-reason")
    start.add_argument(
        "--expected-result-status",
        choices=("partial", "completed", "blocked"),
        default="completed",
    )
    start.add_argument(
        "--controller-gate-mode", choices=("focused", "full", "none"), default="full"
    )
    start.add_argument(
        "--codex-reasoning-effort",
        help=(
            "Fixed Codex reasoning effort; requires --codex-model. A model without effort "
            "uses the configured default and disables policy adaptation; known models must "
            "use an effort declared by the current cache"
        ),
    )
    start.add_argument("--claude-model", help="Model to use when --backend=claude")
    start.add_argument(
        "--claude-reasoning-effort",
        help=(
            "Claude reasoning effort to use when --backend=claude; known catalog models "
            "must use an effort declared by the builtin Claude inventory"
        ),
    )
    start.add_argument(
        "--codex-quality-tier",
        help="Semantic Codex routing policy; leave raw model/effort unset for adaptation",
    )
    start.add_argument(
        "--codex-tool-call-budget",
        type=int,
        help="Hard per-attempt Codex tool-call budget; overrides the routing-policy default",
    )
    start.add_argument(
        "--brief-file",
        help=(
            "Install this file as the brief at the conventional path "
            "<coordination_root>/tasks/<task-id>/brief.md before launch"
        ),
    )
    start.add_argument(
        "--overwrite-brief",
        action="store_true",
        help=(
            "With --brief-file, replace an existing brief at the conventional path "
            "even when its contents differ"
        ),
    )
    start.add_argument("--slot", help="Use a managed IDE-indexed slot by name")
    start.add_argument(
        "--workspace-access",
        choices=("ide_mcp", "native"),
        help="Workspace access mode (ide_mcp or native)",
    )
    start.add_argument("--workspace-path")
    start.add_argument("--expected-branch")
    start.add_argument("--timeout-sec", type=int)
    start.add_argument("--idle-timeout-sec", type=int)
    start.add_argument("--print-timeout")
    start.add_argument("--max-restarts", type=int)
    start.add_argument("--yolo", action="store_true")
    start.add_argument("--allow-dirty", action="store_true")
    start.add_argument("--read-only", action="store_true")
    start.add_argument("--plan-id", help="Bind this job to a durable supervisor plan")
    start.add_argument(
        "--plan-task-id",
        help="Logical plan task to bind; defaults to --task-id and supports retry task IDs",
    )
    start.add_argument(
        "--wait",
        action="store_true",
        help="Wait until the job is terminal and finalization has settled before returning",
    )
    start.add_argument(
        "--wait-timeout-sec",
        type=float,
        help="Maximum seconds to wait with --wait; omitted means wait indefinitely",
    )
    start.add_argument(
        "--poll-interval-sec",
        type=float,
        default=30.0,
        help="Polling interval for --wait",
    )
    start.add_argument("--lines", type=int, default=80, help="Log tail lines returned by --wait")
    start.add_argument(
        "--live",
        action="store_true",
        help="With --wait, print status and new log tail updates to stderr while waiting",
    )

    run_job = subparsers.add_parser("run-job", parents=[common], help=argparse.SUPPRESS)
    run_job.add_argument("--job-id", required=True)
    run_job.add_argument("--worker-instance-id", required=True, help=argparse.SUPPRESS)

    status = subparsers.add_parser("status", parents=[common], help="Show job status")
    status.add_argument("job_id")

    summary = subparsers.add_parser(
        "summary",
        parents=[common],
        help="Show compact job status, guardrails, dirty state, and short log tail",
    )
    summary.add_argument("job_id")
    summary.add_argument("--lines", type=int, default=20)

    analytics = subparsers.add_parser(
        "analytics",
        parents=[common],
        help="Aggregate Codex duration, token, cache, tool, and cost metrics",
    )
    analytics.add_argument("--limit", type=int, default=100)
    analytics.add_argument("--model")
    analytics.add_argument("--reasoning-effort")
    analytics.add_argument("--backend", help="Filter to a single backend (e.g. codex, claude)")
    analytics.add_argument(
        "--valid-only",
        action="store_true",
        help="Include only completed attempts with a final usage event",
    )

    add_review_parser(subparsers, common)

    inbox = subparsers.add_parser(
        "inbox",
        help="Inspect durable terminal job and Codex subagent handoffs",
    )
    inbox_subparsers = inbox.add_subparsers(dest="inbox_command", required=True)

    inbox_list = inbox_subparsers.add_parser(
        "list",
        parents=[common],
        help="List review items; pending items are returned by default",
    )
    inbox_list.add_argument(
        "--status",
        choices=("pending", "accepted", "rejected", "all"),
        default="pending",
    )
    inbox_list.add_argument("--limit", type=int, default=50)
    inbox_list.add_argument(
        "--sync-subagents",
        action="store_true",
        help="Import recent completed Codex subagents before listing",
    )
    inbox_list.add_argument("--since-hours", type=float, default=72.0)
    inbox_list.add_argument("--max-files", type=int, default=500)
    inbox_list.add_argument("--parent-thread-id")

    inbox_show = inbox_subparsers.add_parser(
        "show",
        aliases=["get"],
        parents=[common],
        help="Show one durable review item (MCP name: agent_review_inbox_get)",
    )
    inbox_show.add_argument("item_id")

    inbox_resolve = inbox_subparsers.add_parser(
        "resolve",
        parents=[common],
        help="Resolve an inbox item without changing plan acceptance",
    )
    inbox_resolve.add_argument("item_id")
    inbox_resolve.add_argument("--decision", choices=("accepted", "rejected"), required=True)

    inbox_requalify = inbox_subparsers.add_parser(
        "requalify",
        parents=[common],
        help=(
            "Re-run controller quality gates against a pending item's durable checkpoint "
            "and rebuild its verification bundle"
        ),
    )
    inbox_requalify.add_argument("item_id")

    inbox_sync = inbox_subparsers.add_parser(
        "sync-subagents",
        parents=[common],
        help="Import recent completed Codex subagent results",
    )
    inbox_sync.add_argument("--since-hours", type=float, default=72.0)
    inbox_sync.add_argument("--max-files", type=int, default=500)
    inbox_sync.add_argument("--parent-thread-id")

    watch = subparsers.add_parser(
        "watch",
        parents=[common],
        help="Poll one or more jobs until terminal status or timeout",
        description=(
            "Without --events, watch takes exactly one job_id and prints one JSON payload "
            "(unchanged from prior releases). With --events, it accepts multiple job_ids "
            "and/or --plan/--task-glob, and streams one line per event to stdout instead. "
            "Exit codes: 0 every watched job ended on-contract; 1 at least one job ended "
            "terminal but off-contract; 3 timed out with a job still non-terminal; 4 the "
            "selection matched no jobs or job state could not be read; 2 is argparse usage "
            "error. See `agent-control statuses` for the terminal status vocabulary."
        ),
    )
    watch.add_argument(
        "job_id", nargs="*", help="Job id(s) to watch (exactly one without --events)"
    )
    watch.add_argument("--poll-interval-sec", type=float, default=30.0)
    watch.add_argument("--timeout-sec", type=float)
    watch.add_argument("--lines", type=int, default=80)
    watch.add_argument(
        "--live",
        action="store_true",
        help="Print status and new log tail updates to stderr while waiting (no --events)",
    )
    watch.add_argument(
        "--events",
        action="store_true",
        help=(
            "Stream one line per event to stdout instead of a single JSON payload; "
            "supports multiple job ids and --plan/--task-glob selection"
        ),
    )
    watch.add_argument(
        "--plan",
        dest="plan_id",
        help="Watch every job currently bound to this plan (requires --events)",
    )
    watch.add_argument(
        "--task-glob",
        dest="task_id_glob",
        help="Watch jobs whose task_id matches this SQLite GLOB pattern (requires --events)",
    )
    watch.add_argument(
        "--stale-after-sec",
        type=float,
        default=DEFAULT_STALE_AFTER_SEC,
        help="Heartbeat age in seconds before a running job is reported STALE (--events only)",
    )

    tail = subparsers.add_parser(
        "tail",
        parents=[common],
        help="Print the end of the current job log",
    )
    tail.add_argument("job_id")
    tail.add_argument("--lines", type=int, default=80)

    result = subparsers.add_parser("result", parents=[common], help="Print the task result file")
    result.add_argument("job_id")

    cancel = subparsers.add_parser(
        "cancel",
        parents=[common],
        help="Request cooperative job cancel",
    )
    cancel.add_argument("job_id")

    list_jobs = subparsers.add_parser("list", parents=[common], help="List recent jobs")
    list_jobs.add_argument("--limit", type=int, default=20)

    archive = subparsers.add_parser(
        "archive",
        parents=[common],
        help="List or archive terminal job run directories older than a threshold",
    )
    archive.add_argument("--older-than-days", type=int, default=14)
    archive.add_argument("--limit", type=int, default=50)
    archive.add_argument("--apply", action="store_true", help="Move run dirs into runs/_archive")

    accept_handoff = subparsers.add_parser(
        "accept-handoff",
        parents=[common],
        help="Atomically accept the inbox item, plan task, and root review outcome",
    )
    accept_handoff.add_argument("plan_id")
    accept_handoff.add_argument("task_id")
    accept_handoff.add_argument("--review-span-id", required=True)
    accept_handoff.add_argument("--accepted-sha")
    accept_handoff.add_argument("--attempt", type=int)
    accept_handoff.add_argument("--defects-found", type=int, default=0)
    accept_handoff.add_argument("--false-positives", type=int, default=0)
    accept_handoff.add_argument("--notes")

    add_plan_parser(subparsers, common)

    retention_gc = subparsers.add_parser(
        "gc",
        parents=[common],
        help="Prune archived plans, old events, resolved payloads, and verified checkpoint refs",
    )
    retention_gc.add_argument("--older-than-days", type=int, default=30)
    retention_gc.add_argument("--limit", type=int, default=500)
    retention_gc.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reported cleanup; default is a dry-run",
    )

    slots = subparsers.add_parser("slots", help="Manage reusable IDE-indexed worktree slots")
    slot_subparsers = slots.add_subparsers(dest="slot_command", required=True)

    slot_subparsers.add_parser("sync", parents=[common], help="Register configured slots in SQLite")
    list_slots = slot_subparsers.add_parser(
        "list",
        parents=[common],
        help="List slots, usage, and git state",
    )
    list_slots.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include deleted slot registry records",
    )
    list_slots.add_argument(
        "--include-stale",
        action="store_true",
        help="Include stale slot registry records for audit",
    )
    list_scope = list_slots.add_mutually_exclusive_group(required=True)
    list_scope.add_argument("--route", help="Inspect slots for one configured route")
    list_scope.add_argument("--all-routes", action="store_true", help="Inspect every route")

    create = slot_subparsers.add_parser("create", parents=[common], help="Create a slot worktree")
    create.add_argument("name")
    create.add_argument("--route", help="Route for a dynamic slot not listed in config")
    create.add_argument("--branch", help="Local branch name to use for the slot worktree")
    create.add_argument("--start-point", help="Git start point for a new slot branch")

    bootstrap = slot_subparsers.add_parser(
        "bootstrap",
        parents=[common],
        help="Add missing route/slot config, create the slot, and update IDE/VCS mappings",
    )
    bootstrap.add_argument("name")
    bootstrap.add_argument("--route", help="Route name; defaults to slot name prefix")
    bootstrap.add_argument(
        "--repo-path",
        help="Repository path; required when the route is not already configured",
    )
    bootstrap.add_argument(
        "--required-branch",
        help="Required route branch; defaults to existing route branch or repo current branch",
    )
    bootstrap.add_argument("--slot-path", help="Slot path; defaults to slot_root/name")
    bootstrap.add_argument("--branch", help="Local branch name to use for the slot worktree")
    bootstrap.add_argument("--start-point", help="Git start point for a new slot branch")
    bootstrap.add_argument("--no-create", action="store_true", help="Only update config")
    bootstrap.add_argument(
        "--skip-ide", action="store_true", help="Do not update IDEA module/VCS state"
    )
    bootstrap.add_argument(
        "--keep-slot-modules",
        action="store_true",
        help="Do not remove legacy per-slot IDEA module entries",
    )

    delete = slot_subparsers.add_parser("delete", parents=[common], help="Delete a slot worktree")
    delete.add_argument("name")
    delete.add_argument("--force", action="store_true", help="Allow deleting dirty or active slots")

    checkout = slot_subparsers.add_parser(
        "checkout",
        parents=[common],
        help="Checkout a clean inactive slot to a target branch",
    )
    checkout.add_argument("name")
    checkout.add_argument("--branch", required=True)
    checkout.add_argument(
        "--start-point",
        help="Create the branch from this start point if missing",
    )

    ensure_module = slot_subparsers.add_parser(
        "ensure-module",
        parents=[common],
        help="Legacy: ensure one configured slot is registered as an IDEA module",
    )
    ensure_module.add_argument("name")

    ensure_root_module = slot_subparsers.add_parser(
        "ensure-root-module",
        parents=[common],
        help="Ensure slot_root is registered as one IDEA module for all slots",
    )
    ensure_root_module.add_argument(
        "--remove-slot-modules",
        action="store_true",
        help="Remove configured legacy per-slot module entries from IDEA project/workspace state",
    )

    unload_module = slot_subparsers.add_parser(
        "unload-module",
        parents=[common],
        help="Legacy: mark a configured slot module as unloaded in IDEA workspace state",
    )
    unload_module.add_argument("name")

    slot_subparsers.add_parser(
        "unload-root-module",
        parents=[common],
        help="Mark the slot_root IDEA module as unloaded in IDEA workspace state",
    )

    remove_module = slot_subparsers.add_parser(
        "remove-module",
        parents=[common],
        help="Remove a configured legacy slot module from IDEA project/workspace state",
    )
    remove_module.add_argument("name")

    prepare = slot_subparsers.add_parser(
        "prepare",
        parents=[common],
        help="Run configured slot preparation commands when markers are missing",
    )
    prepare.add_argument("name")

    checkpoint = slot_subparsers.add_parser(
        "checkpoint",
        parents=[common],
        help="Checkpoint a terminal job's dirty slot and make it reusable",
    )
    checkpoint.add_argument("name")
    checkpoint.add_argument("--job-id", required=True)

    cleanup = slot_subparsers.add_parser(
        "cleanup",
        parents=[common],
        help="Delete least-recently-used slots above a per-route limit",
    )
    cleanup.add_argument("--max-per-route", type=int, required=True)
    cleanup.add_argument("--apply", action="store_true", help="Actually delete candidates")
    cleanup.add_argument("--force", action="store_true", help="Allow dirty slots during cleanup")
    cleanup_scope = cleanup.add_mutually_exclusive_group(required=True)
    cleanup_scope.add_argument("--route", help="Clean slots for one configured route")
    cleanup_scope.add_argument("--all-routes", action="store_true", help="Clean every route")

    manager = subparsers.add_parser(
        "manager",
        parents=[common],
        help="Inspect and switch Antigravity Manager accounts for agy",
    )
    manager_subparsers = manager.add_subparsers(dest="manager_command", required=True)
    manager_accounts = manager_subparsers.add_parser(
        "accounts",
        parents=[common],
        help="List Antigravity Manager cloud accounts and active targets",
    )
    manager_accounts.add_argument("--model", help="Show cached quota for one AGY model")
    switch_agy = manager_subparsers.add_parser(
        "switch-agy",
        parents=[common],
        help="Switch the Antigravity CLI credential target through Manager account storage",
    )
    switch_agy.add_argument("--model", help="Select CLI account by quota for this exact model")
    switch_agy.add_argument("--account-id")
    switch_agy.add_argument("--email")
    switch_agy.add_argument(
        "--strategy",
        choices=["best", "ide-active", "classic-active", "global-active", "first-active"],
        help="Account selection strategy when --account-id/--email is omitted",
    )
    switch_agy.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the agy credential store and active_cloud_account.agy",
    )

    return parser


def _handle_watch_command(control: AgentControlPlane, args: argparse.Namespace) -> int:
    job_ids: list[str] = args.job_id
    if args.events:
        return _handle_watch_events(control, args)
    if args.plan_id or args.task_id_glob:
        print("watch: --plan and --task-glob require --events", file=sys.stderr)
        return 2
    if len(job_ids) != 1:
        print("watch: exactly one job_id is required without --events", file=sys.stderr)
        return 2
    job_id = job_ids[0]
    try:
        if args.live:
            payload = _watch_job_live(
                control,
                job_id,
                poll_interval_sec=args.poll_interval_sec,
                timeout_sec=args.timeout_sec,
                log_lines=args.lines,
            )
        else:
            payload = control.watch_job(
                job_id,
                poll_interval_sec=args.poll_interval_sec,
                timeout_sec=args.timeout_sec,
                log_lines=args.lines,
                include_details=True,
            )
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    _print_json(payload)
    return _exit_code_for_watch_payload(payload)


def _exit_code_for_watch_payload(payload: dict[str, Any]) -> int:
    if payload.get("timed_out"):
        return 3
    on_contract = (
        payload.get("status") == payload.get("expected_result_status")
        and payload.get("finalization_status") == "completed"
    )
    return 0 if on_contract else 1


def _handle_watch_events(
    control: AgentControlPlane,
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    selection = WatchSelection(
        job_ids=frozenset(args.job_id),
        plan_id=args.plan_id,
        task_id_glob=args.task_id_glob,
    )
    try:
        stream = WatchEventStream(
            control.store,
            selection,
            stale_after_sec=args.stale_after_sec,
        )
    except EmptySelectionError as exc:
        _write_watch_error_line(out, str(exc))
        _write_summary_line(out, on_contract=0, off_contract=0, non_terminal=0, timed_out=False)
        return 4
    return _run_watch_events(
        stream,
        timeout_sec=args.timeout_sec,
        poll_interval_sec=args.poll_interval_sec,
        out=out,
        clock=clock,
        sleep=sleep,
    )


def _run_watch_events(
    stream: WatchEventStream,
    *,
    timeout_sec: float | None,
    poll_interval_sec: float,
    out: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    if poll_interval_sec < 0:
        raise ValueError("poll_interval_sec must be non-negative")
    if timeout_sec is not None and timeout_sec < 0:
        raise ValueError("timeout_sec must be non-negative")
    if poll_interval_sec == 0 and timeout_sec is None:
        raise ValueError("poll_interval_sec=0 requires a timeout_sec")

    started = clock()
    on_contract = 0
    off_contract = 0
    timed_out = False
    while True:
        for event in stream.tick():
            _write_event_line(out, event)
            if event.on_contract is not None:
                if event.on_contract:
                    on_contract += 1
                else:
                    off_contract += 1
        if stream.all_settled():
            break
        elapsed = clock() - started
        if timeout_sec is not None and elapsed >= timeout_sec:
            timed_out = True
            break
        sleep_for = poll_interval_sec
        if timeout_sec is not None:
            sleep_for = min(sleep_for, max(0.0, timeout_sec - elapsed))
        if sleep_for <= 0:
            continue
        sleep(sleep_for)

    non_terminal = len(stream.job_ids) - on_contract - off_contract
    _write_summary_line(
        out,
        on_contract=on_contract,
        off_contract=off_contract,
        non_terminal=non_terminal,
        timed_out=timed_out,
    )
    if timed_out:
        return 3
    if off_contract:
        return 1
    return 0


def _write_event_line(out: TextIO, event: WatchEvent) -> None:
    label = _WATCH_EVENT_KIND_LABELS[event.kind]
    parts = [event.at, label, f"job={event.task_id or event.job_id or '-'}"]
    if event.status:
        parts.append(f"status={event.status}")
    if event.finalization_status:
        parts.append(f"finalization={event.finalization_status}")
    error_text = _error_text_for_event(event)
    if error_text:
        parts.append(f"error={_compact_multiline(error_text, limit=_WATCH_ERROR_MAX_LEN)}")
    print(" ".join(parts), file=out, flush=True)


def _error_text_for_event(event: WatchEvent) -> str | None:
    """Only surface `error=` where it means something: a real failure or a watch fault.

    A completed job's ``last_error`` can carry informational text, and printing it
    unconditionally trains readers to ignore the field on jobs that are fine.
    """
    if event.kind == WATCH_ERROR:
        return event.message or event.last_error
    if event.kind == TERMINAL and event.on_contract is False:
        return event.last_error
    return None


def _write_watch_error_line(out: TextIO, message: str) -> None:
    line = (
        f"{utc_now()} WATCH-ERROR job=- "
        f"error={_compact_multiline(message, limit=_WATCH_ERROR_MAX_LEN)}"
    )
    print(line, file=out, flush=True)


def _write_summary_line(
    out: TextIO,
    *,
    on_contract: int,
    off_contract: int,
    non_terminal: int,
    timed_out: bool,
) -> None:
    total = on_contract + off_contract + non_terminal
    print(
        f"{utc_now()} SUMMARY jobs={total} on_contract={on_contract} "
        f"off_contract={off_contract} non_terminal={non_terminal} timed_out={str(timed_out).lower()}",
        file=out,
        flush=True,
    )


def _statuses_payload() -> dict[str, Any]:
    on_contract_capable = sorted(TERMINAL_STATUSES & _ON_CONTRACT_CAPABLE_STATUSES)
    always_off_contract = sorted(TERMINAL_STATUSES - _ON_CONTRACT_CAPABLE_STATUSES)
    return {
        "terminal_statuses": sorted(TERMINAL_STATUSES),
        "on_contract_capable_statuses": on_contract_capable,
        "always_off_contract_statuses": always_off_contract,
    }


def _print_statuses(*, json_output: bool) -> None:
    payload = _statuses_payload()
    if json_output:
        _print_json(payload)
        return
    capable = set(payload["on_contract_capable_statuses"])
    for status in payload["terminal_statuses"]:
        label = "on_contract_capable" if status in capable else "always_off_contract"
        print(f"{status:<28} {label}")


def _watch_job_live(
    control: AgentControlPlane,
    job_id: str,
    *,
    poll_interval_sec: float,
    timeout_sec: float | None,
    log_lines: int,
) -> dict[str, Any]:
    if poll_interval_sec < 0:
        raise ValueError("poll_interval_sec must be non-negative")
    if timeout_sec is not None and timeout_sec < 0:
        raise ValueError("timeout_sec must be non-negative")
    if poll_interval_sec == 0 and timeout_sec is None:
        raise ValueError("poll_interval_sec=0 requires a timeout_sec")

    started = time.monotonic()
    last_log_tail = ""
    while True:
        summary = control.summary_job(job_id, log_lines)
        elapsed = time.monotonic() - started
        _print_live_summary(summary, elapsed)

        log_tail = str(summary.get("log_tail") or "")
        new_log = _new_log_tail(last_log_tail, log_tail)
        if new_log:
            print("[agy-live] log tail:", file=sys.stderr, flush=True)
            print(new_log.rstrip(), file=sys.stderr, flush=True)
        last_log_tail = log_tail

        if summary["settled"]:
            summary["timed_out"] = False
            summary["watch_elapsed_sec"] = round(elapsed, 3)
            return summary

        if timeout_sec is not None and elapsed >= timeout_sec:
            summary["timed_out"] = True
            summary["watch_elapsed_sec"] = round(elapsed, 3)
            return summary

        sleep_for = poll_interval_sec
        if timeout_sec is not None:
            sleep_for = min(sleep_for, max(0.0, timeout_sec - elapsed))
        if sleep_for <= 0:
            if timeout_sec == 0:
                summary["timed_out"] = True
                summary["watch_elapsed_sec"] = round(time.monotonic() - started, 3)
                return summary
            continue
        time.sleep(sleep_for)


def _print_live_summary(summary: dict[str, Any], elapsed: float) -> None:
    parts = [
        f"elapsed={elapsed:.1f}s",
        f"status={summary.get('status')}",
        f"terminal={summary.get('terminal')}",
        f"settled={summary.get('settled')}",
        f"finalization={summary.get('finalization_status') or '-'}",
        f"backend={summary.get('backend') or '-'}",
        f"worker_pid={summary.get('worker_pid') or '-'}",
        f"runner_pid={summary.get('runner_pid') or '-'}",
        f"agy_pid={summary.get('agy_pid') or '-'}",
        f"result={summary.get('result_status') or '-'}",
    ]
    dirty_status = _compact_multiline(str(summary.get("dirty_status") or ""))
    if dirty_status:
        parts.append(f"dirty={dirty_status}")
    last_error = _compact_multiline(str(summary.get("last_error") or ""))
    if last_error:
        parts.append(f"last_error={last_error}")
    print("[agy-live] " + " ".join(parts), file=sys.stderr, flush=True)


def _new_log_tail(previous: str, current: str) -> str:
    if not current or current == previous:
        return ""
    if previous and current.startswith(previous):
        return current[len(previous) :].lstrip("\r\n")
    return current


def _compact_multiline(value: str, *, limit: int = 240) -> str:
    compact = " | ".join(line.strip() for line in value.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _job_payload(job: Any) -> dict[str, Any]:
    payload = {
        "job_id": job.job_id,
        "status": job.status,
        "expected_result_status": job.expected_result_status,
        "controller_gate_mode": job.controller_gate_mode,
        "run_dir": str(job.run_dir),
        "prompt_path": str(job.prompt_path),
        "result_path": str(job.result_path),
        "backend": job.backend,
        "agy_model": job.agy_model,
        "codex_model": job.codex_model,
        "codex_reasoning_effort": job.codex_reasoning_effort,
        "codex_quality_tier": job.codex_quality_tier,
        "codex_premium_override_reason": job.codex_premium_override_reason,
        "workspace_access": job.workspace_access,
        "worker_pid": job.worker_pid,
        "worker_instance_id": job.worker_instance_id,
        "worker_heartbeat_at": job.worker_heartbeat_at,
        "runner_pid": job.runner_pid,
        "finalization_status": job.finalization_status,
        "finalization_error": job.finalization_error,
        "finalized_at": job.finalized_at,
        "read_only": job.read_only,
        "slot_name": job.slot_name,
    }
    alerts = getattr(job, "alerts", None)
    if alerts:
        payload["alerts"] = alerts
    return payload


def _install_brief_file(
    *,
    coordination_root: Path,
    task_id: str,
    brief_file: Path,
    overwrite: bool,
) -> None:
    """Install ``brief_file`` at the conventional brief path for ``task_id``.

    Refuses to silently clobber an operator's existing brief: a conventional path
    that already holds different content is a hard error unless ``overwrite`` was
    explicitly requested.
    """
    try:
        content = brief_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read --brief-file {brief_file}: {exc}") from exc

    conventional_path = coordination_root / "tasks" / task_id / "brief.md"
    if conventional_path.is_file():
        existing = conventional_path.read_text(encoding="utf-8")
        if existing == content:
            return
        if not overwrite:
            raise ValueError(
                f"A brief already exists at {conventional_path} and differs from "
                f"--brief-file {brief_file}. Pass --overwrite-brief to replace it, "
                "or resolve the conflict manually."
            )

    conventional_path.parent.mkdir(parents=True, exist_ok=True)
    conventional_path.write_text(content, encoding="utf-8")


def _infer_route_from_slot_name(slot_name: str) -> str:
    prefix, separator, suffix = slot_name.rpartition("-")
    if separator and prefix and suffix.isdigit():
        return prefix
    return slot_name


if __name__ == "__main__":
    raise SystemExit(main())
