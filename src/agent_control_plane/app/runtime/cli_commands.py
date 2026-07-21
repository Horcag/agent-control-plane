from __future__ import annotations

import argparse

from agent_control_plane.features.agent_runner import SUPPORTED_BACKENDS


def add_demo_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    demo = subparsers.add_parser(
        "demo", help="Run or inspect the self-contained offline pipeline demo"
    )
    demo_subparsers = demo.add_subparsers(dest="demo_command", required=True)
    demo_run = demo_subparsers.add_parser("run", help="Create and run a self-contained demo")
    demo_run.add_argument("--output", required=True, help="Empty directory for durable demo state")
    demo_run.add_argument(
        "--no-failure", action="store_true", help="Skip the injected first failure"
    )
    demo_show = demo_subparsers.add_parser("show", help="Show durable demo state")
    demo_show.add_argument("root")
    demo_accept = demo_subparsers.add_parser("accept", help="Atomically accept the demo handoff")
    demo_accept.add_argument("root")


def add_plan_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    plan = subparsers.add_parser("plan", help="Manage durable multi-job supervisor plans")
    plan_subparsers = plan.add_subparsers(dest="plan_command", required=True)

    plan_create = plan_subparsers.add_parser(
        "create", parents=[common], help="Create a plan directly or from a JSON manifest"
    )
    plan_create.add_argument("--plan-id")
    plan_create.add_argument("--title")
    plan_create.add_argument("--objective", default="")
    plan_create.add_argument("--manifest", help="JSON plan manifest with tasks and dependencies")

    plan_add = plan_subparsers.add_parser(
        "add-task", parents=[common], help="Add one logical task to an existing plan"
    )
    plan_add.add_argument("plan_id")
    plan_add.add_argument("--task-id", required=True)
    plan_add.add_argument("--title", required=True)
    plan_add.add_argument("--depends-on", action="append", default=[])
    plan_add.add_argument("--route")
    plan_add.add_argument("--brief-file")
    plan_add.add_argument("--slot")
    plan_add.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    plan_add.add_argument("--workspace-access", choices=("ide_mcp", "native"))
    plan_add.add_argument("--read-only", action="store_true")
    plan_add.add_argument("--codex-quality-tier")
    plan_add.add_argument("--expected-result-status", choices=("partial", "completed", "blocked"))
    plan_add.add_argument("--controller-gate-mode", choices=("focused", "full", "none"))
    plan_add.add_argument("--codex-premium-override-reason")
    plan_add.add_argument("--codex-model", help="Model to use when --backend=codex")
    plan_add.add_argument(
        "--codex-reasoning-effort",
        help=(
            "Codex reasoning effort to use when --backend=codex; known catalog models "
            "must use an effort declared by the current cache"
        ),
    )
    plan_add.add_argument("--claude-model", help="Model to use when --backend=claude")
    plan_add.add_argument(
        "--claude-reasoning-effort",
        help=(
            "Claude reasoning effort to use when --backend=claude; known catalog models "
            "must use an effort declared by the builtin Claude inventory"
        ),
    )

    plan_bind = plan_subparsers.add_parser(
        "bind", parents=[common], help="Bind an existing job to a logical plan task"
    )
    plan_bind.add_argument("plan_id")
    plan_bind.add_argument("task_id")
    plan_bind.add_argument("job_id")

    plan_accept = plan_subparsers.add_parser(
        "accept", parents=[common], help="Record root acceptance and unlock dependent tasks"
    )
    plan_accept.add_argument("plan_id")
    plan_accept.add_argument("task_id")
    plan_accept.add_argument("--sha")

    plan_reject = plan_subparsers.add_parser(
        "reject", parents=[common], help="Record root rejection for a plan task"
    )
    plan_reject.add_argument("plan_id")
    plan_reject.add_argument("task_id")

    plan_summary = plan_subparsers.add_parser(
        "summary",
        parents=[common],
        help="Return compact plan state and optionally only changes after a cursor",
    )
    plan_summary.add_argument("plan_id")
    plan_summary.add_argument("--since", type=int)
    plan_summary.add_argument("--event-limit", type=int, default=100)
    plan_summary.add_argument("--item-limit", type=int, default=20)

    plan_watch = plan_subparsers.add_parser(
        "watch",
        parents=[common],
        help="Long-poll until the plan cursor advances or the timeout expires",
    )
    plan_watch.add_argument("plan_id")
    plan_watch.add_argument("--since", type=int, required=True)
    plan_watch.add_argument("--poll-interval-sec", type=float, default=5.0)
    plan_watch.add_argument("--timeout-sec", type=float, default=25.0)
    plan_watch.add_argument("--event-limit", type=int, default=100)
    plan_watch.add_argument("--item-limit", type=int, default=20)

    plan_dispatch = plan_subparsers.add_parser(
        "dispatch",
        parents=[common],
        help="Claim and start dependency-ready executable tasks in one durable pass",
    )
    plan_dispatch.add_argument("plan_id")
    plan_dispatch.add_argument("--max-jobs", type=int, default=1)

    plan_run = plan_subparsers.add_parser(
        "run",
        parents=[common],
        help="Continuously dispatch, watch, and reconcile until root review is required",
    )
    plan_run.add_argument("plan_id")
    plan_run.add_argument(
        "--until-review",
        action="store_true",
        required=True,
        help="Stop before root acceptance, rejection, or explicit retry decisions",
    )
    plan_run.add_argument("--max-jobs", type=int, default=1)
    plan_run.add_argument("--poll-interval-sec", type=float, default=5.0)
    plan_run.add_argument(
        "--timeout-sec",
        type=float,
        help="Maximum supervisor runtime; omitted means run until a safe stop boundary",
    )

    plan_retry = plan_subparsers.add_parser(
        "retry", parents=[common], help="Explicitly make one failed plan task dispatchable again"
    )
    plan_retry.add_argument("plan_id")
    plan_retry.add_argument("task_id")
    plan_retry.add_argument("--brief-file")

    plan_cancel = plan_subparsers.add_parser(
        "cancel",
        parents=[common],
        help="Stop future dispatch and request cancellation of unfinished plan jobs",
    )
    plan_cancel.add_argument("plan_id")

    plan_archive = plan_subparsers.add_parser(
        "archive",
        parents=[common],
        help="Mark a completed or cancelled, fully reviewed plan as retention-eligible",
    )
    plan_archive.add_argument("plan_id")

    plan_list = plan_subparsers.add_parser("list", parents=[common], help="List recent plans")
    plan_list.add_argument("--limit", type=int, default=20)
    plan_list.add_argument("--include-archived", action="store_true")
