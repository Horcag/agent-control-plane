from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job.model.attempt_metrics import (
    AttemptMetrics,
    build_metrics_report,
    create_attempt_metrics_table,
    load_attempt_metrics,
    save_attempt_metrics,
)
from agent_control_plane.shared.agent_backends import AGY_BACKEND, normalize_backend
from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database

JOB_COLUMNS = {
    "status",
    "agy_model",
    "codex_model",
    "codex_reasoning_effort",
    "codex_tool_call_budget",
    "workspace_access",
    "run_dir",
    "prompt_path",
    "log_path",
    "worker_pid",
    "runner_pid",
    "runner_process_identity",
    "agy_pid",
    "started_at",
    "finished_at",
    "last_error",
    "read_only",
    "cancel_requested",
    "slot_name",
    "archived_at",
    "worker_instance_id",
    "worker_heartbeat_at",
    "finalization_status",
    "finalization_error",
    "finalized_at",
}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    task_id: str
    route: str
    workspace_path: Path
    expected_branch: str
    status: str
    config_path: Path
    run_dir: Path
    prompt_path: Path
    result_path: Path
    log_path: Path | None
    worker_pid: int | None
    runner_pid: int | None
    runner_process_identity: str | None
    agy_pid: int | None
    backend: str
    agy_model: str | None
    codex_model: str | None
    codex_reasoning_effort: str | None
    codex_quality_tier: str | None
    codex_premium_override_reason: str | None
    codex_tool_call_budget: int | None
    workspace_access: str
    archived_at: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    timeout_sec: int
    idle_timeout_sec: int
    print_timeout: str
    max_restarts: int
    yolo: bool
    allow_dirty: bool
    read_only: bool
    last_error: str | None
    cancel_requested: bool
    slot_name: str | None
    worker_instance_id: str | None
    worker_heartbeat_at: str | None
    finalization_status: str
    finalization_error: str | None
    finalized_at: str | None


class JobStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="job_store",
            version=1,
            checksum="job-store-v1-20260715",
            migrate=self._migrate_schema,
        )
        apply_schema_migration(
            self.database_path,
            component="job_store",
            version=2,
            checksum="job-store-runner-process-identity-v2-20260715",
            migrate=self._migrate_runner_process_identity,
        )
        apply_schema_migration(
            self.database_path,
            component="job_store",
            version=3,
            checksum="job-store-premium-override-reason-v3-20260718",
            migrate=self._migrate_premium_override_reason,
        )

    @staticmethod
    def _migrate_premium_override_reason(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(jobs)").fetchall()}
        if "codex_premium_override_reason" not in columns:
            db.execute("alter table jobs add column codex_premium_override_reason text")

    @staticmethod
    def _migrate_runner_process_identity(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(jobs)").fetchall()}
        if "runner_process_identity" not in columns:
            db.execute("alter table jobs add column runner_process_identity text")

    @classmethod
    def _migrate_schema(cls, db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists jobs (
                    job_id text primary key,
                    task_id text not null,
                    route text not null,
                    workspace_path text not null,
                    expected_branch text not null,
                    status text not null,
                    config_path text not null,
                    run_dir text not null,
                    prompt_path text not null,
                    result_path text not null,
                    log_path text,
                    worker_pid integer,
                    runner_pid integer,
                    agy_pid integer,
                    backend text not null default 'agy',
                    agy_model text,
                    codex_model text,
                    codex_reasoning_effort text,
                    codex_quality_tier text,
                    codex_tool_call_budget integer,
                    workspace_access text not null default 'ide_mcp',
                    archived_at text,
                    created_at text not null,
                    updated_at text not null,
                    started_at text,
                    finished_at text,
                    timeout_sec integer not null,
                    idle_timeout_sec integer not null,
                    print_timeout text not null,
                    max_restarts integer not null,
                    yolo integer not null,
                    allow_dirty integer not null,
                    read_only integer not null default 0,
                    slot_name text,
                    last_error text,
                    cancel_requested integer not null default 0,
                    worker_instance_id text,
                    worker_heartbeat_at text,
                    finalization_status text not null default 'not_started',
                    finalization_error text,
                    finalized_at text
                )
            """
        )
        db.execute(
            """
            create table if not exists attempts (
                    id integer primary key autoincrement,
                    job_id text not null references jobs(job_id),
                    attempt_no integer not null,
                    status text not null,
                    result_status text,
                    started_at text not null,
                    finished_at text,
                    log_path text not null,
                    exit_code integer,
                    message text,
                    unique(job_id, attempt_no)
                )
            """
        )
        db.execute(
            """
            create table if not exists events (
                    id integer primary key autoincrement,
                    job_id text not null references jobs(job_id),
                    created_at text not null,
                    level text not null,
                    message text not null
                )
            """
        )
        db.execute(
            """
            create table if not exists orphaned_events (
                original_event_id integer primary key,
                job_id text not null,
                created_at text not null,
                level text not null,
                message text not null,
                quarantined_at text not null,
                reason text not null
            )
            """
        )
        create_attempt_metrics_table(db)
        cls._ensure_columns(db)
        cls._ensure_attempt_columns(db)
        cls._quarantine_orphan_events(db)

    def create_job(
        self,
        *,
        job_id: str | None = None,
        task_id: str,
        route: str,
        workspace_path: Path,
        expected_branch: str,
        config_path: Path,
        run_dir: Path,
        prompt_path: Path,
        result_path: Path,
        timeout_sec: int,
        idle_timeout_sec: int,
        print_timeout: str,
        max_restarts: int,
        yolo: bool,
        allow_dirty: bool,
        read_only: bool,
        backend: str = AGY_BACKEND,
        agy_model: str | None = None,
        codex_model: str | None = None,
        codex_reasoning_effort: str | None = None,
        codex_quality_tier: str | None = None,
        codex_premium_override_reason: str | None = None,
        codex_tool_call_budget: int | None = None,
        workspace_access: str = "ide_mcp",
        slot_name: str | None = None,
    ) -> JobRecord:
        self.initialize()
        backend = normalize_backend(backend)
        job_id = job_id or new_job_id(task_id)
        now = utc_now()
        with self._connect() as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select job_id from jobs where task_id = ? limit 1",
                (task_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Task ID already exists: {task_id} (job {existing['job_id']})")
            db.execute(
                """
                insert into jobs (
                    job_id, task_id, route, workspace_path, expected_branch, status,
                    config_path, run_dir, prompt_path, result_path,
                    backend, agy_model, codex_model, codex_reasoning_effort,
                    codex_quality_tier, codex_premium_override_reason,
                    codex_tool_call_budget, workspace_access,
                    created_at, updated_at, timeout_sec, idle_timeout_sec,
                    print_timeout, max_restarts, yolo, allow_dirty, read_only, slot_name
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    task_id,
                    route,
                    str(workspace_path),
                    expected_branch,
                    "created",
                    str(config_path),
                    str(run_dir),
                    str(prompt_path),
                    str(result_path),
                    backend,
                    agy_model,
                    codex_model,
                    codex_reasoning_effort,
                    codex_quality_tier,
                    codex_premium_override_reason,
                    codex_tool_call_budget,
                    workspace_access,
                    now,
                    now,
                    timeout_sec,
                    idle_timeout_sec,
                    print_timeout,
                    max_restarts,
                    int(yolo),
                    int(allow_dirty),
                    int(read_only),
                    slot_name,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        self.initialize()
        with self._connect() as db:
            row = db.execute("select * from jobs where job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        return _job_from_row(row)

    def update_job(self, job_id: str, **values: Any) -> JobRecord:
        invalid = sorted(set(values) - JOB_COLUMNS)
        if invalid:
            raise ValueError(f"Unsupported job columns: {', '.join(invalid)}")
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [_to_sql(value) for value in values.values()]
        params.append(job_id)
        with self._connect() as db:
            db.execute(f"update jobs set {assignments} where job_id = ?", params)  # nosec
        return self.get_job(job_id)

    def mark_finished(self, job_id: str, status: str, last_error: str | None = None) -> JobRecord:
        now = utc_now()
        self.initialize()
        with self._connect() as db:
            db.execute(
                """
                update jobs
                set status = ?, finished_at = ?, last_error = ?,
                    worker_pid = null, runner_pid = null, runner_process_identity = null,
                    agy_pid = null,
                    worker_instance_id = null, worker_heartbeat_at = null,
                    finalization_status = 'pending', finalization_error = null,
                    finalized_at = null, updated_at = ?
                where job_id = ? and finished_at is null
                """,
                (status, now, last_error, now, job_id),
            )
        return self.get_job(job_id)

    def mark_finished_by_worker(
        self,
        job_id: str,
        worker_instance_id: str,
        status: str,
        last_error: str | None = None,
    ) -> JobRecord | None:
        """Persist terminal state only while the caller still owns the worker identity."""
        normalized = worker_instance_id.strip()
        if not normalized:
            raise ValueError("worker_instance_id must not be empty")
        now = utc_now()
        self.initialize()
        with self._connect() as db:
            cursor = db.execute(
                """
                update jobs
                set status = ?, finished_at = ?, last_error = ?,
                    worker_pid = null, runner_pid = null, runner_process_identity = null,
                    agy_pid = null,
                    worker_instance_id = null, worker_heartbeat_at = null,
                    finalization_status = 'pending', finalization_error = null,
                    finalized_at = null, updated_at = ?
                where job_id = ? and worker_instance_id = ? and finished_at is null
                """,
                (status, now, last_error, now, job_id, normalized),
            )
        if cursor.rowcount != 1:
            return None
        return self.get_job(job_id)

    def assign_worker(
        self,
        job_id: str,
        worker_instance_id: str,
        *,
        worker_pid: int | None = None,
        heartbeat_at: str | None = None,
    ) -> JobRecord:
        normalized = worker_instance_id.strip()
        if not normalized:
            raise ValueError("worker_instance_id must not be empty")
        now = heartbeat_at or utc_now()
        self.initialize()
        with self._connect() as db:
            cursor = db.execute(
                """
                update jobs
                set status = 'queued', worker_instance_id = ?, worker_heartbeat_at = ?,
                    worker_pid = ?, updated_at = ?
                where job_id = ? and finished_at is null
                    and (worker_instance_id is null or worker_instance_id = ?)
                """,
                (normalized, now, worker_pid, utc_now(), job_id, normalized),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Cannot assign worker to terminal, missing, or already-owned job: {job_id}"
                )
        return self.get_job(job_id)

    def update_for_worker(
        self,
        job_id: str,
        worker_instance_id: str,
        **values: Any,
    ) -> bool:
        """Apply a worker mutation only if its durable identity is still current."""
        invalid = sorted(set(values) - JOB_COLUMNS)
        if invalid:
            raise ValueError(f"Unsupported job columns: {', '.join(invalid)}")
        if not values:
            raise ValueError("At least one job column must be updated")
        normalized = worker_instance_id.strip()
        if not normalized:
            raise ValueError("worker_instance_id must not be empty")
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [_to_sql(value) for value in values.values()]
        params.extend((job_id, normalized))
        self.initialize()
        with self._connect() as db:
            cursor = db.execute(
                f"""
                update jobs set {assignments}
                where job_id = ? and worker_instance_id = ? and finished_at is null
                """,  # nosec B608
                params,
            )
        return cursor.rowcount == 1

    def heartbeat_worker(
        self,
        job_id: str,
        worker_instance_id: str,
        *,
        worker_pid: int,
        status: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "worker_heartbeat_at": utc_now(),
            "worker_pid": worker_pid,
        }
        if status is not None:
            values["status"] = status
        return self.update_for_worker(job_id, worker_instance_id, **values)

    def mark_finalization_completed(self, job_id: str) -> JobRecord:
        return self.update_job(
            job_id,
            finalization_status="completed",
            finalization_error=None,
            finalized_at=utc_now(),
        )

    def mark_finalization_failed(self, job_id: str, error: str) -> JobRecord:
        return self.update_job(
            job_id,
            finalization_status="failed",
            finalization_error=error,
            finalized_at=None,
        )

    def prepare_finalization_replay(self, job_id: str) -> JobRecord:
        return self.update_job(
            job_id,
            finalization_status="pending",
            finalization_error=None,
            finalized_at=None,
        )

    def request_cancel(self, job_id: str) -> JobRecord:
        return self.update_job(job_id, cancel_requested=True, status="cancel_requested")

    def cancel_requested(self, job_id: str) -> bool:
        return self.get_job(job_id).cancel_requested

    def start_attempt(self, job_id: str, attempt_no: int, log_path: Path) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert or replace into attempts (job_id, attempt_no, status, started_at, log_path)
                values (?, ?, ?, ?, ?)
                """,
                (job_id, attempt_no, "running", utc_now(), str(log_path)),
            )

    def finish_attempt(
        self,
        job_id: str,
        attempt_no: int,
        status: str,
        *,
        result_status: str | None = None,
        exit_code: int | None = None,
        message: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                update attempts
                set status = ?, result_status = ?, finished_at = ?, exit_code = ?, message = ?
                where job_id = ? and attempt_no = ?
                """,
                (
                    status,
                    result_status,
                    utc_now(),
                    exit_code,
                    message,
                    job_id,
                    attempt_no,
                ),
            )

    def finish_running_attempts(
        self,
        job_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        message: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                update attempts
                set status = ?, finished_at = ?, exit_code = ?, message = ?
                where job_id = ? and finished_at is null
                """,
                (status, utc_now(), exit_code, message, job_id),
            )

    def record_attempt_metrics(
        self,
        job_id: str,
        attempt_no: int,
        *,
        backend: str,
        model: str | None,
        reasoning_effort: str | None,
        metrics: AttemptMetrics,
    ) -> None:
        self.initialize()
        with self._connect() as db:
            save_attempt_metrics(
                db,
                job_id=job_id,
                attempt_no=attempt_no,
                backend=backend,
                model=model,
                reasoning_effort=reasoning_effort,
                metrics=metrics,
            )

    def attempt_metrics(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as db:
            return load_attempt_metrics(db, job_id=job_id, limit=limit)

    def metrics_report(
        self,
        *,
        limit: int = 100,
        model: str | None = None,
        reasoning_effort: str | None = None,
        backend: str | None = None,
        valid_only: bool = False,
    ) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            rows = load_attempt_metrics(
                db,
                model=model,
                reasoning_effort=reasoning_effort,
                backend=backend,
                valid_only=valid_only,
                limit=limit,
            )
        return build_metrics_report(rows)

    def record_routing_decision(self, job_id: str, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise TypeError("Routing decision payload must be a mapping")
        if payload.get("event") != "routing_decision":
            raise ValueError("Routing decision payload must have event='routing_decision'")
        try:
            message = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Routing decision payload must be JSON-serializable") from exc
        self.initialize()
        self.add_event(job_id, "routing_decision", message)

    def routing_decision(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                """
                select message from events
                where job_id = ? and level = 'routing_decision'
                order by id desc limit 1
                """,
                (job_id,),
            ).fetchone()
        return _decode_routing_payload(row["message"] if row is not None else None)

    def record_explicit_premium_launch(self, job_id: str, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise TypeError("Explicit premium launch payload must be a mapping")
        if payload.get("event") != "explicit_premium_launch":
            raise ValueError(
                "Explicit premium launch payload must have event='explicit_premium_launch'"
            )
        try:
            message = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Explicit premium launch payload must be JSON-serializable") from exc
        self.initialize()
        self.add_event(job_id, "explicit_premium_launch", message)

    def explicit_premium_launch(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                """
                select message from events
                where job_id = ? and level = 'explicit_premium_launch'
                order by id desc limit 1
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["message"])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def routing_history(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("routing history limit must be positive")
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """
                select m.job_id, m.model, m.reasoning_effort, m.duration_sec,
                       m.input_tokens, m.cached_input_tokens, m.output_tokens,
                       m.usage_available, m.turn_completed,
                       a.status as attempt_status, a.result_status, j.route,
                       e.message as routing_message
                from attempt_metrics as m
                join attempts as a
                  on a.job_id = m.job_id and a.attempt_no = m.attempt_no
                join jobs as j on j.job_id = m.job_id
                left join events as e on e.id = (
                    select max(event.id) from events as event
                    where event.job_id = m.job_id and event.level = 'routing_decision'
                )
                order by m.created_at desc, m.attempt_no desc
                limit ?
                """,
                (limit,),
            ).fetchall()
            tables = {
                str(row["name"])
                for row in db.execute(
                    "select name from sqlite_master where type = 'table'"
                ).fetchall()
            }
            reviews = _routing_review_outcomes(db) if "review_job_outcomes" in tables else {}
            plans = _routing_plan_outcomes(db) if "plan_tasks" in tables else {}
        return [_routing_history_row(row, reviews=reviews, plans=plans) for row in rows]

    def add_event(self, job_id: str, level: str, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "insert into events (job_id, created_at, level, message) values (?, ?, ?, ?)",
                (job_id, utc_now(), level, message),
            )

    def recent_events(self, job_id: str, limit: int = 20) -> list[tuple[str, str, str]]:
        with self._connect() as db:
            rows = db.execute(
                """
                select created_at, level, message
                from events
                where job_id = ?
                order by id desc
                limit ?
                """,
                (job_id, limit),
            ).fetchall()
        return [(row["created_at"], row["level"], row["message"]) for row in reversed(rows)]

    def list_jobs(self, limit: int = 20) -> list[JobRecord]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                "select * from jobs order by created_at desc limit ?",
                (limit,),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def reconciliation_candidates(self) -> list[JobRecord]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """
                select * from jobs
                where finished_at is null or finalization_status != 'completed'
                order by created_at
                """
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with control_database(self.database_path) as db:
            yield db

    @staticmethod
    def _ensure_columns(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(jobs)").fetchall()}
        if "read_only" not in columns:
            db.execute("alter table jobs add column read_only integer not null default 0")
        if "slot_name" not in columns:
            db.execute("alter table jobs add column slot_name text")
        if "runner_pid" not in columns:
            db.execute("alter table jobs add column runner_pid integer")
        if "backend" not in columns:
            db.execute("alter table jobs add column backend text not null default 'agy'")
        if "agy_model" not in columns:
            db.execute("alter table jobs add column agy_model text")
        if "codex_model" not in columns:
            db.execute("alter table jobs add column codex_model text")
        if "codex_reasoning_effort" not in columns:
            db.execute("alter table jobs add column codex_reasoning_effort text")
        if "codex_quality_tier" not in columns:
            db.execute("alter table jobs add column codex_quality_tier text")
        if "codex_tool_call_budget" not in columns:
            db.execute("alter table jobs add column codex_tool_call_budget integer")
        if "archived_at" not in columns:
            db.execute("alter table jobs add column archived_at text")
        if "workspace_access" not in columns:
            db.execute(
                "alter table jobs add column workspace_access text not null default 'ide_mcp'"
            )
        if "worker_instance_id" not in columns:
            db.execute("alter table jobs add column worker_instance_id text")
        if "worker_heartbeat_at" not in columns:
            db.execute("alter table jobs add column worker_heartbeat_at text")
        if "finalization_status" not in columns:
            db.execute(
                "alter table jobs add column finalization_status text not null default 'completed'"
            )
        if "finalization_error" not in columns:
            db.execute("alter table jobs add column finalization_error text")
        if "finalized_at" not in columns:
            db.execute("alter table jobs add column finalized_at text")

    @staticmethod
    def _ensure_attempt_columns(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(attempts)").fetchall()}
        if "result_status" not in columns:
            db.execute("alter table attempts add column result_status text")

    @staticmethod
    def _quarantine_orphan_events(db: sqlite3.Connection) -> None:
        db.execute(
            """
            insert or ignore into orphaned_events (
                original_event_id, job_id, created_at, level, message,
                quarantined_at, reason
            )
            select events.id, events.job_id, events.created_at, events.level,
                   events.message, ?, 'missing_parent_job'
            from events
            left join jobs on jobs.job_id = events.job_id
            where jobs.job_id is null
            """,
            (utc_now(),),
        )
        db.execute(
            """
            delete from events
            where not exists (
                select 1 from jobs where jobs.job_id = events.job_id
            )
            """
        )
        violations = db.execute("pragma foreign_key_check").fetchall()
        if violations:
            details = ", ".join(
                f"{row['table']}:{row['rowid']}->{row['parent']}" for row in violations[:5]
            )
            raise RuntimeError(
                f"Unresolved ACP foreign-key violations after job migration: {details}"
            )


def new_job_id(task_id: str) -> str:
    return f"{_slug(task_id)}-{uuid.uuid4().hex[:8]}"


def _slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "-" for char in value]
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:80] or "job"


def _to_sql(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _decode_routing_payload(value: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return (
        payload
        if isinstance(payload, dict) and payload.get("event") == "routing_decision"
        else None
    )


def _routing_review_outcomes(db: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    rows = db.execute(
        "select job_id, outcome, defects_found from review_job_outcomes "
        "where root_verified = 1 and outcome in ('accepted', 'rejected') "
        "order by recorded_at, span_id"
    ).fetchall()
    return {
        str(row["job_id"]): (str(row["outcome"]), max(0, int(row["defects_found"]))) for row in rows
    }


def _routing_plan_outcomes(db: sqlite3.Connection) -> dict[str, str]:
    rows = db.execute(
        "select job_id, review_status from plan_tasks "
        "where job_id is not null and review_status in ('accepted', 'rejected') "
        "order by updated_at, plan_id, task_id"
    ).fetchall()
    return {str(row["job_id"]): str(row["review_status"]) for row in rows}


def _routing_history_row(
    row: sqlite3.Row,
    *,
    reviews: dict[str, tuple[str, int]],
    plans: dict[str, str],
) -> dict[str, Any]:
    payload = _decode_routing_payload(row["routing_message"])
    metadata = payload or {}
    catalog = metadata.get("catalog")
    catalog = catalog if isinstance(catalog, dict) else {}
    review = reviews.get(str(row["job_id"]))
    root_outcome = review[0] if review is not None else plans.get(str(row["job_id"]))
    raw_values = tuple(
        _routing_float(row[field])
        for field in ("duration_sec", "input_tokens", "cached_input_tokens", "output_tokens")
    )
    metrics_valid = bool(row["usage_available"]) and bool(row["turn_completed"])
    metrics_valid = metrics_valid and all(
        math.isfinite(value) and value >= 0 for value in raw_values
    )
    metrics_valid = metrics_valid and raw_values[2] <= raw_values[1]
    metrics_valid = metrics_valid and bool(_payload_text(row["model"]))
    metrics_valid = metrics_valid and bool(_payload_text(row["reasoning_effort"]))
    policy_name = _payload_text(
        metadata.get("policy_name") or metadata.get("requested_policy") or metadata.get("policy")
    )
    selection_source = _payload_text(metadata.get("selection_source"))
    return {
        "model": _payload_text(row["model"]),
        "reasoning_effort": _payload_text(row["reasoning_effort"]),
        "attempt_status": row["attempt_status"],
        "result_status": row["result_status"],
        "input_tokens": row["input_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "output_tokens": row["output_tokens"],
        "duration_sec": raw_values[0],
        "metrics_valid": metrics_valid,
        "route": _payload_text(row["route"]),
        "policy_name": policy_name,
        "task_class": _payload_text(metadata.get("task_class")),
        "selection_source": selection_source,
        "catalog_source": _payload_text(catalog.get("source")),
        "catalog_version": _payload_text(catalog.get("version")),
        "root_outcome": root_outcome,
        "defects_found": review[1] if review is not None else 0,
    }


def _routing_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _payload_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        task_id=row["task_id"],
        route=row["route"],
        workspace_path=Path(row["workspace_path"]),
        expected_branch=row["expected_branch"],
        status=row["status"],
        config_path=Path(row["config_path"]),
        run_dir=Path(row["run_dir"]),
        prompt_path=Path(row["prompt_path"]),
        result_path=Path(row["result_path"]),
        log_path=_optional_path(row["log_path"]),
        worker_pid=row["worker_pid"],
        runner_pid=row["runner_pid"],
        runner_process_identity=row["runner_process_identity"],
        agy_pid=row["agy_pid"],
        backend=normalize_backend(row["backend"]),
        agy_model=row["agy_model"],
        codex_model=row["codex_model"],
        codex_reasoning_effort=row["codex_reasoning_effort"],
        codex_quality_tier=row["codex_quality_tier"],
        codex_premium_override_reason=row["codex_premium_override_reason"],
        codex_tool_call_budget=row["codex_tool_call_budget"],
        workspace_access=row["workspace_access"],
        archived_at=row["archived_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        timeout_sec=row["timeout_sec"],
        idle_timeout_sec=row["idle_timeout_sec"],
        print_timeout=row["print_timeout"],
        max_restarts=row["max_restarts"],
        yolo=bool(row["yolo"]),
        allow_dirty=bool(row["allow_dirty"]),
        read_only=bool(row["read_only"]),
        last_error=row["last_error"],
        cancel_requested=bool(row["cancel_requested"]),
        slot_name=row["slot_name"],
        worker_instance_id=row["worker_instance_id"],
        worker_heartbeat_at=row["worker_heartbeat_at"],
        finalization_status=row["finalization_status"],
        finalization_error=row["finalization_error"],
        finalized_at=row["finalized_at"],
    )


def format_events(events: Iterable[tuple[str, str, str]]) -> str:
    return "\n".join(f"{created_at} [{level}] {message}" for created_at, level, message in events)
