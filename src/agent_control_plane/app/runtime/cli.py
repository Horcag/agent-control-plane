from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

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
from agent_control_plane.features.agent_runner import SUPPORTED_BACKENDS
from agent_control_plane.features.antigravity_accounts import AntigravityManagerError
from agent_control_plane.features.slot_lifecycle import ConfigBootstrapError, SlotError


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
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
    try:
        control = AgentControlPlane.from_config_path(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "smoke":
            _print_json(control.smoke())
            return 0
        if args.command == "reconcile":
            _print_json(
                control.reconcile_jobs(
                    args.job_id,
                    terminate_verified_runners=args.terminate_verified_runners,
                )
            )
            return 0
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
            job = control.start_job(
                StartOptions(
                    task_id=args.task_id,
                    route=args.route,
                    backend=args.backend,
                    agy_model=args.agy_model,
                    codex_model=args.codex_model,
                    codex_reasoning_effort=args.codex_reasoning_effort,
                    codex_quality_tier=args.codex_quality_tier,
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
            _print_json(payload)
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
            if args.inbox_command == "show":
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
            if args.live:
                _print_json(
                    _watch_job_live(
                        control,
                        args.job_id,
                        poll_interval_sec=args.poll_interval_sec,
                        timeout_sec=args.timeout_sec,
                        log_lines=args.lines,
                    )
                )
            else:
                _print_json(
                    control.watch_job(
                        args.job_id,
                        poll_interval_sec=args.poll_interval_sec,
                        timeout_sec=args.timeout_sec,
                        log_lines=args.lines,
                        include_details=True,
                    )
                )
            return 0
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
                _print_json(control.list_slots(include_deleted=args.include_deleted))
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
                    )
                )
                return 0
        if args.command == "manager":
            if args.manager_command == "accounts":
                _print_json(control.manager_accounts())
                return 0
            if args.manager_command == "switch-agy":
                _print_json(
                    control.switch_agy_account(
                        account_id=args.account_id,
                        email=args.email,
                        strategy=args.strategy,
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

    subparsers.add_parser("smoke", parents=[common], help="Check config and local prerequisites")

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

    start = subparsers.add_parser("start", parents=[common], help="Start a background agent job")
    start.add_argument("--task-id", required=True)
    start.add_argument("--route", required=True)
    start.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    start.add_argument("--agy-model", help="Antigravity model to use when --backend=agy")
    start.add_argument("--codex-model", help="Model to use when --backend=codex")
    start.add_argument(
        "--codex-reasoning-effort",
        help=(
            "Codex reasoning effort to use when --backend=codex; managed "
            "Luna/Terra/Sol profiles accept none, low, medium, high, or xhigh"
        ),
    )
    start.add_argument(
        "--codex-quality-tier",
        choices=("mechanical", "balanced", "deep"),
        help="Opt into a quality tier; deep remains the safe default",
    )
    start.add_argument(
        "--codex-tool-call-budget",
        type=int,
        help="Hard per-attempt Codex tool-call budget; overrides the quality-tier default",
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
        help="Wait until the job reaches a terminal status before returning",
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
        parents=[common],
        help="Show one durable review item",
    )
    inbox_show.add_argument("item_id")

    inbox_resolve = inbox_subparsers.add_parser(
        "resolve",
        parents=[common],
        help="Resolve an inbox item without changing plan acceptance",
    )
    inbox_resolve.add_argument("item_id")
    inbox_resolve.add_argument("--decision", choices=("accepted", "rejected"), required=True)

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
        help="Poll a job until terminal status or timeout",
    )
    watch.add_argument("job_id")
    watch.add_argument("--poll-interval-sec", type=float, default=30.0)
    watch.add_argument("--timeout-sec", type=float)
    watch.add_argument("--lines", type=int, default=80)
    watch.add_argument(
        "--live",
        action="store_true",
        help="Print status and new log tail updates to stderr while waiting",
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

    manager = subparsers.add_parser(
        "manager",
        parents=[common],
        help="Inspect and switch Antigravity Manager accounts for agy",
    )
    manager_subparsers = manager.add_subparsers(dest="manager_command", required=True)
    manager_subparsers.add_parser(
        "accounts",
        parents=[common],
        help="List Antigravity Manager cloud accounts and active targets",
    )
    switch_agy = manager_subparsers.add_parser(
        "switch-agy",
        parents=[common],
        help="Switch the Antigravity CLI credential target through Manager account storage",
    )
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

        if summary["terminal"]:
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
    return {
        "job_id": job.job_id,
        "status": job.status,
        "run_dir": str(job.run_dir),
        "prompt_path": str(job.prompt_path),
        "result_path": str(job.result_path),
        "backend": job.backend,
        "agy_model": job.agy_model,
        "codex_model": job.codex_model,
        "codex_reasoning_effort": job.codex_reasoning_effort,
        "codex_quality_tier": job.codex_quality_tier,
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


def _infer_route_from_slot_name(slot_name: str) -> str:
    prefix, separator, suffix = slot_name.rpartition("-")
    if separator and prefix and suffix.isdigit():
        return prefix
    return slot_name


if __name__ == "__main__":
    raise SystemExit(main())
