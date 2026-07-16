from __future__ import annotations

import json
from pathlib import Path

from agent_control_plane.entities.job import AttemptMetrics, JobStore, ReviewMetricsStore
from agent_control_plane.shared.codex_session_usage import TokenUsage, latest_session_usage


def test_review_report_counts_root_tax_and_accepted_agent_work(tmp_path: Path) -> None:
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
        outcome="accepted",
        root_verified=True,
        accepted_sha="abc123",
        defects_found=2,
        false_positives=1,
    )
    store.finish_span(span_id, usage=TokenUsage(250, 200, 30, 12))

    report = store.report(span_id)

    assert report["root_usage"]["comparable_tokens"] == 50
    assert report["accepted_agent_comparable_tokens"] == 600
    assert report["agent_comparable_tokens"] == 600
    assert report["agent_comparable_by_outcome"]["accepted"] == 600
    assert report["agent_acceptance_efficiency"] == 1.0
    assert report["review_tax"] == 50 / 600
    assert report["total_comparable_tokens"] == 650
    assert report["defects_found"] == 2
    assert report["false_positives"] == 1
    assert report["checkpoints"][0]["phase"] == "review"
    assert report["job_outcomes"][0]["root_verified"] is True
    assert report["job_outcomes"][0]["metrics_attempt_no"] == 1


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
