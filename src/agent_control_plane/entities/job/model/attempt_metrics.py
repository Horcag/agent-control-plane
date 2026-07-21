from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.shared.clock import utc_now


@dataclass(frozen=True)
class AttemptMetrics:
    duration_sec: float
    thread_id: str | None
    event_count: int
    turn_completed: bool
    usage_available: bool
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    tool_calls: int
    failed_tool_calls: int
    error_events: int
    tool_counts: tuple[tuple[str, int], ...]
    estimated_credits: float | None
    estimated_api_usd: float | None
    rate_card_version: str
    event_log_path: Path | None
    cache_creation_input_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def cache_hit_ratio(self) -> float:
        if self.input_tokens <= 0:
            return 0.0
        return self.cached_input_tokens / self.input_tokens

    @property
    def output_tokens_per_sec(self) -> float:
        if self.duration_sec <= 0:
            return 0.0
        return self.output_tokens / self.duration_sec


def create_attempt_metrics_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        create table if not exists attempt_metrics (
            job_id text not null references jobs(job_id),
            attempt_no integer not null,
            backend text not null,
            model text,
            reasoning_effort text,
            duration_sec real not null,
            thread_id text,
            event_count integer not null,
            turn_completed integer not null,
            usage_available integer not null,
            input_tokens integer not null,
            cached_input_tokens integer not null,
            output_tokens integer not null,
            reasoning_output_tokens integer not null,
            tool_calls integer not null,
            failed_tool_calls integer not null,
            error_events integer not null,
            tool_counts_json text not null,
            estimated_credits real,
            estimated_api_usd real,
            rate_card_version text not null,
            event_log_path text,
            created_at text not null,
            cache_creation_input_tokens integer not null default 0,
            primary key(job_id, attempt_no)
        )
        """
    )
    columns = {row["name"] for row in db.execute("pragma table_info(attempt_metrics)")}
    if "thread_id" not in columns:
        db.execute("alter table attempt_metrics add column thread_id text")
    if "cache_creation_input_tokens" not in columns:
        db.execute(
            "alter table attempt_metrics "
            "add column cache_creation_input_tokens integer not null default 0"
        )


def save_attempt_metrics(
    db: sqlite3.Connection,
    *,
    job_id: str,
    attempt_no: int,
    backend: str,
    model: str | None,
    reasoning_effort: str | None,
    metrics: AttemptMetrics,
) -> None:
    db.execute(
        """
        insert or replace into attempt_metrics (
            job_id, attempt_no, backend, model, reasoning_effort,
            duration_sec, thread_id, event_count, turn_completed, usage_available,
            input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
            tool_calls, failed_tool_calls, error_events, tool_counts_json,
            estimated_credits, estimated_api_usd, rate_card_version,
            event_log_path, created_at, cache_creation_input_tokens
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            attempt_no,
            backend,
            model,
            reasoning_effort,
            metrics.duration_sec,
            metrics.thread_id,
            metrics.event_count,
            int(metrics.turn_completed),
            int(metrics.usage_available),
            metrics.input_tokens,
            metrics.cached_input_tokens,
            metrics.output_tokens,
            metrics.reasoning_output_tokens,
            metrics.tool_calls,
            metrics.failed_tool_calls,
            metrics.error_events,
            json.dumps(dict(metrics.tool_counts), sort_keys=True),
            metrics.estimated_credits,
            metrics.estimated_api_usd,
            metrics.rate_card_version,
            str(metrics.event_log_path) if metrics.event_log_path else None,
            utc_now(),
            metrics.cache_creation_input_tokens,
        ),
    )


def load_attempt_metrics(
    db: sqlite3.Connection,
    *,
    job_id: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    backend: str | None = None,
    valid_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        select
            m.*, a.status, a.result_status, a.exit_code, j.task_id, j.route
        from attempt_metrics as m
        join attempts as a
          on a.job_id = m.job_id and a.attempt_no = m.attempt_no
        join jobs as j on j.job_id = m.job_id
        where (? is null or m.job_id = ?)
          and (? is null or m.model = ?)
          and (? is null or m.reasoning_effort = ?)
          and (? is null or m.backend = ?)
          and (? = 0 or (a.status = 'completed' and m.usage_available = 1 and m.turn_completed = 1))
        order by m.created_at desc, m.attempt_no desc
        limit ?
        """,
        (
            job_id,
            job_id,
            model,
            model,
            reasoning_effort,
            reasoning_effort,
            backend,
            backend,
            int(valid_only),
            max(1, limit),
        ),
    ).fetchall()
    return [_metrics_row(row) for row in rows]


def build_metrics_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["model"], row["reasoning_effort"])
        groups.setdefault(key, []).append(row)

    group_payloads = []
    for (model, effort), grouped_rows in sorted(
        groups.items(),
        key=lambda item: (item[0][0] or "", item[0][1] or ""),
    ):
        group_payloads.append(
            {
                "model": model,
                "reasoning_effort": effort,
                **_aggregate(grouped_rows),
            }
        )

    return {
        "rate_card_versions": sorted({row["rate_card_version"] for row in rows}),
        "totals": _aggregate(rows),
        "groups": group_payloads,
        "attempts": rows,
    }


def _metrics_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["turn_completed"] = bool(payload["turn_completed"])
    payload["usage_available"] = bool(payload["usage_available"])
    payload["tool_counts"] = json.loads(payload.pop("tool_counts_json"))
    return payload


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    completed = sum(row["status"] == "completed" for row in rows)
    result_completed = sum(row["result_status"] == "completed" for row in rows)
    partial = sum(row["result_status"] == "partial" for row in rows)
    blocked = sum(row["result_status"] == "blocked" for row in rows)
    known_result_statuses = result_completed + partial + blocked
    durations = sorted(float(row["duration_sec"]) for row in rows)
    input_tokens = sum(int(row["input_tokens"]) for row in rows)
    cached_tokens = sum(int(row["cached_input_tokens"]) for row in rows)
    cache_creation_tokens = sum(int(row.get("cache_creation_input_tokens") or 0) for row in rows)
    output_tokens = sum(int(row["output_tokens"]) for row in rows)
    reasoning_tokens = sum(int(row["reasoning_output_tokens"]) for row in rows)
    total_duration = sum(durations)
    credit_values = [
        float(row["estimated_credits"]) for row in rows if row["estimated_credits"] is not None
    ]
    usd_values = [
        float(row["estimated_api_usd"]) for row in rows if row["estimated_api_usd"] is not None
    ]

    return {
        "attempt_count": count,
        "completed_attempt_count": completed,
        "result_completed_attempt_count": result_completed,
        "partial_attempt_count": partial,
        "blocked_attempt_count": blocked,
        "unknown_result_status_attempt_count": count - known_result_statuses,
        "usage_available_attempt_count": sum(bool(row["usage_available"]) for row in rows),
        "success_rate": (
            result_completed / known_result_statuses if known_result_statuses else None
        ),
        "total_duration_sec": total_duration,
        "avg_duration_sec": statistics.fmean(durations) if durations else 0.0,
        "p50_duration_sec": statistics.median(durations) if durations else 0.0,
        "p95_duration_sec": _nearest_rank(durations, 0.95),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "cache_creation_input_tokens": cache_creation_tokens,
        "uncached_input_tokens": max(0, input_tokens - cached_tokens),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "cache_hit_ratio": cached_tokens / input_tokens if input_tokens else 0.0,
        "output_tokens_per_sec": output_tokens / total_duration if total_duration else 0.0,
        "tool_calls": sum(int(row["tool_calls"]) for row in rows),
        "failed_tool_calls": sum(int(row["failed_tool_calls"]) for row in rows),
        "error_events": sum(int(row["error_events"]) for row in rows),
        "estimated_credits": sum(credit_values) if credit_values else None,
        "estimated_api_usd": sum(usd_values) if usd_values else None,
        "avg_credits": statistics.fmean(credit_values) if credit_values else None,
    }


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return values[index]
