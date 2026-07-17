from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database

ACTIVE_JOB_STATES = frozenset(
    {
        "dispatching",
        "created",
        "queued",
        "running",
        "waiting_quota",
        "cancel_requested",
        "finalizing",
    }
)
TERMINAL_JOB_STATES = frozenset(
    {
        "completed",
        "partial",
        "blocked",
        "failed",
        "cancelled",
        "guardrail_violation",
        "worker_error",
        "stopped_dirty_after_failure",
    }
)
DECISION_STATES = frozenset(
    {
        "awaiting_review",
        "dispatch_failed",
        "partial",
        "blocked",
        "failed",
        "cancelled",
        "guardrail_violation",
        "worker_error",
        "stopped_dirty_after_failure",
        "rejected",
    }
)
BLOCKED_STATES = DECISION_STATES - {"awaiting_review"}


@dataclass(frozen=True)
class PlanExecutionSpec:
    route: str
    brief: str
    slot: str | None = None
    backend: str | None = None
    workspace_access: str | None = None
    read_only: bool = False
    codex_quality_tier: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _required("execution route", self.route))
        object.__setattr__(self, "brief", _required("execution brief", self.brief))
        for field_name in (
            "slot",
            "backend",
            "workspace_access",
            "codex_quality_tier",
            "codex_model",
            "codex_reasoning_effort",
        ):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, value.strip() if value and value.strip() else None)


@dataclass(frozen=True)
class PlanTaskDefinition:
    task_id: str
    title: str
    depends_on: tuple[str, ...] = ()
    execution: PlanExecutionSpec | None = None


@dataclass(frozen=True)
class PlanDispatchClaim:
    plan_id: str
    task_id: str
    dispatch_task_id: str
    dispatch_token: str
    attempt_no: int
    execution: PlanExecutionSpec


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    title: str
    objective: str
    status: str
    created_at: str
    updated_at: str
    cancel_requested_at: str | None
    cancelled_at: str | None
    archived_at: str | None


class PlanStore:
    """Durable plan graph plus a compact, cursor-based supervisor projection."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="plan_store",
            version=1,
            checksum="plan-store-v1-20260715",
            migrate=self._migrate_schema,
        )
        apply_schema_migration(
            self.database_path,
            component="plan_store",
            version=2,
            checksum="plan-dispatch-owner-v2-20260715",
            migrate=self._migrate_dispatch_owner,
        )
        apply_schema_migration(
            self.database_path,
            component="plan_store",
            version=3,
            checksum="plan-lifecycle-v3-20260715",
            migrate=self._migrate_plan_lifecycle,
        )
        apply_schema_migration(
            self.database_path,
            component="plan_store",
            version=5,
            checksum="plan-execution-contract-v5-20260717",
            migrate=self._migrate_plan_execution_contract,
        )

    @classmethod
    def _migrate_schema(cls, db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists plans (
                    plan_id text primary key,
                    title text not null,
                    objective text not null,
                    status text not null,
                    created_at text not null,
                    updated_at text not null,
                    cancel_requested_at text,
                    cancelled_at text,
                    archived_at text
                )
            """
        )
        db.execute(
            """
            create table if not exists plan_tasks (
                    plan_id text not null references plans(plan_id) on delete cascade,
                    task_id text not null,
                    title text not null,
                    state text not null,
                    job_id text,
                    job_status text,
                    review_status text not null,
                    accepted_sha text,
                    execution_json text,
                    attempt_no integer not null default 0,
                    dispatch_token text,
                    dispatch_started_at text,
                    dispatch_task_id text,
                    dispatch_error text,
                    dispatch_owner_pid integer,
                    created_at text not null,
                    updated_at text not null,
                    primary key(plan_id, task_id)
                )
            """
        )
        cls._ensure_task_columns(db)
        db.execute(
            """
            create unique index if not exists plan_tasks_job_id
            on plan_tasks(job_id) where job_id is not null
            """
        )
        db.execute(
            """
            create table if not exists plan_task_dependencies (
                    plan_id text not null,
                    task_id text not null,
                    dependency_task_id text not null,
                    primary key(plan_id, task_id, dependency_task_id),
                    foreign key(plan_id, task_id)
                        references plan_tasks(plan_id, task_id) on delete cascade,
                    foreign key(plan_id, dependency_task_id)
                        references plan_tasks(plan_id, task_id) on delete cascade
                )
            """
        )
        db.execute(
            """
            create table if not exists plan_events (
                    id integer primary key autoincrement,
                    plan_id text not null references plans(plan_id) on delete cascade,
                    task_id text,
                    event_type text not null,
                    payload_json text not null,
                    created_at text not null
                )
            """
        )
        db.execute(
            """
            create index if not exists plan_events_plan_cursor
            on plan_events(plan_id, id)
            """
        )

    @staticmethod
    def _ensure_task_columns(db: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in db.execute("pragma table_info(plan_tasks)")}
        text_column_type = "text"
        additions = {
            "execution_json": text_column_type,
            "attempt_no": "integer not null default 0",
            "dispatch_token": text_column_type,
            "dispatch_started_at": text_column_type,
            "dispatch_task_id": text_column_type,
            "dispatch_error": text_column_type,
            "dispatch_owner_pid": "integer",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"alter table plan_tasks add column {name} {definition}")  # nosec B608

    @staticmethod
    def _migrate_dispatch_owner(db: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in db.execute("pragma table_info(plan_tasks)")}
        if "dispatch_owner_pid" not in columns:
            db.execute("alter table plan_tasks add column dispatch_owner_pid integer")

    @staticmethod
    def _migrate_plan_lifecycle(db: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in db.execute("pragma table_info(plans)")}
        for name in ("cancel_requested_at", "cancelled_at", "archived_at"):
            if name not in columns:
                db.execute(f"alter table plans add column {name} text")  # nosec B608
        db.execute(
            """
            create index if not exists plans_archived_at_idx
            on plans(archived_at, updated_at)
            """
        )

    @staticmethod
    def _migrate_plan_execution_contract(db: sqlite3.Connection) -> None:
        # Existing rows already store execution JSON. Missing fields are optional and
        # now load as None through PlanExecutionSpec normalization.
        # This no-op migration keeps older durable plans readable and preserves
        # backward compatibility.
        db.execute("pragma schema_version")

    def create_plan(
        self,
        *,
        plan_id: str,
        title: str,
        objective: str = "",
        tasks: Sequence[PlanTaskDefinition] = (),
    ) -> PlanRecord:
        self.initialize()
        plan_id = _required("plan_id", plan_id)
        title = _required("title", title)
        definitions = tuple(tasks)
        _validate_task_graph(definitions)
        now = utc_now()
        with self._connect() as db:
            db.execute("begin immediate")
            if (
                db.execute("select 1 from plans where plan_id = ?", (plan_id,)).fetchone()
                is not None
            ):
                raise ValueError(f"Plan already exists: {plan_id}")
            db.execute(
                """
                insert into plans (plan_id, title, objective, status, created_at, updated_at)
                values (?, ?, ?, 'active', ?, ?)
                """,
                (plan_id, title, objective.strip(), now, now),
            )
            self._add_event(db, plan_id, "plan_created", {"title": title})
            for task in definitions:
                self._insert_task(db, plan_id, task, now)
            self._refresh_ready_states(db, plan_id)
        return self.get_plan(plan_id)

    def request_cancel(self, plan_id: str) -> dict[str, Any]:
        """Stop future dispatch and return unfinished jobs needing cooperative cancellation."""
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            plan = self._sync_plan(db, plan_id)
            if plan.archived_at is not None:
                raise ValueError(f"Plan is archived: {plan_id}")
            if plan.status == "completed":
                raise ValueError(f"Completed plan cannot be cancelled: {plan_id}")
            if plan.status not in {"active", "cancelling", "cancelled"}:
                raise ValueError(f"Plan cannot be cancelled from status {plan.status}: {plan_id}")
            if plan.status == "active":
                now = utc_now()
                db.execute(
                    """
                    update plans set status = 'cancelling', cancel_requested_at = ?,
                        updated_at = ? where plan_id = ?
                    """,
                    (now, now, plan_id),
                )
                db.execute(
                    """
                    update plan_tasks set state = 'cancelled', updated_at = ?
                    where plan_id = ? and job_id is null
                        and state not in ('completed', 'dispatching')
                    """,
                    (now, plan_id),
                )
                self._add_event(db, plan_id, "plan_cancel_requested", {})
            active_job_ids = [
                str(row["job_id"])
                for row in db.execute(
                    """
                    select distinct j.job_id
                    from plan_tasks t join jobs j on j.job_id = t.job_id
                    where t.plan_id = ? and j.finished_at is null
                    order by j.job_id
                    """,
                    (plan_id,),
                ).fetchall()
            ]
            updated = self._sync_lifecycle_status(db, plan_id)
            return {
                "plan_id": plan_id,
                "status": updated.status,
                "active_job_ids": active_job_ids,
                "cancel_requested_at": updated.cancel_requested_at,
                "cancelled_at": updated.cancelled_at,
            }

    def archive_plan(self, plan_id: str) -> PlanRecord:
        """Mark a terminal, fully reviewed plan as retention-eligible."""
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            plan = self._sync_plan(db, plan_id)
            if plan.archived_at is not None:
                return plan
            if plan.status not in {"completed", "cancelled"}:
                raise ValueError(
                    f"Plan must be completed or cancelled before archive: {plan_id} ({plan.status})"
                )
            active = db.execute(
                """
                select task_id from plan_tasks
                where plan_id = ? and state in (
                    'dispatching', 'created', 'queued', 'running', 'waiting_quota',
                    'cancel_requested', 'finalizing'
                ) order by task_id
                """,
                (plan_id,),
            ).fetchall()
            if active:
                task_ids = ", ".join(str(row["task_id"]) for row in active)
                raise ValueError(f"Plan still has active tasks: {plan_id}: {task_ids}")
            pending_items = self._pending_review_items(db, plan_id)
            if pending_items:
                raise ValueError(
                    f"Plan still has pending inbox items: {plan_id}: " + ", ".join(pending_items)
                )
            now = utc_now()
            db.execute(
                "update plans set archived_at = ?, updated_at = ? where plan_id = ?",
                (now, now, plan_id),
            )
            self._add_event(db, plan_id, "plan_archived", {})
            return _plan_from_row(self._require_plan(db, plan_id))

    def add_task(self, plan_id: str, task: PlanTaskDefinition) -> None:
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            self._require_active_plan(db, plan_id)
            existing = self._task_definitions(db, plan_id)
            _validate_task_graph((*existing, task))
            self._insert_task(db, plan_id, task, utc_now())
            self._refresh_ready_states(db, plan_id)

    def assert_task_can_start(self, plan_id: str, task_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            self._sync_plan(db, plan_id)
            self._require_active_plan(db, plan_id)
            task = self._require_task(db, plan_id, task_id)
            if task["state"] == "completed":
                raise ValueError(f"Plan task already completed: {plan_id}/{task_id}")
            if task["state"] in ACTIVE_JOB_STATES:
                raise ValueError(
                    f"Plan task already has an active job: {plan_id}/{task_id} ({task['job_id']})"
                )
            if task["state"] == "awaiting_review":
                raise ValueError(
                    f"Plan task is awaiting root review: {plan_id}/{task_id} ({task['job_id']})"
                )
            incomplete = self._incomplete_dependencies(db, plan_id, task_id)
            if incomplete:
                raise ValueError(
                    f"Plan task dependencies are incomplete: {plan_id}/{task_id}: "
                    + ", ".join(incomplete)
                )

    def bind_job(self, plan_id: str, task_id: str, job_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            self._sync_plan(db, plan_id)
            self._require_active_plan(db, plan_id)
            task = self._require_task(db, plan_id, task_id)
            incomplete = self._incomplete_dependencies(db, plan_id, task_id)
            if incomplete:
                raise ValueError(
                    f"Plan task dependencies are incomplete: {plan_id}/{task_id}: "
                    + ", ".join(incomplete)
                )
            if task["state"] in ACTIVE_JOB_STATES:
                raise ValueError(
                    f"Plan task already has an active job: {plan_id}/{task_id} ({task['job_id']})"
                )
            if task["state"] == "awaiting_review":
                raise ValueError(
                    f"Plan task is awaiting root review: {plan_id}/{task_id} ({task['job_id']})"
                )
            job = db.execute(
                "select job_id, status, finalization_status from jobs where job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job not found: {job_id}")
            if task["state"] == "completed":
                raise ValueError(f"Plan task already completed: {plan_id}/{task_id}")
            now = utc_now()
            state = _state_for_job(
                job["status"],
                "pending",
                job["finalization_status"],
            )
            try:
                db.execute(
                    """
                    update plan_tasks set
                        job_id = ?, job_status = ?, state = ?, review_status = 'pending',
                        accepted_sha = null, updated_at = ?
                    where plan_id = ? and task_id = ?
                    """,
                    (job_id, job["status"], state, now, plan_id, task_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Job is already bound to another plan task: {job_id}") from exc
            self._add_event(
                db,
                plan_id,
                "job_bound",
                {"job_id": job_id, "state": state},
                task_id=task_id,
            )

    def claim_ready_tasks(
        self,
        plan_id: str,
        *,
        owner_pid: int | None = None,
        limit: int = 1,
    ) -> list[PlanDispatchClaim]:
        """Atomically claim executable dependency-ready tasks for one dispatch pass."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if owner_pid is not None and owner_pid <= 0:
            raise ValueError("owner_pid must be positive")
        self.initialize()
        claims: list[PlanDispatchClaim] = []
        with self._connect() as db:
            db.execute("begin immediate")
            self._sync_plan(db, plan_id)
            self._require_active_plan(db, plan_id)
            rows = db.execute(
                """
                select * from plan_tasks
                where plan_id = ? and state = 'ready' and execution_json is not null
                order by created_at, task_id
                limit ?
                """,
                (plan_id, limit),
            ).fetchall()
            for row in rows:
                execution = _execution_from_json(row["execution_json"])
                if execution is None:
                    continue
                attempt_no = int(row["attempt_no"]) + 1
                dispatch_token = uuid.uuid4().hex
                dispatch_task_id = _dispatch_task_id(
                    plan_id,
                    str(row["task_id"]),
                    attempt_no,
                    dispatch_token,
                )
                now = utc_now()
                cursor = db.execute(
                    """
                    update plan_tasks set
                        state = 'dispatching', attempt_no = ?, dispatch_token = ?,
                        dispatch_started_at = ?, dispatch_task_id = ?, dispatch_error = null,
                        dispatch_owner_pid = ?, updated_at = ?
                    where plan_id = ? and task_id = ? and state = 'ready'
                        and job_id is null
                    """,
                    (
                        attempt_no,
                        dispatch_token,
                        now,
                        dispatch_task_id,
                        owner_pid,
                        now,
                        plan_id,
                        row["task_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                claims.append(
                    PlanDispatchClaim(
                        plan_id=plan_id,
                        task_id=str(row["task_id"]),
                        dispatch_task_id=dispatch_task_id,
                        dispatch_token=dispatch_token,
                        attempt_no=attempt_no,
                        execution=execution,
                    )
                )
                self._add_event(
                    db,
                    plan_id,
                    "task_dispatch_claimed",
                    {
                        "attempt_no": attempt_no,
                        "dispatch_task_id": dispatch_task_id,
                    },
                    task_id=str(row["task_id"]),
                )
        return claims

    def reconcile_orphaned_dispatches(
        self,
        plan_id: str,
        *,
        process_is_alive: Callable[[int], bool],
    ) -> list[str]:
        """Fail closed when a dispatcher died after claiming but before binding a job."""
        self.initialize()
        recovered: list[str] = []
        with self._connect() as db:
            db.execute("begin immediate")
            self._require_plan(db, plan_id)
            rows = db.execute(
                """
                select task_id, dispatch_token, dispatch_owner_pid
                from plan_tasks
                where plan_id = ? and state = 'dispatching' and job_id is null
                """,
                (plan_id,),
            ).fetchall()
            for row in rows:
                owner_pid = row["dispatch_owner_pid"]
                if owner_pid is not None and process_is_alive(int(owner_pid)):
                    continue
                cursor = db.execute(
                    """
                    update plan_tasks set state = 'dispatch_failed',
                        dispatch_error = 'dispatcher exited before binding a job',
                        dispatch_owner_pid = null, updated_at = ?
                    where plan_id = ? and task_id = ? and state = 'dispatching'
                        and dispatch_token = ? and job_id is null
                    """,
                    (utc_now(), plan_id, row["task_id"], row["dispatch_token"]),
                )
                if cursor.rowcount != 1:
                    continue
                recovered.append(str(row["task_id"]))
                self._add_event(
                    db,
                    plan_id,
                    "task_dispatch_orphaned",
                    {"owner_pid": owner_pid},
                    task_id=str(row["task_id"]),
                )
        return recovered

    def assert_dispatch_claim(
        self,
        plan_id: str,
        task_id: str,
        *,
        dispatch_token: str,
        dispatch_task_id: str,
    ) -> None:
        self.initialize()
        with self._connect() as db:
            self._require_active_plan(db, plan_id)
            task = self._require_task(db, plan_id, task_id)
            if (
                task["state"] != "dispatching"
                or task["dispatch_token"] != dispatch_token
                or task["dispatch_task_id"] != dispatch_task_id
                or task["job_id"] is not None
            ):
                raise ValueError(f"Plan dispatch claim is stale: {plan_id}/{task_id}")

    def bind_dispatched_job(
        self,
        plan_id: str,
        task_id: str,
        *,
        dispatch_token: str,
        job_id: str,
    ) -> None:
        """Bind a created job only while the caller still owns the dispatch claim."""
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            plan = _plan_from_row(self._require_plan(db, plan_id))
            job = db.execute(
                """
                select job_id, status, finalization_status, finished_at
                from jobs where job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"Job not found: {job_id}")
            job_status = str(job["status"])
            if plan.status != "active" and job["finished_at"] is None:
                now = utc_now()
                db.execute(
                    """
                    update jobs set status = 'cancel_requested', cancel_requested = 1,
                        updated_at = ? where job_id = ? and finished_at is null
                    """,
                    (now, job_id),
                )
                db.execute(
                    """
                    insert into events (job_id, created_at, level, message)
                    values (?, ?, 'warning', 'Plan cancellation raced with dispatch; cancel requested')
                    """,
                    (job_id, now),
                )
                job_status = "cancel_requested"
            state = _state_for_job(job_status, "pending", job["finalization_status"])
            try:
                cursor = db.execute(
                    """
                    update plan_tasks set
                        job_id = ?, job_status = ?, state = ?, review_status = 'pending',
                        accepted_sha = null, dispatch_error = null,
                        dispatch_owner_pid = null, updated_at = ?
                    where plan_id = ? and task_id = ? and state = 'dispatching'
                        and dispatch_token = ? and job_id is null
                    """,
                    (job_id, job_status, state, utc_now(), plan_id, task_id, dispatch_token),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Job is already bound to another plan task: {job_id}") from exc
            if cursor.rowcount != 1:
                raise ValueError(f"Plan dispatch claim is stale: {plan_id}/{task_id}")
            self._add_event(
                db,
                plan_id,
                "job_dispatched",
                {"job_id": job_id, "state": state},
                task_id=task_id,
            )

    def mark_dispatch_failed(
        self,
        plan_id: str,
        task_id: str,
        *,
        dispatch_token: str,
        error: str,
    ) -> bool:
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            cursor = db.execute(
                """
                update plan_tasks set state = 'dispatch_failed', dispatch_error = ?,
                    dispatch_owner_pid = null, updated_at = ?
                where plan_id = ? and task_id = ? and state = 'dispatching'
                    and dispatch_token = ? and job_id is null
                """,
                (error, utc_now(), plan_id, task_id, dispatch_token),
            )
            if cursor.rowcount == 1:
                self._add_event(
                    db,
                    plan_id,
                    "task_dispatch_failed",
                    {"error": error},
                    task_id=task_id,
                )
                self._sync_plan(db, plan_id)
                return True
            return False

    def retry_task(
        self,
        plan_id: str,
        task_id: str,
        *,
        brief_override: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly clear a failed/rejected attempt so it may be dispatched again."""
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            self._sync_plan(db, plan_id)
            self._require_active_plan(db, plan_id)
            task = self._require_task(db, plan_id, task_id)
            retryable_states = BLOCKED_STATES - {"awaiting_review"}
            if task["state"] not in retryable_states:
                raise ValueError(
                    f"Plan task is not eligible for retry: {plan_id}/{task_id} ({task['state']})"
                )
            execution = _execution_from_json(task["execution_json"])
            if execution is None:
                raise ValueError(f"Plan task has no execution specification: {plan_id}/{task_id}")
            if brief_override is not None:
                execution = PlanExecutionSpec(
                    route=execution.route,
                    brief=_required("brief_override", brief_override),
                    slot=execution.slot,
                    backend=execution.backend,
                    workspace_access=execution.workspace_access,
                    read_only=execution.read_only,
                    codex_quality_tier=execution.codex_quality_tier,
                    codex_model=execution.codex_model,
                    codex_reasoning_effort=execution.codex_reasoning_effort,
                )
            now = utc_now()
            db.execute(
                """
                update plan_tasks set
                    state = 'pending', job_id = null, job_status = null,
                    review_status = 'pending', accepted_sha = null,
                    execution_json = ?, dispatch_token = null,
                    dispatch_started_at = null, dispatch_task_id = null,
                    dispatch_error = null, dispatch_owner_pid = null, updated_at = ?
                where plan_id = ? and task_id = ?
                """,
                (_execution_json(execution), now, plan_id, task_id),
            )
            self._add_event(
                db,
                plan_id,
                "task_retry_requested",
                {"previous_state": task["state"], "attempt_no": int(task["attempt_no"])},
                task_id=task_id,
            )
            self._refresh_ready_states(db, plan_id)
            updated = self._require_task(db, plan_id, task_id)
            return {
                "plan_id": plan_id,
                "task_id": task_id,
                "state": updated["state"],
                "attempt_no": int(updated["attempt_no"]),
                "execution": _execution_summary(execution),
            }

    def accept_task(self, plan_id: str, task_id: str, *, accepted_sha: str | None = None) -> None:
        self._record_decision(plan_id, task_id, "accepted", accepted_sha=accepted_sha)

    def accept_task_in_transaction(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        task_id: str,
        *,
        accepted_sha: str | None = None,
    ) -> None:
        self._record_decision_in_transaction(
            db,
            plan_id,
            task_id,
            "accepted",
            accepted_sha=accepted_sha,
        )

    def reject_task(self, plan_id: str, task_id: str) -> None:
        self._record_decision(plan_id, task_id, "rejected")

    def snapshot(
        self,
        plan_id: str,
        *,
        since: int | None = None,
        event_limit: int = 100,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        if since is not None and since < 0:
            raise ValueError("since must be non-negative")
        if event_limit <= 0 or item_limit <= 0:
            raise ValueError("snapshot limits must be positive")
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            plan = self._sync_plan(db, plan_id)
            tasks = self._task_payloads(db, plan_id)
            latest_cursor = self._latest_cursor(db, plan_id)
            changes, cursor = self._changes(
                db,
                plan_id,
                since=since,
                limit=event_limit,
                latest_cursor=latest_cursor,
            )

        counts = dict(sorted(Counter(task["state"] for task in tasks).items()))
        completed_count = counts.get("completed", 0)
        projection_groups = {
            "completed_tasks": [task for task in tasks if task["state"] == "completed"],
            "running": [task for task in tasks if task["state"] in ACTIVE_JOB_STATES],
            "awaiting_review": [task for task in tasks if task["state"] == "awaiting_review"],
            "blocked": [task for task in tasks if task["state"] in BLOCKED_STATES],
            "ready_next": [task for task in tasks if task["state"] == "ready"],
            "requires_root_decision": [task for task in tasks if task["state"] in DECISION_STATES],
        }
        item_counts = {name: len(items) for name, items in projection_groups.items()}
        truncated = {name: len(items) > item_limit for name, items in projection_groups.items()}
        return {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "objective": plan.objective,
            "status": plan.status,
            "cancel_requested_at": plan.cancel_requested_at,
            "cancelled_at": plan.cancelled_at,
            "archived_at": plan.archived_at,
            "progress": f"{completed_count}/{len(tasks)}",
            "counts": counts,
            "cursor": cursor,
            "latest_cursor": latest_cursor,
            "has_more_changes": cursor < latest_cursor,
            "changes": changes,
            "completed": _completed_changes(changes),
            "completed_tasks": _completed_task_identities(projection_groups["completed_tasks"])[
                :item_limit
            ],
            "running": projection_groups["running"][:item_limit],
            "awaiting_review": projection_groups["awaiting_review"][:item_limit],
            "blocked": projection_groups["blocked"][:item_limit],
            "ready_next": projection_groups["ready_next"][:item_limit],
            "requires_root_decision": projection_groups["requires_root_decision"][:item_limit],
            "item_counts": item_counts,
            "truncated": truncated,
        }

    def get_plan(self, plan_id: str) -> PlanRecord:
        self.initialize()
        with self._connect() as db:
            row = self._require_plan(db, plan_id)
        return _plan_from_row(row)

    def review_target(self, plan_id: str, task_id: str) -> dict[str, Any]:
        """Return the exact bound result needed for a root acceptance decision."""
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            return self.review_target_in_transaction(db, plan_id, task_id)

    def review_target_in_transaction(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        self._sync_plan(db, plan_id)
        row = db.execute(
            """
            select t.state, t.job_id, t.job_status, j.result_path
            from plan_tasks t
            left join jobs j on j.job_id = t.job_id
            where t.plan_id = ? and t.task_id = ?
            """,
            (plan_id, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Plan task not found: {plan_id}/{task_id}")
        return dict(row)

    def list_plans(
        self,
        limit: int = 20,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            plan_ids = [
                row["plan_id"]
                for row in db.execute(
                    """
                    select plan_id from plans
                    where ? or archived_at is null
                    order by updated_at desc limit ?
                    """,
                    (int(include_archived), limit),
                ).fetchall()
            ]
            for plan_id in plan_ids:
                self._sync_plan(db, plan_id)
            rows = db.execute(
                """
                select p.*, count(t.task_id) as task_count,
                       sum(case when t.state = 'completed' then 1 else 0 end) as completed_count
                from plans p left join plan_tasks t on t.plan_id = p.plan_id
                where ? or p.archived_at is null
                group by p.plan_id order by p.updated_at desc limit ?
                """,
                (int(include_archived), limit),
            ).fetchall()
        return [
            {
                "plan_id": row["plan_id"],
                "title": row["title"],
                "status": _listed_plan_status(row),
                "progress": f"{int(row['completed_count'] or 0)}/{int(row['task_count'])}",
                "updated_at": row["updated_at"],
                "cancel_requested_at": row["cancel_requested_at"],
                "cancelled_at": row["cancelled_at"],
                "archived_at": row["archived_at"],
            }
            for row in rows
        ]

    def _record_decision(
        self,
        plan_id: str,
        task_id: str,
        decision: str,
        *,
        accepted_sha: str | None = None,
    ) -> None:
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            self._record_decision_in_transaction(
                db,
                plan_id,
                task_id,
                decision,
                accepted_sha=accepted_sha,
            )

    def _record_decision_in_transaction(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        task_id: str,
        decision: str,
        *,
        accepted_sha: str | None = None,
    ) -> None:
        self._sync_plan(db, plan_id)
        task = self._require_task(db, plan_id, task_id)
        if task["review_status"] == "accepted":
            if decision == "accepted" and accepted_sha == task["accepted_sha"]:
                return
            raise ValueError(f"Plan task already accepted: {plan_id}/{task_id}")
        if task["review_status"] == "rejected" and decision == "rejected":
            return
        if task["job_id"] is None or task["state"] != "awaiting_review":
            raise ValueError(f"Plan task has no eligible completed worker: {plan_id}/{task_id}")
        state = "completed" if decision == "accepted" else "rejected"
        now = utc_now()
        db.execute(
            """
            update plan_tasks set state = ?, review_status = ?, accepted_sha = ?, updated_at = ?
            where plan_id = ? and task_id = ?
            """,
            (state, decision, accepted_sha, now, plan_id, task_id),
        )
        self._add_event(
            db,
            plan_id,
            "task_accepted" if decision == "accepted" else "task_rejected",
            {"job_id": task["job_id"], "accepted_sha": accepted_sha},
            task_id=task_id,
        )
        self._refresh_ready_states(db, plan_id)
        self._sync_lifecycle_status(db, plan_id)

    def _sync_plan(self, db: sqlite3.Connection, plan_id: str) -> PlanRecord:
        self._require_plan(db, plan_id)
        rows = db.execute(
            """
            select t.*, j.status as observed_job_status,
                j.finalization_status as observed_finalization_status
            from plan_tasks t left join jobs j on j.job_id = t.job_id
            where t.plan_id = ? and t.job_id is not null
            """,
            (plan_id,),
        ).fetchall()
        for task in rows:
            review_status, accepted_sha = self._observed_review(db, task)
            job_status = task["observed_job_status"] or task["job_status"]
            state = _state_for_job(
                job_status,
                review_status,
                task["observed_finalization_status"],
            )
            if (
                state == task["state"]
                and job_status == task["job_status"]
                and review_status == task["review_status"]
                and accepted_sha == task["accepted_sha"]
            ):
                continue
            now = utc_now()
            db.execute(
                """
                update plan_tasks set state = ?, job_status = ?, review_status = ?,
                    accepted_sha = ?, updated_at = ?
                where plan_id = ? and task_id = ?
                """,
                (state, job_status, review_status, accepted_sha, now, plan_id, task["task_id"]),
            )
            event_type = "task_accepted" if state == "completed" else "task_state_changed"
            self._add_event(
                db,
                plan_id,
                event_type,
                {
                    "job_id": task["job_id"],
                    "old_state": task["state"],
                    "state": state,
                    "job_status": job_status,
                    "review_status": review_status,
                    "accepted_sha": accepted_sha,
                },
                task_id=task["task_id"],
            )
        self._refresh_ready_states(db, plan_id)
        return self._sync_lifecycle_status(db, plan_id)

    @staticmethod
    def _observed_review(
        db: sqlite3.Connection,
        task: sqlite3.Row,
    ) -> tuple[str, str | None]:
        if task["review_status"] in {"accepted", "rejected"}:
            return task["review_status"], task["accepted_sha"]
        if task["observed_job_status"] != "completed":
            return task["review_status"], task["accepted_sha"]
        exists = db.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'review_job_outcomes'"
        ).fetchone()
        if exists is None:
            return task["review_status"], task["accepted_sha"]
        review = db.execute(
            """
            select outcome, accepted_sha
            from review_job_outcomes
            where job_id = ? and root_verified = 1
            order by recorded_at desc
            limit 1
            """,
            (task["job_id"],),
        ).fetchone()
        if review is None:
            return task["review_status"], task["accepted_sha"]
        if review["outcome"] == "accepted":
            return "accepted", review["accepted_sha"]
        if review["outcome"] == "rejected":
            return "rejected", None
        return task["review_status"], task["accepted_sha"]

    def _refresh_ready_states(self, db: sqlite3.Connection, plan_id: str) -> None:
        plan = self._require_plan(db, plan_id)
        if plan["status"] != "active" or plan["archived_at"] is not None:
            return
        rows = db.execute(
            """
            select * from plan_tasks
            where plan_id = ? and job_id is null and state in ('pending', 'ready')
            """,
            (plan_id,),
        ).fetchall()
        for task in rows:
            state = (
                "pending"
                if self._incomplete_dependencies(db, plan_id, task["task_id"])
                else "ready"
            )
            if state == task["state"]:
                continue
            db.execute(
                "update plan_tasks set state = ?, updated_at = ? where plan_id = ? and task_id = ?",
                (state, utc_now(), plan_id, task["task_id"]),
            )
            self._add_event(
                db,
                plan_id,
                "task_ready" if state == "ready" else "task_waiting",
                {"old_state": task["state"], "state": state},
                task_id=task["task_id"],
            )

    def _sync_lifecycle_status(self, db: sqlite3.Connection, plan_id: str) -> PlanRecord:
        plan = _plan_from_row(self._require_plan(db, plan_id))
        if plan.status == "cancelling":
            now = utc_now()
            db.execute(
                """
                update plan_tasks set state = 'cancelled', updated_at = ?
                where plan_id = ? and job_id is null
                    and state not in ('completed', 'cancelled', 'dispatching')
                """,
                (now, plan_id),
            )
            active_count = int(
                db.execute(
                    """
                    select count(*) as count from plan_tasks
                    where plan_id = ? and state in (
                        'dispatching', 'created', 'queued', 'running', 'waiting_quota',
                        'cancel_requested', 'finalizing'
                    )
                    """,
                    (plan_id,),
                ).fetchone()["count"]
            )
            if active_count == 0:
                db.execute(
                    """
                    update plans set status = 'cancelled', cancelled_at = ?, updated_at = ?
                    where plan_id = ? and status = 'cancelling'
                    """,
                    (now, now, plan_id),
                )
                self._add_event(db, plan_id, "plan_cancelled", {})
        elif plan.status == "active":
            counts = db.execute(
                """
                select count(*) as task_count,
                    sum(case when state = 'completed' then 1 else 0 end) as completed_count
                from plan_tasks where plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            task_count = int(counts["task_count"])
            completed_count = int(counts["completed_count"] or 0)
            if task_count and completed_count == task_count:
                now = utc_now()
                db.execute(
                    """
                    update plans set status = 'completed', updated_at = ?
                    where plan_id = ? and status = 'active'
                    """,
                    (now, plan_id),
                )
                self._add_event(db, plan_id, "plan_completed", {})
        return _plan_from_row(self._require_plan(db, plan_id))

    @staticmethod
    def _pending_review_items(db: sqlite3.Connection, plan_id: str) -> list[str]:
        inbox_exists = db.execute(
            """
            select 1 from sqlite_master
            where type = 'table' and name = 'review_inbox_items'
            """
        ).fetchone()
        if inbox_exists is None:
            return []
        rows = db.execute(
            """
            select i.item_id
            from plan_tasks t join review_inbox_items i
              on i.source_kind = 'agent_job' and i.source_id = t.job_id
            where t.plan_id = ? and i.review_status = 'pending'
            order by i.item_id
            """,
            (plan_id,),
        ).fetchall()
        return [str(row["item_id"]) for row in rows]

    @staticmethod
    def _incomplete_dependencies(
        db: sqlite3.Connection,
        plan_id: str,
        task_id: str,
    ) -> list[str]:
        rows = db.execute(
            """
            select d.dependency_task_id, t.state
            from plan_task_dependencies d
            join plan_tasks t
              on t.plan_id = d.plan_id and t.task_id = d.dependency_task_id
            where d.plan_id = ? and d.task_id = ? and t.state != 'completed'
            order by d.dependency_task_id
            """,
            (plan_id, task_id),
        ).fetchall()
        return [row["dependency_task_id"] for row in rows]

    def _insert_task(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        task: PlanTaskDefinition,
        now: str,
    ) -> None:
        task_id = _required("task_id", task.task_id)
        title = _required("task title", task.title)
        db.execute(
            """
            insert into plan_tasks (
                plan_id, task_id, title, state, review_status, execution_json,
                created_at, updated_at
            ) values (?, ?, ?, 'pending', 'pending', ?, ?, ?)
            """,
            (
                plan_id,
                task_id,
                title,
                _execution_json(task.execution) if task.execution is not None else None,
                now,
                now,
            ),
        )
        for dependency in task.depends_on:
            db.execute(
                """
                insert into plan_task_dependencies (plan_id, task_id, dependency_task_id)
                values (?, ?, ?)
                """,
                (plan_id, task_id, dependency),
            )
        self._add_event(
            db,
            plan_id,
            "task_added",
            {"title": title, "depends_on": list(task.depends_on)},
            task_id=task_id,
        )

    @staticmethod
    def _task_definitions(db: sqlite3.Connection, plan_id: str) -> tuple[PlanTaskDefinition, ...]:
        rows = db.execute(
            """
            select task_id, title, execution_json from plan_tasks
            where plan_id = ? order by created_at, task_id
            """,
            (plan_id,),
        ).fetchall()
        dependencies = db.execute(
            """
            select task_id, dependency_task_id from plan_task_dependencies
            where plan_id = ? order by dependency_task_id
            """,
            (plan_id,),
        ).fetchall()
        by_task: dict[str, list[str]] = {}
        for dependency in dependencies:
            by_task.setdefault(dependency["task_id"], []).append(dependency["dependency_task_id"])
        return tuple(
            PlanTaskDefinition(
                row["task_id"],
                row["title"],
                tuple(by_task.get(row["task_id"], ())),
                _execution_from_json(row["execution_json"]),
            )
            for row in rows
        )

    @staticmethod
    def _add_event(
        db: sqlite3.Connection,
        plan_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> None:
        db.execute(
            """
            insert into plan_events (plan_id, task_id, event_type, payload_json, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (plan_id, task_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
        )
        db.execute("update plans set updated_at = ? where plan_id = ?", (utc_now(), plan_id))

    def _task_payloads(self, db: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
        rows = db.execute(
            """
            select t.*, j.result_path, j.route, j.backend
            from plan_tasks t left join jobs j on j.job_id = t.job_id
            where t.plan_id = ? order by t.created_at, t.task_id
            """,
            (plan_id,),
        ).fetchall()
        dependencies = db.execute(
            """
            select task_id, dependency_task_id from plan_task_dependencies
            where plan_id = ? order by dependency_task_id
            """,
            (plan_id,),
        ).fetchall()
        by_task: dict[str, list[str]] = {}
        for dependency in dependencies:
            by_task.setdefault(dependency["task_id"], []).append(dependency["dependency_task_id"])
        payloads = []
        for row in rows:
            result_path = Path(row["result_path"]) if row["result_path"] else None
            payloads.append(
                {
                    "task_id": row["task_id"],
                    "title": row["title"],
                    "state": row["state"],
                    "depends_on": by_task.get(row["task_id"], []),
                    "job_id": row["job_id"],
                    "job_status": row["job_status"],
                    "review_status": row["review_status"],
                    "accepted_sha": row["accepted_sha"],
                    "attempt_no": int(row["attempt_no"]),
                    "dispatch_task_id": row["dispatch_task_id"],
                    "dispatch_error": row["dispatch_error"],
                    "execution": _execution_summary_from_json(row["execution_json"]),
                    "route": row["route"],
                    "backend": row["backend"],
                    "result_path": str(result_path) if result_path else None,
                    "result_summary": _compact_result(result_path),
                }
            )
        return payloads

    @staticmethod
    def _changes(
        db: sqlite3.Connection,
        plan_id: str,
        *,
        since: int | None,
        limit: int,
        latest_cursor: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if since is None:
            return [], latest_cursor
        rows = db.execute(
            """
            select * from plan_events where plan_id = ? and id > ? order by id limit ?
            """,
            (plan_id, since, limit),
        ).fetchall()
        changes = [
            {
                "cursor": int(row["id"]),
                "event": row["event_type"],
                "task_id": row["task_id"],
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        cursor = int(rows[-1]["id"]) if rows else since
        return changes, cursor

    @staticmethod
    def _latest_cursor(db: sqlite3.Connection, plan_id: str) -> int:
        row = db.execute(
            "select coalesce(max(id), 0) as cursor from plan_events where plan_id = ?",
            (plan_id,),
        ).fetchone()
        return int(row["cursor"])

    @staticmethod
    def _require_plan(db: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = db.execute("select * from plans where plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(f"Plan not found: {plan_id}")
        return row

    @classmethod
    def _require_active_plan(cls, db: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = cls._require_plan(db, plan_id)
        if row["status"] != "active" or row["archived_at"] is not None:
            raise ValueError(f"Plan is not active: {plan_id} ({row['status']})")
        return row

    @staticmethod
    def _require_task(db: sqlite3.Connection, plan_id: str, task_id: str) -> sqlite3.Row:
        row = db.execute(
            "select * from plan_tasks where plan_id = ? and task_id = ?",
            (plan_id, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Plan task not found: {plan_id}/{task_id}")
        return row

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with control_database(self.database_path) as db:
            yield db


def _execution_json(execution: PlanExecutionSpec) -> str:
    return json.dumps(
        {
            "route": execution.route,
            "brief": execution.brief,
            "slot": execution.slot,
            "backend": execution.backend,
            "workspace_access": execution.workspace_access,
            "read_only": execution.read_only,
            "codex_quality_tier": execution.codex_quality_tier,
            "codex_model": execution.codex_model,
            "codex_reasoning_effort": execution.codex_reasoning_effort,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _execution_from_json(value: str | None) -> PlanExecutionSpec | None:
    if value is None:
        return None
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("Plan execution specification must be a JSON object")
    read_only = payload.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError("Plan execution read_only must be a boolean")
    return PlanExecutionSpec(
        route=str(payload.get("route", "")),
        brief=str(payload.get("brief", "")),
        slot=_optional_text(payload.get("slot")),
        backend=_optional_text(payload.get("backend")),
        workspace_access=_optional_text(payload.get("workspace_access")),
        read_only=read_only,
        codex_quality_tier=_optional_text(payload.get("codex_quality_tier")),
        codex_model=_optional_text(payload.get("codex_model")),
        codex_reasoning_effort=_optional_text(payload.get("codex_reasoning_effort")),
    )


def _execution_summary_from_json(value: str | None) -> dict[str, Any] | None:
    execution = _execution_from_json(value)
    return _execution_summary(execution) if execution is not None else None


def _execution_summary(execution: PlanExecutionSpec) -> dict[str, Any]:
    brief_bytes = execution.brief.encode("utf-8")
    return {
        "route": execution.route,
        "slot": execution.slot,
        "backend": execution.backend,
        "workspace_access": execution.workspace_access,
        "read_only": execution.read_only,
        "codex_quality_tier": execution.codex_quality_tier,
        "codex_model": execution.codex_model,
        "codex_reasoning_effort": execution.codex_reasoning_effort,
        "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(),
        "brief_chars": len(execution.brief),
    }


def _dispatch_task_id(
    plan_id: str,
    task_id: str,
    attempt_no: int,
    dispatch_token: str,
) -> str:
    identity = _slug(f"plan-{plan_id}-{task_id}")[:80]
    return f"{identity}-a{attempt_no}-{dispatch_token[:8]}"


def _validate_task_graph(tasks: Sequence[PlanTaskDefinition]) -> None:
    ids = [_required("task_id", task.task_id) for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Plan contains duplicate task IDs")
    known = set(ids)
    graph: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        if len(task.depends_on) != len(set(task.depends_on)):
            raise ValueError(f"Plan task contains duplicate dependency IDs: {task.task_id}")
        unknown = sorted(set(task.depends_on) - known)
        if unknown:
            raise ValueError(
                f"Plan task {task.task_id!r} depends on unknown task(s): {', '.join(unknown)}"
            )
        graph[task.task_id] = tuple(task.depends_on)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError(f"Plan dependency cycle detected at task: {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)


def _state_for_job(
    job_status: str | None,
    review_status: str,
    finalization_status: str | None,
) -> str:
    if job_status in TERMINAL_JOB_STATES and finalization_status != "completed":
        return "finalizing"
    if review_status == "accepted":
        return "completed"
    if review_status == "rejected":
        return "rejected"
    if job_status == "completed":
        return "awaiting_review"
    return job_status or "pending"


def _completed_changes(changes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "task_id": change["task_id"],
            "job_id": change.get("job_id"),
            "accepted_sha": change.get("accepted_sha"),
        }
        for change in changes
        if change["event"] == "task_accepted"
    ]


def _completed_task_identities(tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"task_id": task["task_id"], "job_id": task["job_id"], "accepted_sha": task["accepted_sha"]}
        for task in tasks
    ]


def _compact_result(path: Path | None, limit: int = 1200) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as result_file:
            text = result_file.read(limit + 1)
    except OSError as exc:
        return f"<result unavailable: {exc}>"
    compact = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _required(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _slug(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "task"


def _plan_from_row(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        plan_id=row["plan_id"],
        title=row["title"],
        objective=row["objective"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancel_requested_at=row["cancel_requested_at"],
        cancelled_at=row["cancelled_at"],
        archived_at=row["archived_at"],
    )


def _listed_plan_status(row: sqlite3.Row) -> str:
    return str(row["status"])
