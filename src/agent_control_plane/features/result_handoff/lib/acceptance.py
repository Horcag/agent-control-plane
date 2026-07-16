from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_control_plane.entities.job import ReviewMetricsStore
from agent_control_plane.entities.plan import PlanStore
from agent_control_plane.entities.review_inbox import ReviewInboxStore
from agent_control_plane.shared.sqlite_runtime import control_database


class HandoffAcceptanceService:
    """Commit inbox, plan, and review-accounting acceptance as one SQLite unit."""

    def __init__(
        self,
        database_path: Path,
        *,
        plan_store: PlanStore,
        review_inbox: ReviewInboxStore,
        review_metrics: ReviewMetricsStore,
    ) -> None:
        expected = database_path.resolve(strict=False)
        stores = (plan_store, review_inbox, review_metrics)
        if any(store.database_path.resolve(strict=False) != expected for store in stores):
            raise ValueError("Atomic handoff stores must share one SQLite database")
        self.database_path = expected
        self.plan_store = plan_store
        self.review_inbox = review_inbox
        self.review_metrics = review_metrics

    def accept(
        self,
        plan_id: str,
        task_id: str,
        *,
        review_span_id: str,
        accepted_sha: str | None = None,
        attempt_no: int | None = None,
        defects_found: int = 0,
        false_positives: int = 0,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.plan_store.initialize()
        self.review_inbox.initialize()
        self.review_metrics.initialize()
        with control_database(self.database_path) as db:
            db.execute("begin immediate")
            target = self.plan_store.review_target_in_transaction(db, plan_id, task_id)
            job_id = target.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError(f"Plan task has no bound job: {plan_id}/{task_id}")
            item_id = f"agent_job:{job_id}"
            inbox_row = db.execute(
                """
                select source_kind, source_id, checkpoint_sha
                from review_inbox_items where item_id = ?
                """,
                (item_id,),
            ).fetchone()
            if inbox_row is None:
                raise KeyError(f"Review inbox item not found: {item_id}")
            if inbox_row["source_kind"] != "agent_job" or inbox_row["source_id"] != job_id:
                raise ValueError(f"Review inbox item does not belong to plan job: {item_id}")
            effective_sha = accepted_sha or inbox_row["checkpoint_sha"]

            self.review_inbox.resolve_in_transaction(db, item_id, "accepted")
            self.plan_store.accept_task_in_transaction(
                db,
                plan_id,
                task_id,
                accepted_sha=effective_sha,
            )
            self.review_metrics.attach_job_in_transaction(
                db,
                review_span_id,
                job_id=job_id,
                outcome="accepted",
                attempt_no=attempt_no,
                root_verified=True,
                accepted_sha=effective_sha,
                defects_found=defects_found,
                false_positives=false_positives,
                notes=notes,
            )

        return {
            "status": "accepted",
            "plan_id": plan_id,
            "task_id": task_id,
            "job_id": job_id,
            "item_id": item_id,
            "review_span_id": review_span_id,
            "accepted_sha": effective_sha,
            "inbox_status": self.review_inbox.get(item_id).review_status,
            "plan": self.plan_store.snapshot(plan_id),
        }
