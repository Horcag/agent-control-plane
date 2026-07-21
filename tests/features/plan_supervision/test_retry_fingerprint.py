from __future__ import annotations

from agent_control_plane.entities.plan import PlanExecutionSpec
from agent_control_plane.features.plan_supervision.lib.retry_fingerprint import (
    fingerprint_from_spec,
    is_circuit_breaking_failure,
    retry_fingerprint,
)


def _spec(**overrides: object) -> PlanExecutionSpec:
    fields = {
        "route": "acp",
        "brief": "do the thing",
        "effective_scope": ("src/a.py", "src/b.py"),
        "codex_tool_call_budget": 40,
    }
    fields.update(overrides)
    return PlanExecutionSpec(**fields)  # type: ignore[arg-type]


def test_retry_fingerprint_is_deterministic() -> None:
    first = retry_fingerprint(brief_sha256="a", effective_scope_sha256="b", tool_call_budget=5)
    second = retry_fingerprint(brief_sha256="a", effective_scope_sha256="b", tool_call_budget=5)
    assert first == second
    assert len(first) == 64


def test_fingerprint_from_spec_ignores_scope_ordering() -> None:
    ordered = _spec(effective_scope=("src/a.py", "src/b.py"))
    reordered = _spec(effective_scope=("src/b.py", "src/a.py"))
    assert fingerprint_from_spec(ordered) == fingerprint_from_spec(reordered)


def test_fingerprint_from_spec_brief_override_changes_fingerprint() -> None:
    spec = _spec()
    assert fingerprint_from_spec(spec) != fingerprint_from_spec(spec, brief_override="different")


def test_is_circuit_breaking_failure_excludes_rate_limit() -> None:
    assert is_circuit_breaking_failure(runner_failure="tool_call_budget", status="failed")
    assert is_circuit_breaking_failure(runner_failure=None, status="inefficient_tool_usage")
    assert not is_circuit_breaking_failure(
        runner_failure="rate_limit", status="stopped_dirty_after_failure"
    )
