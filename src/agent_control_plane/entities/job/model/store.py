from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Iterator
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

JOB_COLUMNS = {
    "status",
    "agy_model",
    "codex_model",
    "codex_reasoning_effort",
    "codex_tool_call_budget",
    "run_dir",
    "prompt_path",
    "log_path",
    "worker_pid",
    "runner_pid",
    "agy_pid",
    "started_at",
    "finished_at",
    "last_error",
    "read_only",
    "cancel_requested",
    "slot_name",
    "archived_at",
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
    agy_pid: int | None
    backend: str
    agy_model: str | None
    codex_model: str | None
    codex_reasoning_effort: str | None
    codex_quality_tier: str | None
    codex_tool_call_budget: int | None
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


class JobStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
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
                    cancel_requested integer not null default 0
                );

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
                );

                create table if not exists events (
                    id integer primary key autoincrement,
                    job_id text not null references jobs(job_id),
                    created_at text not null,
                    level text not null,
                    message text not null
                );
                """
            )
            create_attempt_metrics_table(db)
            self._ensure_columns(db)
            self._ensure_attempt_columns(db)

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
        codex_tool_call_budget: int | None = None,
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
                    codex_quality_tier,
                    codex_tool_call_budget,
                    created_at, updated_at, timeout_sec, idle_timeout_sec,
                    print_timeout, max_restarts, yolo, allow_dirty, read_only, slot_name
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    codex_tool_call_budget,
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
        return self.update_job(
            job_id,
            status=status,
            finished_at=utc_now(),
            last_error=last_error,
            runner_pid=None,
            agy_pid=None,
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
        valid_only: bool = False,
    ) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            rows = load_attempt_metrics(
                db,
                model=model,
                reasoning_effort=reasoning_effort,
                valid_only=valid_only,
                limit=limit,
            )
        return build_metrics_report(rows)

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

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

    @staticmethod
    def _ensure_attempt_columns(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(attempts)").fetchall()}
        if "result_status" not in columns:
            db.execute("alter table attempts add column result_status text")


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
        agy_pid=row["agy_pid"],
        backend=normalize_backend(row["backend"]),
        agy_model=row["agy_model"],
        codex_model=row["codex_model"],
        codex_reasoning_effort=row["codex_reasoning_effort"],
        codex_quality_tier=row["codex_quality_tier"],
        codex_tool_call_budget=row["codex_tool_call_budget"],
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
    )


def format_events(events: Iterable[tuple[str, str, str]]) -> str:
    return "\n".join(f"{created_at} [{level}] {message}" for created_at, level, message in events)
