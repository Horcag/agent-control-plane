from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_control_plane.shared.clock import utc_now
from agent_control_plane.shared.sqlite_runtime import apply_schema_migration, control_database

REVIEW_DECISIONS = frozenset({"accepted", "rejected"})
REVIEW_STATUSES = frozenset({"pending", *REVIEW_DECISIONS})


@dataclass(frozen=True)
class ReviewInboxDraft:
    source_kind: str
    source_id: str
    source_status: str
    delivery_status: str
    source_completed_at: str | None = None
    task_id: str | None = None
    route: str | None = None
    workspace_path: Path | None = None
    slot_name: str | None = None
    parent_thread_id: str | None = None
    agent_path: str | None = None
    result_path: Path | None = None
    rollout_path: Path | None = None
    checkpoint_ref: str | None = None
    checkpoint_sha: str | None = None
    checkpoint_tree_sha: str | None = None
    base_sha: str | None = None
    result_excerpt: str | None = None
    result_text: str | None = None
    verification_bundle: dict[str, Any] | None = None
    checkpoint_error: str | None = None
    slot_released: bool = False


@dataclass(frozen=True)
class ReviewInboxItem:
    item_id: str
    source_kind: str
    source_id: str
    source_status: str
    source_completed_at: str | None
    delivery_status: str
    review_status: str
    task_id: str | None
    route: str | None
    workspace_path: Path | None
    slot_name: str | None
    parent_thread_id: str | None
    agent_path: str | None
    result_path: Path | None
    rollout_path: Path | None
    checkpoint_ref: str | None
    checkpoint_sha: str | None
    checkpoint_tree_sha: str | None
    base_sha: str | None
    result_excerpt: str | None
    verification_bundle: dict[str, Any] | None
    checkpoint_error: str | None
    slot_released: bool
    created_at: str
    updated_at: str
    reviewed_at: str | None
    result_text: str | None = None
    result_sha256: str | None = None
    verification_state: str | None = None
    verification_schema: int | None = None
    verification_json: dict[str, Any] | None = None
    verification_sha256: str | None = None
    payload_captured_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_status": self.source_status,
            "source_completed_at": self.source_completed_at,
            "delivery_status": self.delivery_status,
            "review_status": self.review_status,
            "task_id": self.task_id,
            "route": self.route,
            "workspace_path": str(self.workspace_path) if self.workspace_path else None,
            "slot_name": self.slot_name,
            "parent_thread_id": self.parent_thread_id,
            "agent_path": self.agent_path,
            "result_path": str(self.result_path) if self.result_path else None,
            "rollout_path": str(self.rollout_path) if self.rollout_path else None,
            "checkpoint_ref": self.checkpoint_ref,
            "checkpoint_sha": self.checkpoint_sha,
            "checkpoint_tree_sha": self.checkpoint_tree_sha,
            "base_sha": self.base_sha,
            "result_excerpt": self.result_excerpt,
            "verification_bundle": self.verification_bundle,
            "checkpoint_error": self.checkpoint_error,
            "slot_released": self.slot_released,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "result_text": self.result_text,
            "result_sha256": self.result_sha256,
            "verification_state": self.verification_state,
            "verification_schema": self.verification_schema,
            "verification_json": self.verification_json,
            "verification_sha256": self.verification_sha256,
            "payload_captured_at": self.payload_captured_at,
        }


class ReviewInboxStore:
    def __init__(self, database_path: Path, *, excerpt_limit: int = 4000) -> None:
        if excerpt_limit <= 0:
            raise ValueError("excerpt_limit must be positive")
        self.database_path = database_path
        self.excerpt_limit = excerpt_limit

    def initialize(self) -> None:
        apply_schema_migration(
            self.database_path,
            component="review_inbox_store",
            version=1,
            checksum="review-inbox-store-v1-20260715",
            migrate=self._migrate_schema,
        )
        apply_schema_migration(
            self.database_path,
            component="review_inbox_store",
            version=2,
            checksum="review-inbox-payloads-v2-20260715",
            migrate=self._migrate_payload_schema,
        )

    @staticmethod
    def _migrate_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists review_inbox_items (
                    item_id text primary key,
                    source_kind text not null,
                    source_id text not null,
                    source_status text not null,
                    source_completed_at text,
                    delivery_status text not null,
                    review_status text not null default 'pending',
                    task_id text,
                    route text,
                    workspace_path text,
                    slot_name text,
                    parent_thread_id text,
                    agent_path text,
                    result_path text,
                    rollout_path text,
                    checkpoint_ref text,
                    checkpoint_sha text,
                    checkpoint_tree_sha text,
                    base_sha text,
                    result_excerpt text,
                    verification_bundle_json text,
                    checkpoint_error text,
                    slot_released integer not null default 0,
                    created_at text not null,
                    updated_at text not null,
                    reviewed_at text,
                    unique(source_kind, source_id)
                )
            """
        )
        ReviewInboxStore._ensure_item_schema(db)

    @staticmethod
    def _migrate_payload_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists review_inbox_payloads (
                item_id text primary key
                    references review_inbox_items(item_id) on delete cascade,
                result_text text not null,
                result_sha256 text not null,
                verification_state text not null
                    check (verification_state in ('valid', 'missing', 'invalid')),
                verification_schema integer,
                verification_json text,
                verification_sha256 text,
                captured_at text not null
            )
            """
        )
        ReviewInboxStore._ensure_item_schema(db)

    @staticmethod
    def _ensure_item_schema(db: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in db.execute("pragma table_info(review_inbox_items)").fetchall()
        }
        if "source_completed_at" not in columns:
            db.execute("alter table review_inbox_items add column source_completed_at text")
        if "verification_bundle_json" not in columns:
            db.execute("alter table review_inbox_items add column verification_bundle_json text")
        db.execute(
            """
            create index if not exists review_inbox_pending_idx
            on review_inbox_items(review_status, updated_at desc)
            """
        )
        db.execute(
            """
            create index if not exists review_inbox_review_completed_idx
            on review_inbox_items(review_status, source_completed_at desc)
            """
        )
        db.execute(
            """
            create index if not exists review_inbox_parent_completed_idx
            on review_inbox_items(parent_thread_id, review_status, source_completed_at desc)
            """
        )

    def upsert(self, draft: ReviewInboxDraft) -> ReviewInboxItem:
        source_kind = _required("source_kind", draft.source_kind)
        source_id = _required("source_id", draft.source_id)
        source_status = _required("source_status", draft.source_status)
        delivery_status = _required("delivery_status", draft.delivery_status)
        item_id = f"{source_kind}:{source_id}"
        now = utc_now()
        excerpt = _bounded(draft.result_excerpt, self.excerpt_limit)
        result_text = draft.result_text if draft.result_text is not None else draft.result_excerpt
        result_text = result_text or ""
        result_sha256 = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
        verification = _verification_payload(draft.verification_bundle)
        self.initialize()
        with self._connect() as db:
            db.execute("begin immediate")
            db.execute(
                """
                insert into review_inbox_items (
                    item_id, source_kind, source_id, source_status, source_completed_at,
                    delivery_status,
                    review_status, task_id, route, workspace_path, slot_name,
                    parent_thread_id, agent_path, result_path, rollout_path,
                    checkpoint_ref, checkpoint_sha, checkpoint_tree_sha, base_sha,
                    result_excerpt, verification_bundle_json, checkpoint_error, slot_released,
                    created_at, updated_at, reviewed_at
                )
                values (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)
                on conflict(source_kind, source_id) do update set
                    source_status = excluded.source_status,
                    source_completed_at = excluded.source_completed_at,
                    delivery_status = excluded.delivery_status,
                    task_id = excluded.task_id,
                    route = excluded.route,
                    workspace_path = excluded.workspace_path,
                    slot_name = excluded.slot_name,
                    parent_thread_id = excluded.parent_thread_id,
                    agent_path = excluded.agent_path,
                    result_path = excluded.result_path,
                    rollout_path = excluded.rollout_path,
                    checkpoint_ref = excluded.checkpoint_ref,
                    checkpoint_sha = excluded.checkpoint_sha,
                    checkpoint_tree_sha = excluded.checkpoint_tree_sha,
                    base_sha = excluded.base_sha,
                    result_excerpt = excluded.result_excerpt,
                    verification_bundle_json = excluded.verification_bundle_json,
                    checkpoint_error = excluded.checkpoint_error,
                    slot_released = excluded.slot_released,
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    source_kind,
                    source_id,
                    source_status,
                    draft.source_completed_at,
                    delivery_status,
                    draft.task_id,
                    draft.route,
                    _path_text(draft.workspace_path),
                    draft.slot_name,
                    draft.parent_thread_id,
                    draft.agent_path,
                    _path_text(draft.result_path),
                    _path_text(draft.rollout_path),
                    draft.checkpoint_ref,
                    draft.checkpoint_sha,
                    draft.checkpoint_tree_sha,
                    draft.base_sha,
                    excerpt,
                    _json_text(draft.verification_bundle),
                    draft.checkpoint_error,
                    int(draft.slot_released),
                    now,
                    now,
                ),
            )
            db.execute(
                """
                insert into review_inbox_payloads (
                    item_id, result_text, result_sha256, verification_state,
                    verification_schema, verification_json, verification_sha256,
                    captured_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(item_id) do update set
                    result_text = excluded.result_text,
                    result_sha256 = excluded.result_sha256,
                    verification_state = excluded.verification_state,
                    verification_schema = excluded.verification_schema,
                    verification_json = excluded.verification_json,
                    verification_sha256 = excluded.verification_sha256,
                    captured_at = excluded.captured_at
                """,
                (
                    item_id,
                    result_text,
                    result_sha256,
                    verification["state"],
                    verification["schema_version"],
                    _json_text(verification["payload"]),
                    verification["sha256"],
                    now,
                ),
            )
        return self.get(item_id)

    def get(self, item_id: str) -> ReviewInboxItem:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                """
                select i.*,
                    p.result_text as payload_result_text,
                    p.result_sha256 as payload_result_sha256,
                    p.verification_state as payload_verification_state,
                    p.verification_schema as payload_verification_schema,
                    p.verification_json as payload_verification_json,
                    p.verification_sha256 as payload_verification_sha256,
                    p.captured_at as payload_captured_at
                from review_inbox_items i
                left join review_inbox_payloads p on p.item_id = i.item_id
                where i.item_id = ?
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Review inbox item not found: {item_id}")
        return _item_from_row(row)

    def list_items(
        self,
        *,
        review_status: str | None = "pending",
        parent_thread_id: str | None = None,
        limit: int = 50,
    ) -> list[ReviewInboxItem]:
        if review_status is not None and review_status not in REVIEW_STATUSES:
            expected = ", ".join(sorted(REVIEW_STATUSES))
            raise ValueError(f"review_status must be one of {expected}, or None")
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.initialize()
        with self._connect() as db:
            if review_status is None and parent_thread_id is None:
                rows = db.execute(
                    """
                    select * from review_inbox_items
                    order by coalesce(source_completed_at, updated_at) desc, item_id limit ?
                    """,
                    (limit,),
                ).fetchall()
            elif review_status is None:
                rows = db.execute(
                    """
                    select * from review_inbox_items
                    where parent_thread_id = ?
                    order by coalesce(source_completed_at, updated_at) desc, item_id limit ?
                    """,
                    (parent_thread_id, limit),
                ).fetchall()
            elif parent_thread_id is None:
                rows = db.execute(
                    """
                    select * from review_inbox_items
                    where review_status = ?
                    order by coalesce(source_completed_at, updated_at) desc, item_id limit ?
                    """,
                    (review_status, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    select * from review_inbox_items
                    where review_status = ? and parent_thread_id = ?
                    order by coalesce(source_completed_at, updated_at) desc, item_id limit ?
                    """,
                    (review_status, parent_thread_id, limit),
                ).fetchall()
        return [_item_from_row(row) for row in rows]

    def resolve(self, item_id: str, decision: str) -> ReviewInboxItem:
        if decision not in REVIEW_DECISIONS:
            expected = ", ".join(sorted(REVIEW_DECISIONS))
            raise ValueError(f"decision must be one of: {expected}")
        self.initialize()
        now = utc_now()
        with self._connect() as db:
            existing = db.execute(
                """
                select i.verification_bundle_json, p.verification_state
                from review_inbox_items i
                left join review_inbox_payloads p on p.item_id = i.item_id
                where i.item_id = ?
                """,
                (item_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"Review inbox item not found: {item_id}")
            if decision == "accepted":
                bundle = _json_object(existing["verification_bundle_json"])
                review_ready = bundle.get("review_ready") if bundle is not None else None
                if existing["verification_state"] != "valid" or review_ready is not True:
                    raise ValueError(
                        "Review item cannot be accepted until verification is valid and review-ready"
                    )
            cursor = db.execute(
                """
                update review_inbox_items
                set review_status = ?, reviewed_at = ?, updated_at = ?
                where item_id = ?
                """,
                (decision, now, now, item_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Review inbox item not found: {item_id}")
        return self.get(item_id)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with control_database(self.database_path) as db:
            yield db


def _item_from_row(row: sqlite3.Row) -> ReviewInboxItem:
    columns = set(row.keys())
    return ReviewInboxItem(
        item_id=row["item_id"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        source_status=row["source_status"],
        source_completed_at=row["source_completed_at"],
        delivery_status=row["delivery_status"],
        review_status=row["review_status"],
        task_id=row["task_id"],
        route=row["route"],
        workspace_path=_optional_path(row["workspace_path"]),
        slot_name=row["slot_name"],
        parent_thread_id=row["parent_thread_id"],
        agent_path=row["agent_path"],
        result_path=_optional_path(row["result_path"]),
        rollout_path=_optional_path(row["rollout_path"]),
        checkpoint_ref=row["checkpoint_ref"],
        checkpoint_sha=row["checkpoint_sha"],
        checkpoint_tree_sha=row["checkpoint_tree_sha"],
        base_sha=row["base_sha"],
        result_excerpt=row["result_excerpt"],
        verification_bundle=_json_object(row["verification_bundle_json"]),
        checkpoint_error=row["checkpoint_error"],
        slot_released=bool(row["slot_released"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reviewed_at=row["reviewed_at"],
        result_text=row["payload_result_text"] if "payload_result_text" in columns else None,
        result_sha256=(
            row["payload_result_sha256"] if "payload_result_sha256" in columns else None
        ),
        verification_state=(
            row["payload_verification_state"]
            if "payload_verification_state" in columns
            else None
        ),
        verification_schema=(
            row["payload_verification_schema"]
            if "payload_verification_schema" in columns
            else None
        ),
        verification_json=(
            _json_object(row["payload_verification_json"])
            if "payload_verification_json" in columns
            else None
        ),
        verification_sha256=(
            row["payload_verification_sha256"]
            if "payload_verification_sha256" in columns
            else None
        ),
        payload_captured_at=(
            row["payload_captured_at"] if "payload_captured_at" in columns else None
        ),
    )


def _required(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _path_text(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _json_text(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _json_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("verification_bundle_json must contain a JSON object")
    return payload


def _verification_payload(bundle: dict[str, Any] | None) -> dict[str, Any]:
    worker = bundle.get("worker_verification") if isinstance(bundle, dict) else None
    if not isinstance(worker, dict):
        return {
            "state": "missing",
            "schema_version": None,
            "payload": None,
            "sha256": None,
        }
    state = worker.get("state")
    if state not in {"valid", "missing", "invalid"}:
        state = "invalid"
    payload = worker.get("payload") if isinstance(worker.get("payload"), dict) else None
    return {
        "state": state,
        "schema_version": (
            worker.get("schema_version")
            if isinstance(worker.get("schema_version"), int)
            else None
        ),
        "payload": payload,
        "sha256": worker.get("sha256") if isinstance(worker.get("sha256"), str) else None,
    }
