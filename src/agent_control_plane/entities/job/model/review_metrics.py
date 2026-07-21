from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
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
REVIEW_OUTCOMES = (
    "accepted",
    "rejected",
    "infra_failed",
    "false_positive",
    "continuation_verified",
)

# How long an active span may go without a fresh segment before it is considered
# stale (an agent/process disappeared without finishing the span).
REVIEW_SPAN_STALE_AFTER_SEC = 6 * 60 * 60


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
        apply_schema_migration(
            self.database_path,
            component="review_metrics_store",
            version=2,
            checksum="review-metrics-store-checkpoint-sha-v2-20260719",
            migrate=self._migrate_checkpoint_sha,
        )
        apply_schema_migration(
            self.database_path,
            component="review_metrics_store",
            version=3,
            checksum="review-metrics-store-span-segments-v3-20260721",
            migrate=self._migrate_span_segments,
        )

    @staticmethod
    def _migrate_span_segments(db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists review_span_segments (
                    span_id text not null references review_spans(span_id),
                    seq integer not null,
                    session_path text not null,
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
                    primary key(span_id, seq)
                )
            """
        )
        existing_spans = db.execute("select * from review_spans").fetchall()
        for span in existing_spans:
            db.execute(
                """
                insert into review_span_segments (
                    span_id, seq, session_path, started_at, finished_at,
                    start_input_tokens, start_cached_input_tokens,
                    start_output_tokens, start_reasoning_output_tokens,
                    end_input_tokens, end_cached_input_tokens,
                    end_output_tokens, end_reasoning_output_tokens
                ) values (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(span_id, seq) do nothing
                """,
                (
                    span["span_id"],
                    span["session_path"],
                    span["started_at"],
                    span["finished_at"],
                    span["start_input_tokens"],
                    span["start_cached_input_tokens"],
                    span["start_output_tokens"],
                    span["start_reasoning_output_tokens"],
                    span["end_input_tokens"],
                    span["end_cached_input_tokens"],
                    span["end_output_tokens"],
                    span["end_reasoning_output_tokens"],
                ),
            )

    @staticmethod
    def _migrate_checkpoint_sha(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(review_job_outcomes)")}
        if "checkpoint_sha" not in columns:
            db.execute("alter table review_job_outcomes add column checkpoint_sha text")

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
        span_started_at = started_at or now
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
                    span_started_at,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    notes,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                insert into review_span_segments (
                    span_id, seq, session_path, started_at,
                    start_input_tokens, start_cached_input_tokens,
                    start_output_tokens, start_reasoning_output_tokens
                ) values (?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    str(session_path),
                    span_started_at,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
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
        span = self.get_span(span_id)
        if span["stale"]:
            raise ValueError(_stale_message(span_id))
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

    def open_segment(
        self,
        span_id: str,
        *,
        session_path: Path,
        usage: TokenUsage,
        started_at: str | None = None,
    ) -> int:
        self.initialize()
        now = utc_now()
        with self._connect() as db:
            db.execute("begin immediate")
            span_row = db.execute(
                "select * from review_spans where span_id = ?", (span_id,)
            ).fetchone()
            if span_row is None:
                raise KeyError(f"Review span not found: {span_id}")
            latest = _latest_segment(db, span_id)
            if span_row["status"] != "active" or _is_stale(
                span_row["status"],
                latest["started_at"] if latest is not None else None,
                latest["finished_at"] if latest is not None else None,
            ):
                raise ValueError(_stale_message(span_id))
            segment_at = started_at or now
            next_seq = (int(latest["seq"]) + 1) if latest is not None else 0
            if latest is not None and latest["finished_at"] is None:
                db.execute(
                    """
                    update review_span_segments set
                        finished_at = ?,
                        end_input_tokens = ?, end_cached_input_tokens = ?,
                        end_output_tokens = ?, end_reasoning_output_tokens = ?
                    where span_id = ? and seq = ?
                    """,
                    (
                        segment_at,
                        usage.input_tokens,
                        usage.cached_input_tokens,
                        usage.output_tokens,
                        usage.reasoning_output_tokens,
                        span_id,
                        latest["seq"],
                    ),
                )
            db.execute(
                """
                insert into review_span_segments (
                    span_id, seq, session_path, started_at,
                    start_input_tokens, start_cached_input_tokens,
                    start_output_tokens, start_reasoning_output_tokens
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    next_seq,
                    str(session_path),
                    segment_at,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                ),
            )
        return next_seq

    def attach_job(
        self,
        span_id: str,
        *,
        job_id: str,
        outcome: str,
        attempt_no: int | None = None,
        root_verified: bool = False,
        checkpoint_sha: str | None = None,
        accepted_sha: str | None = None,
        defects_found: int = 0,
        false_positives: int = 0,
        notes: str | None = None,
    ) -> None:
        _validate_choice("outcome", outcome, REVIEW_OUTCOMES)
        if defects_found < 0 or false_positives < 0:
            raise ValueError("Review counters must be non-negative")
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            self.attach_job_in_transaction(
                db,
                span_id,
                job_id=job_id,
                outcome=outcome,
                attempt_no=attempt_no,
                root_verified=root_verified,
                checkpoint_sha=checkpoint_sha,
                accepted_sha=accepted_sha,
                defects_found=defects_found,
                false_positives=false_positives,
                notes=notes,
            )

    def attach_job_in_transaction(
        self,
        db: sqlite3.Connection,
        span_id: str,
        *,
        job_id: str,
        outcome: str,
        attempt_no: int | None = None,
        root_verified: bool = False,
        checkpoint_sha: str | None = None,
        accepted_sha: str | None = None,
        defects_found: int = 0,
        false_positives: int = 0,
        notes: str | None = None,
    ) -> None:
        _validate_choice("outcome", outcome, REVIEW_OUTCOMES)
        if defects_found < 0 or false_positives < 0:
            raise ValueError("Review counters must be non-negative")
        if outcome == "continuation_verified":
            if (
                not root_verified
                or not isinstance(checkpoint_sha, str)
                or not checkpoint_sha.strip()
            ):
                raise ValueError(
                    "Continuation verification requires a root-verified checkpoint SHA"
                )
            if accepted_sha is not None:
                raise ValueError("Continuation verification cannot set accepted_sha")
        span_row = db.execute("select * from review_spans where span_id = ?", (span_id,)).fetchone()
        if span_row is None:
            raise KeyError(f"Review span not found: {span_id}")
        latest = _latest_segment(db, span_id)
        if _is_stale(
            span_row["status"],
            latest["started_at"] if latest is not None else None,
            latest["finished_at"] if latest is not None else None,
        ):
            raise ValueError(_stale_message(span_id))
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
                checkpoint_sha, accepted_sha, defects_found, false_positives, notes, recorded_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(span_id, job_id) do update set
                attempt_no = excluded.attempt_no,
                outcome = excluded.outcome,
                root_verified = excluded.root_verified,
                checkpoint_sha = excluded.checkpoint_sha,
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
                checkpoint_sha,
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
        with self._connect() as db:
            db.execute("begin immediate")
            if (
                db.execute("select 1 from review_spans where span_id = ?", (span_id,)).fetchone()
                is None
            ):
                raise KeyError(f"Review span not found: {span_id}")
            finished = finished_at or utc_now()
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
                    finished,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    utc_now(),
                    span_id,
                ),
            )
            latest = _latest_segment(db, span_id)
            if latest is not None and latest["finished_at"] is None:
                db.execute(
                    """
                    update review_span_segments set
                        finished_at = ?,
                        end_input_tokens = ?, end_cached_input_tokens = ?,
                        end_output_tokens = ?, end_reasoning_output_tokens = ?
                    where span_id = ? and seq = ?
                    """,
                    (
                        finished,
                        usage.input_tokens,
                        usage.cached_input_tokens,
                        usage.output_tokens,
                        usage.reasoning_output_tokens,
                        span_id,
                        latest["seq"],
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
            latest = _latest_segment(db, span_id)
        span = dict(row)
        span["stale"] = _is_stale(
            span["status"],
            latest["started_at"] if latest is not None else None,
            latest["finished_at"] if latest is not None else None,
        )
        return span

    def list_segments(self, span_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """
                select * from review_span_segments
                where span_id = ? order by seq
                """,
                (span_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
        since: str | None = None,
    ) -> dict[str, Any]:
        span = self.get_span(span_id)
        with self._connect() as db:
            segments = [dict(row) for row in _segment_rows(db, span_id)]
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

        root_delta = _segments_delta(segments, live_usage)

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
        observations = _build_observations(segments, checkpoints, outcomes)
        max_cursor = observations[-1]["cursor"] if observations else None
        visible_observations = (
            observations
            if since is None
            else [obs for obs in observations if _cursor_key(obs["cursor"]) > _cursor_key(since)]
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
            "segments": segments,
            "checkpoints": checkpoints,
            "job_outcomes": outcomes,
            "observations": visible_observations,
            "cursor": max_cursor,
            "since": since,
        }

    def observations_since(
        self,
        span_id: str,
        *,
        since: str | None = None,
    ) -> dict[str, Any]:
        self.get_span(span_id)
        with self._connect() as db:
            segments = [dict(row) for row in _segment_rows(db, span_id)]
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
        observations = _build_observations(segments, checkpoints, outcomes)
        max_cursor = observations[-1]["cursor"] if observations else None
        visible_observations = (
            observations
            if since is None
            else [obs for obs in observations if _cursor_key(obs["cursor"]) > _cursor_key(since)]
        )
        return {
            "span_id": span_id,
            "observations": visible_observations,
            "cursor": max_cursor,
            "since": since,
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


def _segment_rows(db: sqlite3.Connection, span_id: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            "select * from review_span_segments where span_id = ? order by seq",
            (span_id,),
        ).fetchall()
    )


def _latest_segment(db: sqlite3.Connection, span_id: str) -> sqlite3.Row | None:
    return db.execute(
        "select * from review_span_segments where span_id = ? order by seq desc limit 1",
        (span_id,),
    ).fetchone()


def _segments_delta(
    segments: list[dict[str, Any]],
    live_usage: TokenUsage | None,
) -> TokenUsage:
    total_input = total_cached = total_output = total_reasoning = 0
    for segment in segments:
        start = _usage_from_row(segment, "start")
        end = _optional_usage_from_row(segment, "end") or live_usage or start
        delta = end.delta(start)
        total_input += delta.input_tokens
        total_cached += delta.cached_input_tokens
        total_output += delta.output_tokens
        total_reasoning += delta.reasoning_output_tokens
    return TokenUsage(total_input, total_cached, total_output, total_reasoning)


def _is_stale(
    status: str,
    latest_segment_started_at: str | None,
    latest_segment_finished_at: str | None,
) -> bool:
    if status != "active" or latest_segment_finished_at is not None:
        return False
    if latest_segment_started_at is None:
        return False
    started = datetime.fromisoformat(latest_segment_started_at)
    now = datetime.fromisoformat(utc_now())
    elapsed = (now - started).total_seconds()
    return elapsed > REVIEW_SPAN_STALE_AFTER_SEC


def _stale_message(span_id: str) -> str:
    return f"review span {span_id} is stale; start a new span or resume with open_segment"


def _build_observations(
    segments: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for segment in segments:
        seq = int(segment["seq"])
        observations.append(
            {
                "kind": "segment_opened",
                "recorded_at": segment["started_at"],
                "seq": seq,
                "session_path": segment["session_path"],
            }
        )
        if segment["finished_at"] is not None:
            observations.append(
                {
                    "kind": "segment_closed",
                    "recorded_at": segment["finished_at"],
                    "seq": seq,
                    "session_path": segment["session_path"],
                }
            )
    for index, checkpoint in enumerate(checkpoints):
        observations.append(
            {
                "kind": "checkpoint",
                "recorded_at": checkpoint["recorded_at"],
                "seq": index,
                "phase": checkpoint["phase"],
            }
        )
    for index, outcome in enumerate(outcomes):
        observations.append(
            {
                "kind": "job_outcome",
                "recorded_at": outcome["recorded_at"],
                "seq": index,
                "job_id": outcome["job_id"],
                "outcome": outcome["outcome"],
            }
        )
    observations.sort(key=lambda obs: (obs["recorded_at"], obs["kind"], obs["seq"]))
    for observation in observations:
        observation["cursor"] = (
            f"{observation['recorded_at']}|{observation['kind']}|{observation['seq']:010d}"
        )
    return observations


def _cursor_key(cursor: str) -> tuple[str, str, int]:
    try:
        recorded_at, kind, seq = cursor.split("|")
        return (recorded_at, kind, int(seq))
    except ValueError as exc:
        raise ValueError(f"Malformed review observation cursor: {cursor!r}") from exc
