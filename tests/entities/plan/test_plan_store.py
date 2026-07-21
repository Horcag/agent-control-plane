from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agent_control_plane.entities.job import JobStore, ReviewMetricsStore
from agent_control_plane.entities.plan import (
    PlanExecutionSpec,
    PlanStore,
    PlanTaskDefinition,
)
from agent_control_plane.features.agent_runner import (
    capture_process_identity,
    process_is_alive,
    supports_verified_process_termination,
    terminate_verified_process,
)
from agent_control_plane.features.plan_supervision import PlanDispatcher
from agent_control_plane.shared.codex_session_usage import TokenUsage


def test_plan_snapshot_exposes_only_dependency_ready_tasks() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        store = _plan_store(root)

        store.create_plan(
            plan_id="transfer",
            title="Main to dev transfer",
            objective="Restore product parity",
            tasks=(
                PlanTaskDefinition("schema", "Transfer schema"),
                PlanTaskDefinition("api", "Transfer API", depends_on=("schema",)),
            ),
        )

        snapshot = store.snapshot("transfer")

        assert snapshot["progress"] == "0/2"
        assert [task["task_id"] for task in snapshot["ready_next"]] == ["schema"]
        assert snapshot["counts"] == {"pending": 1, "ready": 1}
        assert snapshot["changes"] == []
        assert snapshot["cursor"] > 0


def test_completed_job_waits_for_root_acceptance_before_unlocking_dependents() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(
                PlanTaskDefinition("schema", "Transfer schema"),
                PlanTaskDefinition("api", "Transfer API", depends_on=("schema",)),
            ),
        )
        initial_cursor = plans.snapshot("transfer")["cursor"]
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        plans.bind_job("transfer", "schema", "schema-job")
        jobs.mark_finished("schema-job", "completed")

        finalizing = plans.snapshot("transfer", since=initial_cursor)

        assert finalizing["awaiting_review"] == []
        assert finalizing["running"][0]["state"] == "finalizing"

        jobs.mark_finalization_completed("schema-job")

        awaiting_review = plans.snapshot("transfer", since=initial_cursor)

        assert awaiting_review["ready_next"] == []
        assert awaiting_review["awaiting_review"][0]["job_id"] == "schema-job"
        assert awaiting_review["requires_root_decision"][0]["task_id"] == "schema"
        review_cursor = awaiting_review["cursor"]

        plans.accept_task("transfer", "schema", accepted_sha="abc123")
        accepted = plans.snapshot("transfer", since=review_cursor)

        assert accepted["completed"] == [
            {
                "task_id": "schema",
                "job_id": "schema-job",
                "accepted_sha": "abc123",
            }
        ]
        assert [task["task_id"] for task in accepted["ready_next"]] == ["api"]
        assert accepted["progress"] == "1/2"
        assert plans.snapshot("transfer", since=accepted["cursor"])["changes"] == []


def test_plan_manifest_rejects_unknown_dependencies_and_cycles() -> None:
    with tempfile.TemporaryDirectory() as temp:
        store = _plan_store(Path(temp))

        with pytest.raises(ValueError, match="unknown task"):
            store.create_plan(
                plan_id="unknown",
                title="Unknown dependency",
                tasks=(PlanTaskDefinition("api", "API", depends_on=("schema",)),),
            )

        with pytest.raises(ValueError, match="cycle"):
            store.create_plan(
                plan_id="cycle",
                title="Cycle",
                tasks=(
                    PlanTaskDefinition("schema", "Schema", depends_on=("api",)),
                    PlanTaskDefinition("api", "API", depends_on=("schema",)),
                ),
            )


def test_manual_job_binding_cannot_bypass_plan_dependencies() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(
                PlanTaskDefinition("schema", "Schema"),
                PlanTaskDefinition("api", "API", depends_on=("schema",)),
            ),
        )
        _create_job(jobs, root, job_id="api-job", task_id="api-run")

        with pytest.raises(ValueError, match="dependencies are incomplete"):
            plans.bind_job("transfer", "api", "api-job")


def test_root_verified_review_outcome_automatically_accepts_plan_task() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        reviews = ReviewMetricsStore(root / "jobs.sqlite3")
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(PlanTaskDefinition("schema", "Schema"),),
        )
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        plans.bind_job("transfer", "schema", "schema-job")
        jobs.mark_finished("schema-job", "completed")
        jobs.mark_finalization_completed("schema-job")
        span_id = reviews.start_span(
            span_id="review-transfer",
            name="Transfer review",
            session_path=root / "rollout.jsonl",
            usage=TokenUsage(0, 0, 0, 0),
        )
        reviews.attach_job(
            span_id,
            job_id="schema-job",
            outcome="accepted",
            root_verified=True,
            accepted_sha="abc123",
        )

        snapshot = plans.snapshot("transfer")

        assert snapshot["progress"] == "1/1"
        assert snapshot["status"] == "completed"


def test_plan_invariants_cover_active_jobs_decisions_and_current_projection() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(
                PlanTaskDefinition("schema", "Schema"),
                PlanTaskDefinition("api", "API", depends_on=("schema",)),
            ),
        )
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        _create_job(jobs, root, job_id="schema-retry", task_id="schema-retry-run")
        plans.bind_job("transfer", "schema", "schema-job")
        jobs.update_job("schema-job", status="waiting_quota")

        with pytest.raises(ValueError, match="active job"):
            plans.bind_job("transfer", "schema", "schema-retry")

        snapshot = plans.snapshot("transfer")
        assert snapshot["running"][0]["job_status"] == "waiting_quota"


def test_root_decisions_require_completed_job_and_are_immutable() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(PlanTaskDefinition("schema", "Schema"),),
        )
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        plans.bind_job("transfer", "schema", "schema-job")
        jobs.mark_finished("schema-job", "failed")
        jobs.mark_finalization_completed("schema-job")

        with pytest.raises(ValueError, match="eligible completed worker"):
            plans.accept_task("transfer", "schema")

        _create_job(jobs, root, job_id="schema-retry", task_id="schema-retry-run")
        plans.bind_job("transfer", "schema", "schema-retry")
        jobs.mark_finished("schema-retry", "completed")
        jobs.mark_finalization_completed("schema-retry")
        plans.accept_task("transfer", "schema", accepted_sha="abc123")

        with pytest.raises(ValueError, match="already accepted"):
            plans.reject_task("transfer", "schema")


def test_active_root_verified_outcome_does_not_unlock_dependants() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        reviews = ReviewMetricsStore(root / "jobs.sqlite3")
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(
                PlanTaskDefinition("schema", "Schema"),
                PlanTaskDefinition("api", "API", depends_on=("schema",)),
            ),
        )
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        plans.bind_job("transfer", "schema", "schema-job")
        span_id = reviews.start_span(
            span_id="review-transfer",
            name="Transfer review",
            session_path=root / "rollout.jsonl",
            usage=TokenUsage(0, 0, 0, 0),
        )
        reviews.attach_job(
            span_id,
            job_id="schema-job",
            outcome="accepted",
            root_verified=True,
            accepted_sha="abc123",
        )

        snapshot = plans.snapshot("transfer")

        assert snapshot["progress"] == "0/2"
        assert snapshot["ready_next"] == []
        assert snapshot["running"][0]["task_id"] == "schema"


def test_continuation_verified_keeps_dependents_locked_and_is_retryable() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        reviews = ReviewMetricsStore(root / "jobs.sqlite3")
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(
                PlanTaskDefinition(
                    "schema",
                    "Schema",
                    execution=PlanExecutionSpec(route="dev", brief="Retry the schema task"),
                ),
                PlanTaskDefinition("api", "API", depends_on=("schema",)),
            ),
        )
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        plans.bind_job("transfer", "schema", "schema-job")
        jobs.mark_finished("schema-job", "completed")
        jobs.mark_finalization_completed("schema-job")
        span_id = reviews.start_span(
            name="Continuation", session_path=root / "rollout.jsonl", usage=TokenUsage(0, 0, 0, 0)
        )
        reviews.attach_job(
            span_id,
            job_id="schema-job",
            outcome="continuation_verified",
            root_verified=True,
            checkpoint_sha="checkpoint",
        )

        snapshot = plans.snapshot("transfer")

        assert snapshot["status"] == "active"
        assert snapshot["requires_root_decision"][0]["task_id"] == "schema"
        assert snapshot["requires_root_decision"][0]["state"] == "partial"
        assert snapshot["ready_next"] == []
        assert plans.retry_task("transfer", "schema")["state"] == "ready"


def test_snapshot_exposes_completed_task_identity_and_truncation() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        jobs = JobStore(root / "jobs.sqlite3")
        jobs.initialize()
        plans.create_plan(
            plan_id="transfer",
            title="Transfer",
            tasks=(PlanTaskDefinition("schema", "Schema"),),
        )
        _create_job(jobs, root, job_id="schema-job", task_id="schema-run")
        plans.bind_job("transfer", "schema", "schema-job")
        jobs.mark_finished("schema-job", "completed")
        jobs.mark_finalization_completed("schema-job")
        plans.accept_task("transfer", "schema", accepted_sha="abc123")

        snapshot = plans.snapshot("transfer", item_limit=1)

        assert snapshot["progress"] == "1/1"
        assert snapshot["completed_tasks"] == [
            {"task_id": "schema", "job_id": "schema-job", "accepted_sha": "abc123"}
        ]
        assert snapshot["item_counts"]["completed_tasks"] == 1
        assert snapshot["truncated"]["completed_tasks"] is False


def test_plan_manifest_rejects_duplicate_dependencies_and_duplicate_plan_ids() -> None:
    with tempfile.TemporaryDirectory() as temp:
        store = _plan_store(Path(temp))
        definitions = (PlanTaskDefinition("schema", "Schema"),)
        store.create_plan(plan_id="transfer", title="Transfer", tasks=definitions)

        with pytest.raises(ValueError, match="duplicate dependency"):
            store.create_plan(
                plan_id="duplicate-dependency",
                title="Duplicate dependency",
                tasks=(
                    PlanTaskDefinition("schema", "Schema"),
                    PlanTaskDefinition("api", "API", depends_on=("schema", "schema")),
                ),
            )
        with pytest.raises(ValueError, match="already exists"):
            store.create_plan(plan_id="transfer", title="Transfer", tasks=definitions)


def test_executable_task_spec_round_trips_without_returning_full_brief() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plans = _plan_store(root)
        plans.create_plan(
            plan_id="dispatch",
            title="Dispatch",
            tasks=(
                PlanTaskDefinition(
                    "schema",
                    "Schema",
                    execution=PlanExecutionSpec(
                        route="dev",
                        brief="secret implementation brief",
                        backend="codex",
                        workspace_access="native",
                        codex_quality_tier="mechanical",
                        expected_base_sha="A" * 40,
                        effective_scope=(" tests/api.py ", "src/api.py", "src/api.py"),
                        codex_tool_call_budget=47,
                        retry_override_reason=" approved retry ",
                    ),
                ),
            ),
        )

        task = plans.snapshot("dispatch")["ready_next"][0]

        assert task["execution"] == {
            "route": "dev",
            "slot": None,
            "backend": "codex",
            "workspace_access": "native",
            "read_only": False,
            "codex_quality_tier": "mechanical",
            "codex_premium_override_reason": None,
            "codex_model": None,
            "codex_reasoning_effort": None,
            "claude_model": None,
            "claude_reasoning_effort": None,
            "expected_result_status": "completed",
            "controller_gate_mode": "full",
            "expected_base_sha": "a" * 40,
            "effective_scope": ["src/api.py", "tests/api.py"],
            "effective_scope_sha256": "1ccf7bddcd58584eb1450b2315be9f3a9f5765f526bf975abff4ed8a43891831",
            "codex_tool_call_budget": 47,
            "retry_override_reason": "approved retry",
            "brief_sha256": "2d3f668e501d9979fac44adb78c7cf3b970a83cba93a0d62d0a769caf31b884d",
            "brief_chars": 27,
        }
        assert "secret implementation brief" not in str(task)


def test_executable_task_snapshot_retains_premium_override_reason_without_brief(
    tmp_path: Path,
) -> None:
    plans = _plan_store(tmp_path)
    plans.create_plan(
        plan_id="premium",
        title="Premium",
        tasks=(
            PlanTaskDefinition(
                "schema",
                "Schema",
                execution=PlanExecutionSpec(
                    route="dev",
                    brief="secret premium brief",
                    backend="codex",
                    codex_model="gpt-5.6-sol",
                    codex_premium_override_reason="approved benchmark",
                ),
            ),
        ),
    )

    execution = plans.snapshot("premium")["ready_next"][0]["execution"]

    assert execution["codex_premium_override_reason"] == "approved benchmark"
    assert "secret premium brief" not in str(execution)


def test_legacy_execution_json_without_plan_contract_fields_loads_as_none(tmp_path: Path) -> None:
    plans = _plan_store(tmp_path)
    plans.create_plan(
        plan_id="legacy",
        title="Legacy",
        tasks=(
            PlanTaskDefinition(
                "schema",
                "Schema",
                execution=PlanExecutionSpec(route="dev", brief="legacy plan"),
            ),
        ),
    )
    legacy_payload = json.dumps({"route": "dev", "brief": "legacy plan", "backend": "codex"})

    with sqlite3.connect(plans.database_path) as database:
        database.execute(
            """
            update plan_tasks
            set execution_json = ?
            where plan_id = 'legacy' and task_id = 'schema'
            """,
            (legacy_payload,),
        )
        database.commit()

    task = plans.snapshot("legacy")["ready_next"][0]
    assert task["execution"]["codex_model"] is None
    assert task["execution"]["codex_reasoning_effort"] is None
    assert task["execution"]["expected_result_status"] == "completed"
    assert task["execution"]["controller_gate_mode"] == "full"
    assert task["execution"]["expected_base_sha"] is None
    assert task["execution"]["effective_scope"] == []
    assert task["execution"]["codex_tool_call_budget"] is None
    assert task["execution"]["retry_override_reason"] is None


def test_ready_task_claim_is_atomic_across_two_plan_store_instances(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()
    first = PlanStore(database)
    second = PlanStore(database)
    first.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="dev", brief="Do it"),
            ),
        ),
    )

    first_claims = first.claim_ready_tasks("dispatch", limit=1)
    second_claims = second.claim_ready_tasks("dispatch", limit=1)

    assert len(first_claims) == 1
    assert second_claims == []
    assert first.snapshot("dispatch")["running"][0]["state"] == "dispatching"


def test_cross_process_sqlite_lock_serializes_one_durable_dispatch_claim(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="dev", brief="Do it"),
            ),
        ),
    )
    ready_path = tmp_path / "lock-ready"
    release_path = tmp_path / "lock-release"
    lock_script = """
import sqlite3
import sys
import time
from pathlib import Path

db = sqlite3.connect(sys.argv[1])
db.execute("begin exclusive")
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
release = Path(sys.argv[3])
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
db.commit()
db.close()
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    locker = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", lock_script, str(database), str(ready_path), str(release_path)],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    claim_script = (
        "import sys; from pathlib import Path; "
        "from agent_control_plane.entities.plan import PlanStore; "
        "Path(sys.argv[2]).write_text('started', encoding='utf-8'); "
        "claims = PlanStore(Path(sys.argv[1])).claim_ready_tasks('dispatch', limit=1); "
        "Path(sys.argv[3]).write_text(str(len(claims)), encoding='utf-8')"
    )
    claimant: subprocess.Popen[str] | None = None
    try:
        _wait_for_file(ready_path, locker)
        claimant = subprocess.Popen(  # nosec B603
            [
                sys.executable,
                "-c",
                claim_script,
                str(database),
                str(tmp_path / "claim-started"),
                str(tmp_path / "claim-count"),
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for_file(tmp_path / "claim-started", claimant)
        assert claimant.poll() is None
        release_path.write_text("release", encoding="utf-8")
        locker.wait(timeout=10)
        claimant.wait(timeout=10)
        assert (tmp_path / "claim-count").read_text(encoding="utf-8") == "1"
        assert PlanStore(database).claim_ready_tasks("dispatch", limit=1) == []
    finally:
        release_path.touch(exist_ok=True)
        for process in (claimant, locker):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_failed_task_requires_explicit_retry_before_dispatch(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(
                    route="dev",
                    brief="First attempt",
                    codex_model="retry-model",
                    codex_reasoning_effort="max",
                    expected_result_status="partial",
                    controller_gate_mode="focused",
                    expected_base_sha="B" * 40,
                    effective_scope=("tests/retry.py", "src/retry.py"),
                    codex_tool_call_budget=31,
                    retry_override_reason="transient infrastructure failure",
                ),
            ),
        ),
    )
    _create_job(jobs, tmp_path, job_id="failed-job", task_id="failed-run")
    plans.bind_job("dispatch", "task", "failed-job")
    jobs.mark_finished("failed-job", "failed")
    jobs.mark_finalization_completed("failed-job")

    assert plans.claim_ready_tasks("dispatch", limit=1) == []
    retried = plans.retry_task("dispatch", "task", brief_override="Second attempt")
    claims = plans.claim_ready_tasks("dispatch", limit=1)

    assert retried["state"] == "ready"
    assert claims[0].attempt_no == 1
    assert claims[0].execution.brief == "Second attempt"
    assert claims[0].execution.codex_model == "retry-model"
    assert claims[0].execution.codex_reasoning_effort == "max"
    assert claims[0].execution.expected_result_status == "partial"
    assert claims[0].execution.controller_gate_mode == "focused"
    assert claims[0].execution.expected_base_sha == "b" * 40
    assert claims[0].execution.effective_scope == ("src/retry.py", "tests/retry.py")
    assert claims[0].execution.codex_tool_call_budget == 31
    assert claims[0].execution.retry_override_reason == "transient infrastructure failure"


@pytest.mark.parametrize("terminal_status", ("contract_mismatch", "inefficient_tool_usage"))
def test_terminal_failure_is_retryable_and_does_not_unlock_dependents(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="contract",
        title="Contract",
        tasks=(
            PlanTaskDefinition(
                "first", "First", execution=PlanExecutionSpec(route="dev", brief="Do it")
            ),
            PlanTaskDefinition("next", "Next", depends_on=("first",)),
        ),
    )
    _create_job(jobs, tmp_path, job_id="mismatch-job", task_id="first-run")
    plans.bind_job("contract", "first", "mismatch-job")
    jobs.mark_finished("mismatch-job", terminal_status)
    jobs.mark_finalization_completed("mismatch-job")

    snapshot = plans.snapshot("contract")

    assert snapshot["blocked"][0]["state"] == terminal_status
    assert snapshot["ready_next"] == []
    assert plans.retry_task("contract", "first")["state"] == "ready"


def test_dispatch_claim_token_is_required_to_bind_created_job(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    jobs = JobStore(database)
    jobs.initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="dev", brief="Do it"),
            ),
        ),
    )
    claim = plans.claim_ready_tasks("dispatch")[0]
    _create_job(jobs, tmp_path, job_id="created-job", task_id=claim.dispatch_task_id)

    with pytest.raises(ValueError, match="stale"):
        plans.bind_dispatched_job(
            "dispatch",
            "task",
            dispatch_token="wrong-token",
            job_id="created-job",
        )

    plans.assert_dispatch_claim(
        "dispatch",
        "task",
        dispatch_token=claim.dispatch_token,
        dispatch_task_id=claim.dispatch_task_id,
    )
    plans.bind_dispatched_job(
        "dispatch",
        "task",
        dispatch_token=claim.dispatch_token,
        job_id="created-job",
    )

    running = plans.snapshot("dispatch")["running"][0]
    assert running["job_id"] == "created-job"
    assert running["attempt_no"] == 1


def test_dispatch_failure_is_durable_and_not_automatically_reclaimed(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="dev", brief="Do it"),
            ),
        ),
    )
    claim = plans.claim_ready_tasks("dispatch")[0]

    assert plans.mark_dispatch_failed(
        "dispatch",
        "task",
        dispatch_token=claim.dispatch_token,
        error="slot unavailable",
    )
    assert plans.claim_ready_tasks("dispatch") == []
    blocked = plans.snapshot("dispatch")["blocked"][0]
    assert blocked["state"] == "dispatch_failed"
    assert blocked["dispatch_error"] == "slot unavailable"


def test_dead_dispatch_owner_is_reconciled_to_explicit_retry_state(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="dev", brief="Do it"),
            ),
        ),
    )
    plans.claim_ready_tasks("dispatch", owner_pid=424242, limit=1)

    recovered = plans.reconcile_orphaned_dispatches(
        "dispatch",
        process_is_alive=lambda _pid: False,
    )

    assert recovered == ["task"]
    assert plans.snapshot("dispatch")["blocked"][0]["state"] == "dispatch_failed"
    assert plans.claim_ready_tasks("dispatch") == []


@pytest.mark.skipif(
    not supports_verified_process_termination(),
    reason="OS has no safe exact-process termination primitive",
)
def test_dispatch_reconciles_coordinator_killed_after_claim_before_launch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()
    plans = PlanStore(database)
    plans.create_plan(
        plan_id="dispatch",
        title="Dispatch",
        tasks=(
            PlanTaskDefinition(
                "task",
                "Task",
                execution=PlanExecutionSpec(route="dev", brief="Do it"),
            ),
        ),
    )
    ready_path = tmp_path / "dispatch-ready"
    release_path = tmp_path / "dispatch-release"
    helper_script = """
import sys
import time
from pathlib import Path

from agent_control_plane.entities.plan import PlanStore
from agent_control_plane.features.plan_supervision import PlanDispatcher

database, coordination_root, ready_path, release_path = map(Path, sys.argv[1:])

def launch(_claim):
    ready_path.write_text("claimed", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not release_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    raise RuntimeError("test barrier released")

PlanDispatcher(
    plan_store=PlanStore(database),
    coordination_root=coordination_root,
    launch=launch,
    process_is_alive=lambda _pid: False,
).dispatch("dispatch")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    coordinator = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-c",
            helper_script,
            str(database),
            str(tmp_path / ".agent-work"),
            str(ready_path),
            str(release_path),
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    identity = None
    try:
        _wait_for_file(ready_path, coordinator)
        identity = capture_process_identity(coordinator.pid)
        assert identity is not None

        terminated = terminate_verified_process(identity)
        coordinator.wait(timeout=5)

        assert terminated.pid == coordinator.pid
        assert terminated.state.value == "terminated"
        recovered = PlanDispatcher(
            plan_store=PlanStore(database),
            coordination_root=tmp_path / ".agent-work",
            launch=lambda _claim: pytest.fail("recovery must not relaunch an abandoned claim"),
            process_is_alive=process_is_alive,
        ).dispatch("dispatch")

        assert recovered["reconciled_dispatches"] == ["task"]
        assert recovered["claimed"] == 0
        assert plans.snapshot("dispatch")["blocked"][0]["state"] == "dispatch_failed"
        assert plans.retry_task("dispatch", "task")["state"] == "ready"
    finally:
        release_path.touch(exist_ok=True)
        if identity is not None and capture_process_identity(identity.pid) == identity:
            terminate_verified_process(identity)
        if coordinator.poll() is None:
            coordinator.terminate()
            coordinator.wait(timeout=5)


def test_plan_store_initialization_preserves_legacy_v4_and_registers_v5(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    JobStore(database).initialize()

    with sqlite3.connect(database) as db:
        db.execute(
            """
            insert into schema_migrations (component, version, checksum, applied_at)
            values ('plan_store', 4, 'plan-slot-allocation-v4-20260716', '2026-07-16T00:00:00+00:00')
            """
        )
        db.commit()

    PlanStore(database).initialize()

    with sqlite3.connect(database) as db:
        migration_checksums = dict(
            db.execute(
                "select version, checksum from schema_migrations where component = 'plan_store'"
            ).fetchall()
        )

    assert migration_checksums[4] == "plan-slot-allocation-v4-20260716"
    assert migration_checksums[5] == "plan-execution-contract-v5-20260717"
    assert migration_checksums[6] == "plan-controller-contract-v6-20260719"


def _plan_store(root: Path) -> PlanStore:
    jobs = JobStore(root / "jobs.sqlite3")
    jobs.initialize()
    return PlanStore(root / "jobs.sqlite3")


def _create_job(
    store: JobStore,
    root: Path,
    *,
    job_id: str,
    task_id: str,
) -> None:
    result_path = root / "tasks" / task_id / "result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        "Status: completed\n\nChanged files: src/schema.py\n\nVerification performed: tests passed\n",
        encoding="utf-8",
    )
    store.create_job(
        job_id=job_id,
        task_id=task_id,
        route="dev",
        workspace_path=root / "workspace",
        expected_branch="codex/schema",
        config_path=root / "workspaces.toml",
        run_dir=root / "runs" / job_id,
        prompt_path=root / "runs" / job_id / "prompt.md",
        result_path=result_path,
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        backend="codex",
    )


def _wait_for_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"Lock holder exited before ready: {stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError("Lock holder did not become ready")
        time.sleep(0.05)
