from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_control_plane.entities.plan import PlanExecutionSpec, PlanTaskDefinition


def handle_plan_command(control: Any, args: argparse.Namespace) -> Any:
    if args.plan_command == "create":
        manifest = read_plan_manifest(args.manifest) if args.manifest else {}
        plan_id = args.plan_id or manifest.get("plan_id")
        title = args.title or manifest.get("title")
        objective = args.objective or manifest.get("objective", "")
        if not plan_id or not title:
            raise ValueError(
                "plan create requires --plan-id and --title, or a manifest containing both"
            )
        return control.create_plan(
            plan_id=str(plan_id),
            title=str(title),
            objective=str(objective),
            tasks=plan_task_definitions(manifest.get("tasks", [])),
        )
    if args.plan_command == "add-task":
        return control.add_plan_task(
            args.plan_id,
            task_id=args.task_id,
            title=args.title,
            depends_on=tuple(args.depends_on),
            execution=cli_plan_execution_spec(args),
        )
    if args.plan_command == "bind":
        return control.bind_plan_job(args.plan_id, args.task_id, args.job_id)
    if args.plan_command == "accept":
        return control.accept_plan_task(args.plan_id, args.task_id, accepted_sha=args.sha)
    if args.plan_command == "reject":
        return control.reject_plan_task(args.plan_id, args.task_id)
    if args.plan_command == "summary":
        return control.plan_snapshot(
            args.plan_id,
            since=args.since,
            event_limit=args.event_limit,
            item_limit=args.item_limit,
        )
    if args.plan_command == "watch":
        return control.watch_plan(
            args.plan_id,
            since=args.since,
            poll_interval_sec=args.poll_interval_sec,
            timeout_sec=args.timeout_sec,
            event_limit=args.event_limit,
            item_limit=args.item_limit,
        )
    if args.plan_command == "dispatch":
        return control.dispatch_plan(args.plan_id, max_jobs=args.max_jobs)
    if args.plan_command == "run":
        return control.run_plan_until_review(
            args.plan_id,
            max_jobs=args.max_jobs,
            poll_interval_sec=args.poll_interval_sec,
            timeout_sec=args.timeout_sec,
        )
    if args.plan_command == "retry":
        return control.retry_plan_task(
            args.plan_id,
            args.task_id,
            brief_override=read_retry_brief(args.brief_file),
        )
    if args.plan_command == "cancel":
        return control.cancel_plan(args.plan_id)
    if args.plan_command == "archive":
        return control.archive_plan(args.plan_id)
    if args.plan_command == "list":
        return control.list_plans(args.limit, include_archived=args.include_archived)
    raise ValueError(f"Unknown plan command: {args.plan_command}")


def read_plan_manifest(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read plan manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Plan manifest must be a JSON object")
    return payload


def plan_task_definitions(payload: Any) -> tuple[PlanTaskDefinition, ...]:
    if not isinstance(payload, list):
        raise ValueError("Plan manifest tasks must be a JSON array")
    definitions = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each plan task must be a JSON object")
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
                execution=plan_execution_spec(item.get("execution")),
            )
        )
    return tuple(definitions)


def plan_execution_spec(payload: Any) -> PlanExecutionSpec | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Plan task execution must be a JSON object")
    read_only = payload.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError("Plan task execution read_only must be a boolean")
    return PlanExecutionSpec(
        route=str(payload.get("route", "")),
        brief=str(payload.get("brief", "")),
        slot=optional_manifest_text(payload.get("slot")),
        backend=optional_manifest_text(payload.get("backend")),
        workspace_access=optional_manifest_text(payload.get("workspace_access")),
        read_only=read_only,
        codex_quality_tier=optional_manifest_text(payload.get("codex_quality_tier")),
        codex_model=optional_manifest_text(payload.get("codex_model")),
        codex_reasoning_effort=optional_manifest_text(payload.get("codex_reasoning_effort")),
    )


def cli_plan_execution_spec(args: argparse.Namespace) -> PlanExecutionSpec | None:
    values = (
        args.route,
        args.brief_file,
        args.slot,
        args.backend,
        args.workspace_access,
        args.codex_quality_tier,
        args.codex_model,
        args.codex_reasoning_effort,
    )
    if not any(value is not None for value in values) and not args.read_only:
        return None
    if not args.route or not args.brief_file:
        raise ValueError("Executable plan tasks require both --route and --brief-file")
    try:
        brief = Path(args.brief_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read plan brief {args.brief_file}: {exc}") from exc
    return PlanExecutionSpec(
        route=args.route,
        brief=brief,
        slot=args.slot,
        backend=args.backend,
        workspace_access=args.workspace_access,
        read_only=args.read_only,
        codex_quality_tier=args.codex_quality_tier,
        codex_model=args.codex_model,
        codex_reasoning_effort=args.codex_reasoning_effort,
    )


def read_retry_brief(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read retry brief {path}: {exc}") from exc


def optional_manifest_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
