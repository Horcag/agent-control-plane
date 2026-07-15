from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_control_plane.entities.review_inbox import ReviewInboxDraft, ReviewInboxStore


def test_review_inbox_upsert_is_idempotent_and_does_not_reset_review(tmp_path: Path) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3")
    original = store.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-1",
            source_status="completed",
            delivery_status="checkpointed",
            task_id="task-1",
            route="app",
            workspace_path=tmp_path / "slot",
            slot_name="app-1",
            checkpoint_ref="refs/agent-control-plane/jobs/abc",
            checkpoint_sha="a" * 40,
            result_excerpt="first result",
            verification_bundle=_valid_bundle(review_ready=True),
        )
    )

    accepted = store.resolve(original.item_id, "accepted")
    updated = store.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-1",
            source_status="completed",
            delivery_status="checkpointed",
            task_id="task-1",
            route="app",
            workspace_path=tmp_path / "slot",
            slot_name="app-1",
            checkpoint_ref="refs/agent-control-plane/jobs/abc",
            checkpoint_sha="a" * 40,
            result_excerpt="updated result",
            verification_bundle=_valid_bundle(review_ready=True),
            slot_released=True,
        )
    )

    assert accepted.review_status == "accepted"
    assert updated.item_id == original.item_id
    assert updated.review_status == "accepted"
    assert updated.result_excerpt == "updated result"
    assert updated.verification_bundle == _valid_bundle(review_ready=True)
    assert updated.slot_released is True
    assert len(store.list_items(review_status=None)) == 1


def test_review_inbox_lists_pending_items_and_bounds_result_excerpt(tmp_path: Path) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3", excerpt_limit=32)
    pending = store.upsert(
        ReviewInboxDraft(
            source_kind="codex_subagent",
            source_id="thread-1",
            source_status="completed",
            delivery_status="ready",
            result_excerpt="x" * 100,
        )
    )
    store.upsert(
        ReviewInboxDraft(
            source_kind="codex_subagent",
            source_id="thread-2",
            source_status="completed",
            delivery_status="ready",
            result_excerpt="done",
        )
    )
    store.resolve("codex_subagent:thread-2", "rejected")

    items = store.list_items(review_status="pending")

    assert [item.item_id for item in items] == [pending.item_id]
    assert pending.result_excerpt is not None
    assert len(pending.result_excerpt) == 32
    assert pending.result_excerpt.endswith("...")


def test_review_inbox_can_filter_deliveries_by_parent_thread(tmp_path: Path) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3")
    for source_id, parent_thread_id in (("one", "parent-1"), ("two", "parent-2")):
        store.upsert(
            ReviewInboxDraft(
                source_kind="codex_subagent",
                source_id=source_id,
                source_status="completed",
                delivery_status="ready",
                parent_thread_id=parent_thread_id,
            )
        )

    items = store.list_items(review_status="pending", parent_thread_id="parent-1")

    assert [item.source_id for item in items] == ["one"]


def test_review_inbox_orders_items_by_source_completion_time(tmp_path: Path) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3")
    for source_id, completed_at in (
        ("older", "2026-07-15T10:00:00+00:00"),
        ("newer", "2026-07-15T11:00:00+00:00"),
    ):
        store.upsert(
            ReviewInboxDraft(
                source_kind="codex_subagent",
                source_id=source_id,
                source_status="completed",
                source_completed_at=completed_at,
                delivery_status="ready",
            )
        )

    assert [item.source_id for item in store.list_items()] == ["newer", "older"]


def test_review_inbox_list_is_bounded_but_get_returns_full_durable_payload(
    tmp_path: Path,
) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3", excerpt_limit=32)
    full_result = "Status: completed\n" + ("detail\n" * 2000)
    bundle = {
        "schema_version": 1,
        "review_ready": True,
        "worker_verification": {
            "state": "valid",
            "schema_version": 1,
            "payload": {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [],
                "checks": [],
                "unverified": [],
            },
            "sha256": "a" * 64,
        },
    }
    created = store.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-full",
            source_status="completed",
            delivery_status="ready",
            result_excerpt=full_result,
            result_text=full_result,
            verification_bundle=bundle,
        )
    )

    listed = store.list_items(review_status=None)[0]
    shown = store.get(created.item_id)

    assert listed.result_text is None
    assert listed.result_excerpt is not None and len(listed.result_excerpt) == 32
    assert shown.result_text == full_result
    assert shown.result_sha256 == hashlib.sha256(full_result.encode()).hexdigest()
    assert shown.verification_state == "valid"
    assert shown.verification_json == bundle["worker_verification"]["payload"]


def test_review_inbox_replay_preserves_decision_and_payload_hash(tmp_path: Path) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3")
    draft = ReviewInboxDraft(
        source_kind="agent_job",
        source_id="job-replay",
        source_status="completed",
        delivery_status="ready",
        result_text="Status: completed\n",
    )
    first = store.upsert(draft)
    store.resolve(first.item_id, "rejected")
    replayed = store.upsert(draft)

    assert replayed.review_status == "rejected"
    assert replayed.result_sha256 == first.result_sha256


def test_review_inbox_missing_verification_blocks_normal_acceptance(tmp_path: Path) -> None:
    store = ReviewInboxStore(tmp_path / "jobs.sqlite3")
    item = store.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id="job-unverified",
            source_status="completed",
            delivery_status="ready",
            result_text="Status: completed\n",
        )
    )

    with pytest.raises(ValueError, match="verification"):
        store.resolve(item.item_id, "accepted")


def _valid_bundle(*, review_ready: bool) -> dict:
    return {
        "schema_version": 1,
        "review_ready": review_ready,
        "worker_verification": {
            "state": "valid",
            "schema_version": 1,
            "payload": {
                "schema_version": 1,
                "status": "completed",
                "changed_files": [],
                "checks": [],
                "unverified": [],
            },
            "sha256": "a" * 64,
        },
    }
