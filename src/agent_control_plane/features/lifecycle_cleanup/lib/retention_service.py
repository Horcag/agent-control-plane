from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.plan import PlanStore
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.shared.git_tools import GitError, run_git
from agent_control_plane.shared.sqlite_runtime import control_database

CHECKPOINT_REF_PREFIX = "refs/agent-control-plane/jobs/"


class RetentionService:
    """Prune only explicitly archived, reviewed ACP state after a retention window."""

    def __init__(
        self,
        database_path: Path,
        *,
        plan_store: PlanStore,
        job_store: JobStore,
        review_inbox: ReviewInboxStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = database_path
        self.plan_store = plan_store
        self.job_store = job_store
        self.review_inbox = review_inbox
        self.clock = clock

    def collect(
        self,
        *,
        older_than_days: int = 30,
        limit: int = 500,
        apply: bool = False,
    ) -> dict[str, Any]:
        if older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.job_store.initialize()
        self.plan_store.initialize()
        self.review_inbox.initialize()
        cutoff = datetime.fromtimestamp(
            self.clock() - older_than_days * 24 * 60 * 60,
            UTC,
        ).isoformat(timespec="seconds")
        candidates = self._candidates(cutoff, limit)
        checkpoint_results = [
            self._checkpoint_decision(candidate, apply=apply)
            for candidate in candidates["checkpoint_refs"]
        ]
        counts = {
            "plans": len(candidates["plans"]),
            "plan_events": sum(int(item["event_count"]) for item in candidates["plans"]),
            "job_events": sum(int(item["row_count"]) for item in candidates["job_events"]),
            "inbox_payloads": len(candidates["inbox_payloads"]),
            "checkpoint_refs": len(candidates["checkpoint_refs"]),
            "orphaned_events": len(candidates["orphaned_events"]),
        }
        applied = {name: 0 for name in counts}
        if apply:
            applied = self._apply_database_cleanup(candidates, checkpoint_results)
        blocked = [item for item in checkpoint_results if item["action"] == "blocked"]
        return {
            "apply": apply,
            "cutoff": cutoff,
            "limit_per_category": limit,
            "counts": counts,
            "applied": applied,
            "blocked_checkpoint_refs": blocked,
            "candidates": {
                **candidates,
                "checkpoint_refs": checkpoint_results,
            },
        }

    def _candidates(self, cutoff: str, limit: int) -> dict[str, list[dict[str, Any]]]:
        with control_database(self.database_path) as db:
            plans = _rows(
                db,
                """
                select p.plan_id, p.status, p.archived_at,
                    (select count(*) from plan_events e where e.plan_id = p.plan_id)
                        as event_count
                from plans p
                where p.archived_at is not null and p.archived_at <= ?
                order by p.archived_at, p.plan_id limit ?
                """,
                (cutoff, limit),
            )
            job_events = _rows(
                db,
                """
                select e.job_id, count(*) as row_count
                from events e join jobs j on j.job_id = e.job_id
                where j.archived_at is not null and j.archived_at <= ?
                group by e.job_id order by min(e.id) limit ?
                """,
                (cutoff, limit),
            )
            inbox_payloads = _rows(
                db,
                """
                select i.item_id, i.source_kind, i.source_id
                from review_inbox_items i
                join review_inbox_payloads p on p.item_id = i.item_id
                where i.review_status != 'pending'
                  and coalesce(i.reviewed_at, i.updated_at) <= ?
                  and (
                    i.source_kind != 'agent_job'
                    or exists (
                        select 1 from jobs j where j.job_id = i.source_id
                          and j.archived_at is not null and j.archived_at <= ?
                    )
                  )
                order by coalesce(i.reviewed_at, i.updated_at), i.item_id limit ?
                """,
                (cutoff, cutoff, limit),
            )
            checkpoint_refs = _rows(
                db,
                """
                select i.item_id, i.checkpoint_ref, i.checkpoint_sha, i.workspace_path
                from review_inbox_items i join jobs j on j.job_id = i.source_id
                where i.source_kind = 'agent_job' and i.review_status != 'pending'
                  and coalesce(i.reviewed_at, i.updated_at) <= ?
                  and i.checkpoint_ref is not null and i.checkpoint_sha is not null
                  and j.archived_at is not null and j.archived_at <= ?
                order by coalesce(i.reviewed_at, i.updated_at), i.item_id limit ?
                """,
                (cutoff, cutoff, limit),
            )
            orphaned_events = _rows(
                db,
                """
                select original_event_id from orphaned_events
                where quarantined_at <= ? order by original_event_id limit ?
                """,
                (cutoff, limit),
            )
        return {
            "plans": plans,
            "job_events": job_events,
            "inbox_payloads": inbox_payloads,
            "checkpoint_refs": checkpoint_refs,
            "orphaned_events": orphaned_events,
        }

    def _checkpoint_decision(
        self,
        candidate: dict[str, Any],
        *,
        apply: bool,
    ) -> dict[str, Any]:
        decision = dict(candidate)
        ref_name = str(candidate["checkpoint_ref"])
        expected_sha = str(candidate["checkpoint_sha"])
        raw_workspace = candidate.get("workspace_path")
        if not ref_name.startswith(CHECKPOINT_REF_PREFIX):
            return {**decision, "action": "blocked", "reason": "unsafe_ref_namespace"}
        if not raw_workspace:
            return {**decision, "action": "blocked", "reason": "workspace_missing"}
        workspace = Path(str(raw_workspace))
        if not workspace.exists():
            return {**decision, "action": "blocked", "reason": "workspace_missing"}
        try:
            run_git(workspace, "rev-parse", "--git-dir")
        except GitError:
            return {**decision, "action": "blocked", "reason": "workspace_not_git"}
        try:
            actual_sha = run_git(workspace, "show-ref", "--verify", "--hash", ref_name)
        except GitError:
            return {**decision, "action": "missing", "reason": "already_absent"}
        if actual_sha != expected_sha:
            return {
                **decision,
                "action": "blocked",
                "reason": "sha_mismatch",
                "actual_sha": actual_sha,
            }
        if not apply:
            return {**decision, "action": "would_delete"}
        try:
            run_git(workspace, "update-ref", "-d", ref_name, expected_sha)
        except GitError as exc:
            return {**decision, "action": "blocked", "reason": "delete_failed", "error": str(exc)}
        return {**decision, "action": "deleted"}

    def _apply_database_cleanup(
        self,
        candidates: dict[str, list[dict[str, Any]]],
        checkpoint_results: list[dict[str, Any]],
    ) -> dict[str, int]:
        plan_ids = [str(item["plan_id"]) for item in candidates["plans"]]
        event_job_ids = [str(item["job_id"]) for item in candidates["job_events"]]
        payload_ids = [str(item["item_id"]) for item in candidates["inbox_payloads"]]
        cleared_refs = [
            item for item in checkpoint_results if item["action"] in {"deleted", "missing"}
        ]
        orphan_ids = [int(item["original_event_id"]) for item in candidates["orphaned_events"]]
        with control_database(self.database_path) as db:
            db.execute("begin immediate")
            deleted_plan_events = _count_in(db, "plan_events", "plan_id", plan_ids)
            deleted_plans = _delete_in(db, "plans", "plan_id", plan_ids)
            deleted_events = _delete_in(db, "events", "job_id", event_job_ids)
            deleted_payloads = _delete_in(db, "review_inbox_payloads", "item_id", payload_ids)
            if payload_ids:
                placeholders = ", ".join("?" for _ in payload_ids)
                db.execute(
                    f"""
                    update review_inbox_items set verification_bundle_json = null
                    where item_id in ({placeholders})
                    """,  # nosec B608
                    payload_ids,
                )
            cleared_count = 0
            for item in cleared_refs:
                cursor = db.execute(
                    """
                    update review_inbox_items set checkpoint_ref = null
                    where item_id = ? and checkpoint_ref = ? and checkpoint_sha = ?
                    """,
                    (item["item_id"], item["checkpoint_ref"], item["checkpoint_sha"]),
                )
                cleared_count += cursor.rowcount
            deleted_orphans = _delete_in(
                db,
                "orphaned_events",
                "original_event_id",
                orphan_ids,
            )
        return {
            "plans": deleted_plans,
            "plan_events": deleted_plan_events,
            "job_events": deleted_events,
            "inbox_payloads": deleted_payloads,
            "checkpoint_refs": cleared_count,
            "orphaned_events": deleted_orphans,
        }


def _rows(
    db: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any],
) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, parameters).fetchall()]


def _delete_in(
    db: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[str | int],
) -> int:
    if not values:
        return 0
    allowed = {
        ("plans", "plan_id"),
        ("events", "job_id"),
        ("review_inbox_payloads", "item_id"),
        ("orphaned_events", "original_event_id"),
    }
    if (table, column) not in allowed:
        raise ValueError(f"Unsupported retention target: {table}.{column}")
    placeholders = ", ".join("?" for _ in values)
    cursor = db.execute(
        f"delete from {table} where {column} in ({placeholders})",  # nosec B608
        tuple(values),
    )
    return cursor.rowcount


def _count_in(
    db: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[str | int],
) -> int:
    if not values:
        return 0
    if (table, column) != ("plan_events", "plan_id"):
        raise ValueError(f"Unsupported retention count target: {table}.{column}")
    placeholders = ", ".join("?" for _ in values)
    row = db.execute(
        f"select count(*) as count from {table} where {column} in ({placeholders})",  # nosec B608
        tuple(values),
    ).fetchone()
    return int(row["count"])
