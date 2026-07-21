from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Protocol

from agent_control_plane.entities.job import JobRecord
from agent_control_plane.entities.plan import PlanExecutionSpec

CIRCUIT_BREAKING_RUNNER_FAILURES = frozenset({"tool_call_budget", "inefficient_tool_usage"})
CIRCUIT_BREAKING_STATUSES = frozenset({"tool_call_budget", "inefficient_tool_usage"})


def retry_fingerprint(
    *,
    brief_sha256: str,
    effective_scope_sha256: str,
    tool_call_budget: int | None,
) -> str:
    """Identity of a would-be attempt: identical inputs mean an identical doomed retry."""
    payload = f"{brief_sha256}\n{effective_scope_sha256}\n{tool_call_budget}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_scope_json(scope: Sequence[str]) -> str:
    normalized = sorted({entry.strip() for entry in scope if entry and entry.strip()})
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def fingerprint_from_spec(spec: PlanExecutionSpec, *, brief_override: str | None = None) -> str:
    """Fingerprint of the attempt a retry of `spec` would actually run."""
    brief = brief_override if brief_override is not None else spec.brief
    brief_sha256 = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    effective_scope_sha256 = hashlib.sha256(
        _canonical_scope_json(spec.effective_scope).encode("utf-8")
    ).hexdigest()
    return retry_fingerprint(
        brief_sha256=brief_sha256,
        effective_scope_sha256=effective_scope_sha256,
        tool_call_budget=spec.codex_tool_call_budget,
    )


def fingerprint_from_job(job: JobRecord) -> str:
    """Fingerprint of the attempt a prior job actually ran, for comparison against a retry."""
    if not job.brief_sha256:
        raise ValueError(f"Job has no brief_sha256 recorded: {job.job_id}")
    effective_scope_sha256 = hashlib.sha256(job.effective_scope_json.encode("utf-8")).hexdigest()
    return retry_fingerprint(
        brief_sha256=job.brief_sha256,
        effective_scope_sha256=effective_scope_sha256,
        tool_call_budget=job.codex_tool_call_budget,
    )


def is_circuit_breaking_failure(*, runner_failure: str | None, status: str) -> bool:
    """True for tool-call-budget / inefficient-tool-usage failures; false for e.g. rate limits."""
    if runner_failure in CIRCUIT_BREAKING_RUNNER_FAILURES:
        return True
    return status in CIRCUIT_BREAKING_STATUSES


def circuit_breaker_streak(jobs: Sequence[JobRecord]) -> tuple[int, str | None]:
    """Length (and fingerprint) of the trailing run of same-fingerprint circuit-breaking
    failures in `jobs`, given in chronological (oldest-first) order."""
    streak = 0
    streak_fingerprint: str | None = None
    for job in jobs:
        if not is_circuit_breaking_failure(runner_failure=job.runner_failure, status=job.status):
            streak = 0
            streak_fingerprint = None
            continue
        fingerprint = fingerprint_from_job(job)
        if fingerprint == streak_fingerprint:
            streak += 1
        else:
            streak_fingerprint = fingerprint
            streak = 1
    return streak, streak_fingerprint


class _JobLookup(Protocol):
    def get_job(self, job_id: str) -> JobRecord: ...


class _TaskJobHistory(Protocol):
    def job_ids_for_task(self, plan_id: str, task_id: str) -> list[str]: ...


def circuit_breaker_state(
    plan_store: _TaskJobHistory,
    job_store: _JobLookup,
    plan_id: str,
    task_id: str,
) -> tuple[bool, str | None]:
    """(needs_strategy_revision, escalated_fingerprint) for a task, derived from its durable
    job history: two consecutive same-fingerprint circuit-breaking failures escalate it."""
    jobs: list[JobRecord] = []
    for job_id in plan_store.job_ids_for_task(plan_id, task_id):
        try:
            jobs.append(job_store.get_job(job_id))
        except KeyError:
            continue
    streak, fingerprint = circuit_breaker_streak(jobs)
    needs_strategy_revision = streak >= 2
    return needs_strategy_revision, fingerprint if needs_strategy_revision else None
