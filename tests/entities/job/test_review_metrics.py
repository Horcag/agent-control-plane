from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_control_plane.entities.job import AttemptMetrics, JobStore, ReviewMetricsStore
from agent_control_plane.entities.plan import PlanStore, PlanTaskDefinition
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.codex_session_usage import TokenUsage, latest_session_usage
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration


def _tick(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


@pytest.mark.parametrize(
    ("outcome", "accepted_tokens", "review_tax", "checkpoint_sha", "accepted_sha"),
    (
        ("accepted", 600, 1 / 12, None, "accepted123"),
        ("continuation_verified", 0, None, "checkpoint123", None),
    ),
)
def test_review_report_accounts_for_accepted_and_continuation_work(
    tmp_path: Path,
    outcome: str,
    accepted_tokens: int,
    review_tax: float | None,
    checkpoint_sha: str | None,
    accepted_sha: str | None,
) -> None:
    job_store = JobStore(tmp_path / "jobs.sqlite3")
    _create_job(job_store, tmp_path)
    log_path = tmp_path / "attempt.log"
    job_store.start_attempt("job-1", 1, log_path)
    job_store.finish_attempt("job-1", 1, "completed", result_status="partial", exit_code=0)
    job_store.record_attempt_metrics(
        "job-1",
        1,
        backend="codex",
        model="gpt-5.6-sol",
        reasoning_effort="low",
        metrics=_attempt_metrics(log_path),
    )

    store = ReviewMetricsStore(tmp_path / "jobs.sqlite3")
    span_id = store.start_span(
        span_id="review-1",
        name="transfer review",
        session_path=tmp_path / "rollout.jsonl",
        usage=TokenUsage(100, 80, 10, 3),
    )
    store.checkpoint(
        span_id,
        phase="review",
        usage=TokenUsage(180, 145, 20, 8),
    )
    store.attach_job(
        span_id,
        job_id="job-1",
        attempt_no=1,
        outcome=outcome,
        root_verified=True,
        checkpoint_sha=checkpoint_sha,
        accepted_sha=accepted_sha,
        defects_found=2,
        false_positives=1,
    )
    store.finish_span(span_id, usage=TokenUsage(250, 200, 30, 12))

    report = store.report(span_id)

    assert report["root_usage"]["comparable_tokens"] == 50
    assert report["accepted_agent_comparable_tokens"] == accepted_tokens
    assert report["agent_comparable_tokens"] == 600
    assert report["agent_comparable_by_outcome"][outcome] == 600
    assert report["agent_acceptance_efficiency"] == accepted_tokens / 600
    assert report["review_tax"] == review_tax
    assert report["total_comparable_tokens"] == 650
    assert report["defects_found"] == 2
    assert report["false_positives"] == 1
    assert report["checkpoints"][0]["phase"] == "review"
    assert report["job_outcomes"][0]["root_verified"] is True
    assert report["job_outcomes"][0]["metrics_attempt_no"] == 1
    assert report["job_outcomes"][0]["checkpoint_sha"] == checkpoint_sha
    assert report["job_outcomes"][0]["accepted_sha"] == accepted_sha


def test_latest_session_usage_reads_newest_snapshot_from_file_tail(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    old = _token_event("2026-07-13T10:00:00Z", 10, 8, 2, 1)
    newest = _token_event("2026-07-13T11:00:00Z", 40, 30, 7, 3)
    path.write_text(
        json.dumps(old) + "\n" + ("x" * 70_000) + "\n" + json.dumps(newest) + "\n",
        encoding="utf-8",
    )

    snapshot = latest_session_usage(path)

    assert snapshot is not None
    assert snapshot.recorded_at == "2026-07-13T11:00:00Z"
    assert snapshot.usage.comparable_tokens == 17


def test_review_metrics_v2_preserves_v1_rows_and_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    _create_job(jobs, tmp_path)
    apply_schema_migration(
        database,
        component="review_metrics_store",
        version=1,
        checksum="review-metrics-store-v1-20260715",
        migrate=ReviewMetricsStore._migrate_schema,
    )
    with sqlite3.connect(database) as db:
        db.execute(
            """
            insert into review_spans values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy",
                "Legacy",
                "rollout",
                "completed",
                "t",
                "t",
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                "t",
                "t",
            ),
        )
        db.execute(
            """
            insert into review_job_outcomes values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy", "job-1", None, "accepted", 1, "legacy-accepted", 0, 0, "legacy", "t"),
        )

    plans = PlanStore(database)
    plans.create_plan(plan_id="legacy", title="Legacy", tasks=(PlanTaskDefinition("task", "Task"),))
    plans.bind_job("legacy", "task", "job-1")
    jobs.mark_finished("job-1", "completed")
    jobs.mark_finalization_completed("job-1")
    assert plans.snapshot("legacy")["completed_tasks"] == [
        {"task_id": "task", "job_id": "job-1", "accepted_sha": "legacy-accepted"}
    ]

    store = ReviewMetricsStore(database)
    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as db:
        row = db.execute(
            "select outcome, accepted_sha, checkpoint_sha, notes from review_job_outcomes"
        ).fetchone()
        versions = db.execute(
            "select version from schema_migrations where component = 'review_metrics_store' order by version"
        ).fetchall()
    assert row == ("accepted", "legacy-accepted", None, "legacy")
    assert versions == [(1,), (2,), (3,)]


def test_review_metrics_v3_backfills_one_segment_per_existing_span(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    _create_job(jobs, tmp_path)
    apply_schema_migration(
        database,
        component="review_metrics_store",
        version=1,
        checksum="review-metrics-store-v1-20260715",
        migrate=ReviewMetricsStore._migrate_schema,
    )
    apply_schema_migration(
        database,
        component="review_metrics_store",
        version=2,
        checksum="review-metrics-store-checkpoint-sha-v2-20260719",
        migrate=ReviewMetricsStore._migrate_checkpoint_sha,
    )
    with sqlite3.connect(database) as db:
        db.execute(
            """
            insert into review_spans values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy",
                "Legacy",
                "rollout",
                "completed",
                "2026-07-15T10:00:00+00:00",
                "2026-07-15T11:00:00+00:00",
                10,
                5,
                2,
                1,
                40,
                15,
                12,
                4,
                None,
                "2026-07-15T10:00:00+00:00",
                "2026-07-15T11:00:00+00:00",
            ),
        )

    store = ReviewMetricsStore(database)
    store.initialize()
    store.initialize()

    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        segments = [
            dict(row)
            for row in db.execute(
                "select * from review_span_segments where span_id = 'legacy' order by seq"
            ).fetchall()
        ]
    with sqlite3.connect(database) as db:
        versions = db.execute(
            "select version from schema_migrations where component = 'review_metrics_store' order by version"
        ).fetchall()
    assert versions == [(1,), (2,), (3,)]
    assert len(segments) == 1
    assert segments[0]["seq"] == 0
    assert segments[0]["session_path"] == "rollout"
    assert segments[0]["started_at"] == "2026-07-15T10:00:00+00:00"
    assert segments[0]["finished_at"] == "2026-07-15T11:00:00+00:00"
    assert segments[0]["start_input_tokens"] == 10
    assert segments[0]["end_input_tokens"] == 40


def test_open_segment_appends_and_closes_prior_segment_and_report_sums_usage(
    tmp_path: Path,
) -> None:
    store = ReviewMetricsStore(tmp_path / "jobs.sqlite3")
    span_id = store.start_span(
        span_id="review-seg",
        name="multi session review",
        session_path=tmp_path / "session-a.jsonl",
        usage=TokenUsage(100, 80, 10, 3),
    )

    seq1 = store.open_segment(
        span_id,
        session_path=tmp_path / "session-b.jsonl",
        usage=TokenUsage(150, 100, 20, 5),
    )
    assert seq1 == 1

    seq2 = store.open_segment(
        span_id,
        session_path=tmp_path / "session-c.jsonl",
        usage=TokenUsage(300, 200, 40, 10),
    )
    assert seq2 == 2

    store.finish_span(span_id, usage=TokenUsage(400, 250, 60, 15))

    segments = store.list_segments(span_id)
    assert [segment["seq"] for segment in segments] == [0, 1, 2]
    assert segments[0]["finished_at"] is not None
    assert segments[1]["finished_at"] is not None
    assert segments[2]["finished_at"] is not None

    report = store.report(span_id)
    assert report["root_usage"]["input_tokens"] == (150 - 100) + (300 - 150) + (400 - 300)
    assert report["root_usage"]["output_tokens"] == (20 - 10) + (40 - 20) + (60 - 40)
    assert len(report["segments"]) == 3


def test_review_show_since_returns_only_post_cursor_observations(tmp_path: Path) -> None:
    now = datetime.fromisoformat(utc_now())
    store = ReviewMetricsStore(tmp_path / "jobs.sqlite3")
    span_id = store.start_span(
        span_id="review-cursor",
        name="cursor review",
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
    assert cursor is not None

    store.open_segment(
        span_id,
        session_path=tmp_path / "session-b.jsonl",
        usage=TokenUsage(30, 15, 6, 3),
        started_at=_tick(now, 10),
    )
    second_report = store.report(span_id, since=cursor)

    assert all(_cursor_gt(obs["cursor"], cursor) for obs in second_report["observations"])
    kinds = {obs["kind"] for obs in second_report["observations"]}
    assert "segment_closed" in kinds
    assert "segment_opened" in kinds
    assert second_report["cursor"] != cursor

    full_report = store.report(span_id)
    assert len(full_report["observations"]) > len(second_report["observations"])


def _cursor_gt(cursor: str, other: str) -> bool:
    return tuple(cursor.split("|")) > tuple(other.split("|"))


def test_stale_active_span_blocks_writes_but_get_span_reports_stale_without_raising(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = ReviewMetricsStore(database)
    span_id = store.start_span(
        span_id="review-stale",
        name="stale review",
        session_path=tmp_path / "session.jsonl",
        usage=TokenUsage(10, 5, 2, 1),
    )
    stale_started_at = "2000-01-01T00:00:00+00:00"
    with sqlite3.connect(database) as db:
        db.execute(
            "update review_span_segments set started_at = ? where span_id = ? and seq = 0",
            (stale_started_at, span_id),
        )

    span = store.get_span(span_id)
    assert span["stale"] is True

    with pytest.raises(ValueError, match="stale"):
        store.checkpoint(span_id, phase="review", usage=TokenUsage(20, 10, 4, 2))

    with pytest.raises(ValueError, match="stale"):
        store.open_segment(
            span_id,
            session_path=tmp_path / "session-b.jsonl",
            usage=TokenUsage(20, 10, 4, 2),
        )

    job_store = JobStore(database)
    _create_job(job_store, tmp_path)
    with pytest.raises(ValueError, match="stale"):
        store.attach_job(
            span_id,
            job_id="job-1",
            outcome="accepted",
            root_verified=True,
            accepted_sha="sha",
        )


def _create_job(store: JobStore, root: Path) -> None:
    store.create_job(
        job_id="job-1",
        task_id="task-1",
        route="dev",
        workspace_path=root / "workspace",
        expected_branch="slot/dev",
        config_path=root / "config.toml",
        run_dir=root / "run",
        prompt_path=root / "run" / "prompt.md",
        result_path=root / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="codex",
        codex_model="gpt-5.6-sol",
        codex_reasoning_effort="low",
    )


def _attempt_metrics(log_path: Path) -> AttemptMetrics:
    return AttemptMetrics(
        duration_sec=1.0,
        thread_id="thread-1",
        event_count=4,
        turn_completed=True,
        usage_available=True,
        input_tokens=1000,
        cached_input_tokens=600,
        output_tokens=200,
        reasoning_output_tokens=50,
        tool_calls=2,
        failed_tool_calls=0,
        error_events=0,
        tool_counts=(("mcp:idea/read_file", 2),),
        estimated_credits=1.0,
        estimated_api_usd=0.1,
        rate_card_version="test",
        event_log_path=log_path.with_suffix(".events.jsonl"),
    )


def _token_event(
    timestamp: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_output_tokens,
                }
            },
        },
    }
