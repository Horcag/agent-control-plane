from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_control_plane.entities.job import JobStore
from agent_control_plane.entities.plan import PlanExecutionSpec, PlanStore, PlanTaskDefinition
from agent_control_plane.entities.review_inbox import ReviewInboxDraft, ReviewInboxStore
from agent_control_plane.features.lifecycle_cleanup import RetentionService
from agent_control_plane.shared.git_tools import GitError, run_git

OLD_TIMESTAMP = datetime.fromtimestamp(1, UTC).isoformat(timespec="seconds")
NOW_TIMESTAMP = datetime(2026, 7, 15, tzinfo=UTC).timestamp()


def test_plan_cancel_stops_dispatch_and_tracks_running_jobs(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    job = _create_job(jobs, tmp_path, "job-running")
    jobs.update_job(job.job_id, status="running", started_at=OLD_TIMESTAMP)
    plans.create_plan(
        plan_id="transfer",
        title="Transfer",
        tasks=(
            PlanTaskDefinition("schema", "Schema"),
            PlanTaskDefinition("api", "API", depends_on=("schema",)),
        ),
    )
    plans.bind_job("transfer", "schema", job.job_id)

    cancellation = plans.request_cancel("transfer")
    snapshot = plans.snapshot("transfer")

    assert cancellation["status"] == "cancelling"
    assert cancellation["active_job_ids"] == [job.job_id]
    assert snapshot["status"] == "cancelling"
    assert {item["task_id"]: item["state"] for item in snapshot["blocked"]} == {"api": "cancelled"}
    with pytest.raises(ValueError, match="is not active"):
        plans.claim_ready_tasks("transfer")

    jobs.update_job(
        job.job_id,
        status="cancelled",
        finished_at=OLD_TIMESTAMP,
        finalization_status="completed",
    )

    assert plans.snapshot("transfer")["status"] == "cancelled"


def test_plan_leaves_cancelling_when_bound_job_finished_before_cancel_landed(
    tmp_path: Path,
) -> None:
    """Regression: a cancel that raced a finishing worker stranded the job at
    ``cancel_requested`` with ``finished_at`` set. That row is excluded from
    reconciliation, so it used to pin the owning plan in ``cancelling`` forever.
    The lifecycle sweep must treat a finished-and-finalized job as inactive
    regardless of its clobbered status string.
    """
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    job = _create_job(jobs, tmp_path, "job-raced")
    plans.create_plan(
        plan_id="wave",
        title="Wave",
        tasks=(PlanTaskDefinition("task", "Task"),),
    )
    plans.bind_job("wave", "task", job.job_id)

    assert plans.request_cancel("wave")["status"] == "cancelling"

    # Reproduce the stranded row exactly: finished + finalized, yet the status
    # string was clobbered back to cancel_requested by the racing cancel.
    jobs.update_job(
        job.job_id,
        status="cancel_requested",
        cancel_requested=True,
        finished_at=OLD_TIMESTAMP,
        finalization_status="completed",
    )

    assert plans.snapshot("wave")["status"] == "cancelled"


def test_plan_archive_is_explicit_terminal_state_and_hidden_by_default(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()
    plans = PlanStore(database)
    plans.create_plan(plan_id="active", title="Active")

    with pytest.raises(ValueError, match="must be completed or cancelled"):
        plans.archive_plan("active")

    plans.request_cancel("active")
    archived = plans.archive_plan("active")

    assert archived.status == "cancelled"
    assert archived.archived_at is not None
    assert plans.list_plans() == []
    assert plans.list_plans(include_archived=True)[0]["plan_id"] == "active"
    assert plans.list_plans(include_archived=True)[0]["archived_at"] == archived.archived_at


def test_plan_cancel_fences_a_dispatch_claim_that_already_passed_validation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="race",
        title="Race",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="main", brief="Do it"),
            ),
        ),
    )
    claim = plans.claim_ready_tasks("race")[0]
    plans.assert_dispatch_claim(
        "race",
        "task",
        dispatch_token=claim.dispatch_token,
        dispatch_task_id=claim.dispatch_task_id,
    )

    cancellation = plans.request_cancel("race")
    created = _create_job(jobs, tmp_path, "raced-job")
    plans.bind_dispatched_job(
        "race",
        "task",
        dispatch_token=claim.dispatch_token,
        job_id=created.job_id,
    )

    assert cancellation["status"] == "cancelling"
    assert jobs.get_job(created.job_id).status == "cancel_requested"
    assert jobs.get_job(created.job_id).cancel_requested is True
    assert plans.snapshot("race")["running"][0]["state"] == "cancel_requested"


def test_plan_archive_refuses_unresolved_bound_handoff(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    inbox = ReviewInboxStore(database)
    job = _create_job(jobs, tmp_path, "pending-job")
    jobs.update_job(
        job.job_id,
        status="completed",
        finished_at=OLD_TIMESTAMP,
        finalization_status="completed",
    )
    plans.create_plan(
        plan_id="pending-review",
        title="Pending review",
        tasks=(PlanTaskDefinition("task", "Task"),),
    )
    plans.bind_job("pending-review", "task", job.job_id)
    inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id=job.job_id,
            source_status="completed",
            delivery_status="ready",
            result_text="review me",
        )
    )
    plans.request_cancel("pending-review")

    with pytest.raises(ValueError, match="pending inbox items"):
        plans.archive_plan("pending-review")


def test_retention_gc_is_dry_run_then_prunes_only_archived_resolved_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    inbox = ReviewInboxStore(database)
    repo = _git_repo(tmp_path / "repo")
    checkpoint_sha = run_git(repo, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/retention-test"
    run_git(repo, "update-ref", checkpoint_ref, checkpoint_sha)

    plans.create_plan(plan_id="old-plan", title="Old plan")
    plans.request_cancel("old-plan")
    plans.archive_plan("old-plan")
    job = _create_job(jobs, tmp_path, "job-old", workspace_path=repo)
    jobs.update_job(
        job.job_id,
        status="completed",
        finished_at=OLD_TIMESTAMP,
        finalization_status="completed",
        archived_at=OLD_TIMESTAMP,
    )
    jobs.add_event(job.job_id, "info", "old event")
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id=job.job_id,
            source_status="completed",
            source_completed_at=OLD_TIMESTAMP,
            delivery_status="checkpointed",
            workspace_path=repo,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=checkpoint_sha,
            checkpoint_tree_sha=run_git(repo, "rev-parse", f"{checkpoint_sha}^{{tree}}"),
            base_sha=checkpoint_sha,
            result_excerpt="large result",
            result_text="large result payload",
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "rejected")
    pending = inbox.upsert(
        ReviewInboxDraft(
            source_kind="codex_subagent",
            source_id="pending-subagent",
            source_status="completed",
            delivery_status="ready",
            result_text="must stay",
        )
    )
    _age_rows(database, plan_id="old-plan", item_id=item.item_id)
    retention = RetentionService(
        database,
        plan_store=plans,
        job_store=jobs,
        review_inbox=inbox,
        clock=lambda: NOW_TIMESTAMP,
    )

    dry_run = retention.collect(older_than_days=1, limit=100, apply=False)

    assert dry_run["counts"] == {
        "plans": 1,
        "plan_events": 4,
        "job_events": 1,
        "inbox_payloads": 1,
        "checkpoint_refs": 1,
        "orphaned_events": 0,
    }
    assert plans.get_plan("old-plan").archived_at is not None
    assert jobs.recent_events(job.job_id) != []
    assert inbox.get(item.item_id).result_text == "large result payload"
    assert inbox.get(pending.item_id).result_text == "must stay"
    assert run_git(repo, "show-ref", "--verify", "--hash", checkpoint_ref) == checkpoint_sha

    applied = retention.collect(older_than_days=1, limit=100, apply=True)

    assert applied["applied"] == dry_run["counts"]
    with pytest.raises(KeyError, match="Plan not found"):
        plans.get_plan("old-plan")
    assert jobs.recent_events(job.job_id) == []
    pruned = inbox.get(item.item_id)
    assert pruned.result_text is None
    assert pruned.verification_bundle is None
    assert pruned.checkpoint_ref is None
    assert inbox.get(pending.item_id).result_text == "must stay"
    with pytest.raises(GitError):
        run_git(repo, "show-ref", "--verify", checkpoint_ref)


def test_retention_refuses_to_delete_a_checkpoint_ref_when_sha_changed(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    plans = PlanStore(database)
    inbox = ReviewInboxStore(database)
    repo = _git_repo(tmp_path / "repo")
    expected_sha = run_git(repo, "rev-parse", "HEAD")
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    run_git(repo, "add", "second.txt")
    run_git(repo, "commit", "-m", "second")
    actual_sha = run_git(repo, "rev-parse", "HEAD")
    checkpoint_ref = "refs/agent-control-plane/jobs/mismatch-test"
    run_git(repo, "update-ref", checkpoint_ref, actual_sha)
    job = _create_job(jobs, tmp_path, "job-mismatch", workspace_path=repo)
    jobs.update_job(
        job.job_id,
        status="completed",
        finished_at=OLD_TIMESTAMP,
        finalization_status="completed",
        archived_at=OLD_TIMESTAMP,
    )
    item = inbox.upsert(
        ReviewInboxDraft(
            source_kind="agent_job",
            source_id=job.job_id,
            source_status="completed",
            delivery_status="checkpointed",
            workspace_path=repo,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha=expected_sha,
            result_text="result",
            slot_released=True,
        )
    )
    inbox.resolve(item.item_id, "rejected")
    _age_rows(database, item_id=item.item_id)

    result = RetentionService(
        database,
        plan_store=plans,
        job_store=jobs,
        review_inbox=inbox,
        clock=lambda: NOW_TIMESTAMP,
    ).collect(older_than_days=1, apply=True)

    assert result["applied"]["checkpoint_refs"] == 0
    assert result["blocked_checkpoint_refs"][0]["reason"] == "sha_mismatch"
    assert inbox.get(item.item_id).checkpoint_ref == checkpoint_ref
    assert run_git(repo, "show-ref", "--verify", "--hash", checkpoint_ref) == actual_sha


def _create_job(
    store: JobStore,
    root: Path,
    job_id: str,
    *,
    workspace_path: Path | None = None,
):
    return store.create_job(
        job_id=job_id,
        task_id=f"task-{job_id}",
        route="main",
        workspace_path=workspace_path or root / "repo",
        expected_branch="main",
        config_path=root / "workspaces.toml",
        run_dir=root / "runs" / job_id,
        prompt_path=root / "runs" / job_id / "prompt.md",
        result_path=root / "tasks" / job_id / "result.md",
        timeout_sec=60,
        idle_timeout_sec=30,
        print_timeout="1m",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.email", "retention@example.com"], path)
    _run(["git", "config", "user.name", "Retention Test"], path)
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], path)
    _run(["git", "commit", "-m", "initial"], path)
    return path


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)  # nosec B603


def _age_rows(database: Path, *, plan_id: str | None = None, item_id: str | None = None) -> None:
    with sqlite3.connect(database) as db:
        if plan_id is not None:
            db.execute(
                "update plans set archived_at = ?, updated_at = ? where plan_id = ?",
                (OLD_TIMESTAMP, OLD_TIMESTAMP, plan_id),
            )
        if item_id is not None:
            db.execute(
                """
                update review_inbox_items
                set reviewed_at = ?, updated_at = ? where item_id = ?
                """,
                (OLD_TIMESTAMP, OLD_TIMESTAMP, item_id),
            )
            db.execute(
                "update review_inbox_payloads set captured_at = ? where item_id = ?",
                (OLD_TIMESTAMP, item_id),
            )
