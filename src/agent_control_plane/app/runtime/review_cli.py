from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import (
    REVIEW_OUTCOMES,
    REVIEW_PHASES,
    ReviewMetricsStore,
)
from agent_control_plane.shared.claude_session_usage import latest_claude_session_usage
from agent_control_plane.shared.codex_session_usage import (
    SessionUsageSnapshot,
    TokenUsage,
    latest_session_usage,
)


def add_review_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    review = subparsers.add_parser(
        "review",
        help="Account for root investigation, review, repair, and integration work",
    )
    commands = review.add_subparsers(dest="review_command", required=True)

    start = commands.add_parser("start", parents=[common], help="Start a root review span")
    start.add_argument("--name", help="Human-readable review name")
    start.add_argument("--session", help="Codex rollout JSONL path")
    start.add_argument("--marker", help="Existing root-span marker JSON to import")
    start.add_argument("--span-id", help="Stable explicit span id")
    start.add_argument("--notes")

    checkpoint = commands.add_parser(
        "checkpoint",
        parents=[common],
        help="Record the current cumulative usage at a review phase boundary",
    )
    checkpoint.add_argument("span_id")
    checkpoint.add_argument("phase", choices=REVIEW_PHASES)

    attach = commands.add_parser(
        "attach",
        parents=[common],
        help="Attach the root acceptance outcome for an agent job",
    )
    attach.add_argument("span_id")
    attach.add_argument("job_id")
    attach.add_argument("outcome", choices=REVIEW_OUTCOMES)
    attach.add_argument("--attempt", type=int)
    attach.add_argument("--root-verified", action="store_true")
    attach.add_argument("--accepted-sha")
    attach.add_argument("--defects-found", type=int, default=0)
    attach.add_argument("--false-positives", type=int, default=0)
    attach.add_argument("--notes")

    finish = commands.add_parser("finish", parents=[common], help="Finish a review span")
    finish.add_argument("span_id")

    show = commands.add_parser("show", parents=[common], help="Show review cost and outcomes")
    show.add_argument("span_id")

    list_spans = commands.add_parser("list", parents=[common], help="List recent review spans")
    list_spans.add_argument("--limit", type=int, default=20)


def handle_review_command(args: argparse.Namespace, *, database_path: Path) -> Any:
    store = ReviewMetricsStore(database_path)
    command = args.review_command
    if command == "start":
        marker = _load_marker(Path(args.marker)) if args.marker else None
        session_path = _session_path(args.session, marker)
        snapshot = (
            _marker_snapshot(marker) if marker is not None else _required_snapshot(session_path)
        )
        name = args.name or _marker_text(marker, "task_id")
        if not name:
            raise ValueError("review start requires --name unless --marker contains task_id")
        span_id = store.start_span(
            name=name,
            session_path=session_path,
            usage=snapshot.usage,
            started_at=_marker_text(marker, "started_at"),
            span_id=args.span_id,
            notes=args.notes,
        )
        return store.report(span_id, live_usage=snapshot.usage)
    if command == "checkpoint":
        span = store.get_span(args.span_id)
        snapshot = _required_snapshot(Path(span["session_path"]))
        store.checkpoint(
            args.span_id,
            phase=args.phase,
            usage=snapshot.usage,
            recorded_at=snapshot.recorded_at,
        )
        return store.report(args.span_id, live_usage=snapshot.usage)
    if command == "attach":
        store.attach_job(
            args.span_id,
            job_id=args.job_id,
            outcome=args.outcome,
            attempt_no=args.attempt,
            root_verified=args.root_verified,
            accepted_sha=args.accepted_sha,
            defects_found=args.defects_found,
            false_positives=args.false_positives,
            notes=args.notes,
        )
        return _live_report(store, args.span_id)
    if command == "finish":
        span = store.get_span(args.span_id)
        snapshot = _required_snapshot(Path(span["session_path"]))
        store.finish_span(
            args.span_id,
            usage=snapshot.usage,
            finished_at=snapshot.recorded_at,
        )
        return store.report(args.span_id)
    if command == "show":
        return _live_report(store, args.span_id)
    if command == "list":
        return store.list_spans(limit=args.limit)
    raise ValueError(f"Unknown review command: {command}")


def _live_report(store: ReviewMetricsStore, span_id: str) -> dict[str, Any]:
    span = store.get_span(span_id)
    live_usage = None
    if span["status"] == "active":
        snapshot = _session_snapshot(Path(span["session_path"]))
        live_usage = snapshot.usage if snapshot is not None else None
    return store.report(span_id, live_usage=live_usage)


def _session_snapshot(path: Path) -> SessionUsageSnapshot | None:
    """Read a cumulative usage snapshot from a Codex rollout or Claude transcript."""
    snapshot = latest_session_usage(path)
    if snapshot is not None:
        return snapshot
    return latest_claude_session_usage(path)


def _load_marker(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read review marker {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Review marker must contain a JSON object: {path}")
    return value


def _session_path(explicit: str | None, marker: dict[str, Any] | None) -> Path:
    raw = explicit or _marker_text(marker, "rollout_path")
    if not raw:
        raise ValueError("review start requires --session or a marker with rollout_path")
    return Path(raw).expanduser().resolve(strict=False)


def _marker_snapshot(marker: dict[str, Any]) -> SessionUsageSnapshot:
    usage = TokenUsage.from_mapping(marker.get("usage"))
    if usage is None:
        raise ValueError("Review marker does not contain a valid usage object")
    return SessionUsageSnapshot(
        usage=usage,
        recorded_at=_marker_text(marker, "source_event_at"),
    )


def _required_snapshot(path: Path) -> SessionUsageSnapshot:
    snapshot = _session_snapshot(path)
    if snapshot is None:
        raise ValueError(f"No Codex token_count or Claude usage snapshot found in {path}")
    return snapshot


def _marker_text(marker: dict[str, Any] | None, key: str) -> str | None:
    if marker is None:
        return None
    value = marker.get(key)
    return value if isinstance(value, str) and value else None
