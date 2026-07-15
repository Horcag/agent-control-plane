from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.codex_session_usage import TokenUsage
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database

REVIEW_PHASES = (
    "investigation",
    "issuance_monitoring",
    "review",
    "repair",
    "integration",
)
REVIEW_OUTCOMES = ("accepted", "rejected", "infra_failed", "false_positive")


class ReviewMetricsStore:
    """Durable accounting for root investigation, review, and integration work."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="review_metrics_store",
            version=1,
            checksum="review-metrics-store-v1-20260715",
            migrate=self._migrate_schema,
        )

    @staticmethod
    def _migrate_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists review_spans (
                    span_id text primary key,
                    name text not null,
                    session_path text not null,
                    status text not null,
                    started_at text not null,
                    finished_at text,
                    start_input_tokens integer not null,
                    start_cached_input_tokens integer not null,
                    start_output_tokens integer not null,
                    start_reasoning_output_tokens integer not null,
                    end_input_tokens integer,
                    end_cached_input_tokens integer,
                    end_output_tokens integer,
                    end_reasoning_output_tokens integer,
                    notes text,
                    created_at text not null,
                    updated_at text not null
                )
            """
        )
        db.execute(
            """
            create table if not exists review_checkpoints (
                    span_id text not null references review_spans(span_id),
                    phase text not null,
                    recorded_at text not null,
                    input_tokens integer not null,
                    cached_input_tokens integer not null,
                    output_tokens integer not null,
                    reasoning_output_tokens integer not null,
                    primary key(span_id, phase)
                )
            """
        )
        db.execute(
            """
            create table if not exists review_job_outcomes (
                    span_id text not null references review_spans(span_id),
                    job_id text not null references jobs(job_id),
                    attempt_no integer,
                    outcome text not null,
                    root_verified integer not null,
                    accepted_sha text,
                    defects_found integer not null,
                    false_positives integer not null,
                    notes text,
                    recorded_at text not null,
                    primary key(span_id, job_id)
                )
            """
        )

    def start_span(
        self,
        *,
        name: str,
        session_path: Path,
        usage: TokenUsage,
        started_at: str | None = None,
        span_id: str | None = None,
        notes: str | None = None,
    ) -> str:
        self.initialize()
        now = utc_now()
        span_id = span_id or _new_span_id(name)
        with self._connect() as db:
            db.execute(
                """
                insert into review_spans (
                    span_id, name, session_path, status, started_at,
                    start_input_tokens, start_cached_input_tokens,
                    start_output_tokens, start_reasoning_output_tokens,
                    notes, created_at, updated_at
                ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    name,
                    str(session_path),
                    started_at or now,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    notes,
                    now,
                    now,
                ),
            )
        return span_id

    def checkpoint(
        self,
        span_id: str,
        *,
        phase: str,
        usage: TokenUsage,
        recorded_at: str | None = None,
    ) -> None:
        _validate_choice("phase", phase, REVIEW_PHASES)
        self.get_span(span_id)
        with self._connect() as db:
            db.execute(
                """
                insert into review_checkpoints (
                    span_id, phase, recorded_at, input_tokens,
                    cached_input_tokens, output_tokens, reasoning_output_tokens
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(span_id, phase) do update set
                    recorded_at = excluded.recorded_at,
                    input_tokens = excluded.input_tokens,
                    cached_input_tokens = excluded.cached_input_tokens,
                    output_tokens = excluded.output_tokens,
                    reasoning_output_tokens = excluded.reasoning_output_tokens
                """,
                (
                    span_id,
                    phase,
                    recorded_at or utc_now(),
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                ),
            )

    def attach_job(
        self,
        span_id: str,
        *,
        job_id: str,
        outcome: str,
        attempt_no: int | None = None,
        root_verified: bool = False,
        accepted_sha: str | None = None,
        defects_found: int = 0,
        false_positives: int = 0,
        notes: str | None = None,
    ) -> None:
        _validate_choice("outcome", outcome, REVIEW_OUTCOMES)
        if defects_found < 0 or false_positives < 0:
            raise ValueError("Review counters must be non-negative")
        self.get_span(span_id)
        with self._connect() as db:
            if db.execute("select 1 from jobs where job_id = ?", (job_id,)).fetchone() is None:
                raise KeyError(f"Job not found: {job_id}")
            if (
                attempt_no is not None
                and db.execute(
                    "select 1 from attempts where job_id = ? and attempt_no = ?",
                    (job_id, attempt_no),
                ).fetchone()
                is None
            ):
                raise KeyError(f"Attempt not found: {job_id}#{attempt_no}")
            db.execute(
                """
                insert into review_job_outcomes (
                    span_id, job_id, attempt_no, outcome, root_verified,
                    accepted_sha, defects_found, false_positives, notes, recorded_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(span_id, job_id) do update set
                    attempt_no = excluded.attempt_no,
                    outcome = excluded.outcome,
                    root_verified = excluded.root_verified,
                    accepted_sha = excluded.accepted_sha,
                    defects_found = excluded.defects_found,
                    false_positives = excluded.false_positives,
                    notes = excluded.notes,
                    recorded_at = excluded.recorded_at
                """,
                (
                    span_id,
                    job_id,
                    attempt_no,
                    outcome,
                    int(root_verified),
                    accepted_sha,
                    defects_found,
                    false_positives,
                    notes,
                    utc_now(),
                ),
            )

    def finish_span(
        self,
        span_id: str,
        *,
        usage: TokenUsage,
        finished_at: str | None = None,
    ) -> None:
        self.get_span(span_id)
        with self._connect() as db:
            db.execute(
                """
                update review_spans set
                    status = 'completed', finished_at = ?,
                    end_input_tokens = ?, end_cached_input_tokens = ?,
                    end_output_tokens = ?, end_reasoning_output_tokens = ?,
                    updated_at = ?
                where span_id = ?
                """,
                (
                    finished_at or utc_now(),
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    utc_now(),
                    span_id,
                ),
            )

    def get_span(self, span_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "select * from review_spans where span_id = ?",
                (span_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Review span not found: {span_id}")
        return dict(row)

    def list_spans(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                "select * from review_spans order by started_at desc limit ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def report(
        self,
        span_id: str,
        *,
        live_usage: TokenUsage | None = None,
    ) -> dict[str, Any]:
        span = self.get_span(span_id)
        start = _usage_from_row(span, "start")
        end = _optional_usage_from_row(span, "end") or live_usage or start
        root_delta = end.delta(start)
        with self._connect() as db:
            checkpoints = [
                dict(row)
                for row in db.execute(
                    """
                    select * from review_checkpoints
                    where span_id = ? order by recorded_at
                    """,
                    (span_id,),
                ).fetchall()
            ]
            outcomes = [
                self._outcome_payload(db, row)
                for row in db.execute(
                    """
                    select * from review_job_outcomes
                    where span_id = ? order by recorded_at
                    """,
                    (span_id,),
                ).fetchall()
            ]

        accepted_comparable = sum(
            int(item["agent_comparable_tokens"] or 0)
            for item in outcomes
            if item["outcome"] == "accepted"
        )
        agent_comparable = sum(int(item["agent_comparable_tokens"] or 0) for item in outcomes)
        agent_by_outcome = {
            outcome: sum(
                int(item["agent_comparable_tokens"] or 0)
                for item in outcomes
                if item["outcome"] == outcome
            )
            for outcome in REVIEW_OUTCOMES
        }
        review_tax = (
            root_delta.comparable_tokens / accepted_comparable if accepted_comparable > 0 else None
        )
        return {
            "span": span,
            "root_usage": root_delta.as_dict(),
            "agent_comparable_tokens": agent_comparable,
            "accepted_agent_comparable_tokens": accepted_comparable,
            "agent_comparable_by_outcome": agent_by_outcome,
            "agent_acceptance_efficiency": (
                accepted_comparable / agent_comparable if agent_comparable > 0 else None
            ),
            "review_tax": review_tax,
            "total_comparable_tokens": root_delta.comparable_tokens + agent_comparable,
            "defects_found": sum(int(item["defects_found"]) for item in outcomes),
            "false_positives": sum(int(item["false_positives"]) for item in outcomes),
            "checkpoints": checkpoints,
            "job_outcomes": outcomes,
        }

    @staticmethod
    def _outcome_payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["root_verified"] = bool(payload["root_verified"])
        metrics = None
        if payload["attempt_no"] is not None:
            metrics = db.execute(
                """
                select * from attempt_metrics
                where job_id = ? and attempt_no = ?
                """,
                (payload["job_id"], payload["attempt_no"]),
            ).fetchone()
        else:
            metrics = db.execute(
                """
                select * from attempt_metrics
                where job_id = ? order by attempt_no desc limit 1
                """,
                (payload["job_id"],),
            ).fetchone()
        if metrics is None:
            payload["agent_comparable_tokens"] = None
            payload["metrics_attempt_no"] = None
        else:
            payload["agent_comparable_tokens"] = max(
                0,
                int(metrics["input_tokens"]) - int(metrics["cached_input_tokens"]),
            ) + int(metrics["output_tokens"])
            payload["metrics_attempt_no"] = int(metrics["attempt_no"])
        return payload

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with control_database(self.database_path) as db:
            yield db


def _usage_from_row(row: dict[str, Any], prefix: str) -> TokenUsage:
    return TokenUsage(
        input_tokens=int(row[f"{prefix}_input_tokens"]),
        cached_input_tokens=int(row[f"{prefix}_cached_input_tokens"]),
        output_tokens=int(row[f"{prefix}_output_tokens"]),
        reasoning_output_tokens=int(row[f"{prefix}_reasoning_output_tokens"]),
    )


def _optional_usage_from_row(row: dict[str, Any], prefix: str) -> TokenUsage | None:
    if row[f"{prefix}_input_tokens"] is None:
        return None
    return _usage_from_row(row, prefix)


def _new_span_id(name: str) -> str:
    slug = "-".join(part for part in name.lower().replace("_", "-").split("-") if part)
    return f"review-{slug[:48] or 'root'}-{uuid.uuid4().hex[:8]}"


def _validate_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValueError(f"Unsupported {name} {value!r}; expected one of: {', '.join(choices)}")
