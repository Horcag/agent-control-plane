from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agent_control_plane.app.runtime.cli import _build_parser
from agent_control_plane.app.runtime.review_cli import handle_review_command
from agent_control_plane.entities.job import ReviewMetricsStore
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.codex_session_usage import TokenUsage


def _tick(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_review_show_parser_accepts_since_cursor() -> None:
    args = _build_parser().parse_args(
        ["review", "show", "review-1", "--since", "2026-07-21T00:00:00+00:00|checkpoint|0000000000"]
    )

    assert args.review_command == "show"
    assert args.span_id == "review-1"
    assert args.since == "2026-07-21T00:00:00+00:00|checkpoint|0000000000"


def test_review_show_parser_since_defaults_to_none() -> None:
    args = _build_parser().parse_args(["review", "show", "review-1"])

    assert args.since is None


def test_handle_review_show_since_returns_only_post_cursor_observations(tmp_path: Path) -> None:
    now = datetime.fromisoformat(utc_now())
    database = tmp_path / "jobs.sqlite3"
    store = ReviewMetricsStore(database)
    span_id = store.start_span(
        span_id="review-cli-cursor",
        name="cli cursor review",
        session_path=tmp_path / "session-a.jsonl",
        usage=TokenUsage(10, 5, 2, 1),
        started_at=_tick(now, 0),
    )
    store.checkpoint(
        span_id,
        phase="review",
        usage=TokenUsage(20, 10, 4, 2),
        recorded_at=_tick(now, 5),
    )
    first_report = store.report(span_id)
    cursor = first_report["cursor"]

    store.open_segment(
        span_id,
        session_path=tmp_path / "session-b.jsonl",
        usage=TokenUsage(30, 15, 6, 3),
        started_at=_tick(now, 10),
    )

    args = _build_parser().parse_args(["review", "show", span_id, "--since", cursor])
    result = handle_review_command(args, database_path=database)

    assert all(
        tuple(obs["cursor"].split("|")) > tuple(cursor.split("|")) for obs in result["observations"]
    )
    assert result["cursor"] != cursor

    full_args = _build_parser().parse_args(["review", "show", span_id])
    full_result = handle_review_command(full_args, database_path=database)
    assert len(full_result["observations"]) > len(result["observations"])
