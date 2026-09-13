from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.shared.agent_backends import (
    AGY_BACKEND,
    CLAUDE_BACKEND,
    CODEX_BACKEND,
    CODEX_SPARK_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)

__all__ = [
    "AGY_BACKEND",
    "CLAUDE_BACKEND",
    "CODEX_BACKEND",
    "CODEX_SPARK_BACKEND",
    "SUPPORTED_BACKENDS",
    "AgentRunResult",
    "AgentRunSpec",
    "AgentRunner",
    "AgyLaunchSpec",
    "BudgetLifecycleEvent",
    "normalize_backend",
]


@dataclass(frozen=True)
class AgyLaunchSpec:
    """The non-secret, configured launch contract for one AGY attempt.

    ``launch_id`` identifies this local launch attempt only.  It is deliberately
    not a conversation/session identifier and must never be used as CONNECT
    evidence.
    """

    executable: str
    managed: bool
    add_new_project: bool
    adapter_path: Path | None
    adapter_sha256: str | None
    launch_id: str
    job_id: str
    attempt_ref: str

    def receipt(self) -> dict[str, str | bool | None]:
        return {
            "version": "agy-launch-receipt-v1",
            "managed": self.managed,
            "executable": self.executable,
            "adapter_path": str(self.adapter_path) if self.adapter_path else None,
            "adapter_sha256": self.adapter_sha256,
            "launch_id": self.launch_id,
            "job_id": self.job_id,
            "attempt_ref": self.attempt_ref,
        }


@dataclass(frozen=True)
class AgentRunSpec:
    backend: str
    agy_command: str
    codex_command: str
    codex_model: str
    codex_reasoning_effort: str
    codex_sandbox_mode: str
    codex_disabled_mcp_servers: tuple[str, ...]
    prompt: str
    workspace_path: Path
    result_path: Path
    log_path: Path
    print_timeout: str
    timeout_sec: int
    idle_timeout_sec: int
    yolo: bool
    read_only: bool
    agy_model: str | None = None
    no_progress_timeout_sec: int = 0
    tool_timeout_limit: int = 6
    tool_call_budget: int = 0
    tool_call_budget_grace_sec: int = 120
    invalid_verification_grace_sec: int = 120
    codex_terminal_tab_name: str | None = None
    codex_forbidden_tool_markers: tuple[str, ...] = ()
    codex_resume_thread_id: str | None = None
    codex_sessions_root: Path | None = None
    workspace_access: str = "ide_mcp"
    claude_command: str = "claude"
    claude_model: str | None = None
    claude_reasoning_effort: str | None = None
    claude_permission_mode: str = "acceptEdits"
    claude_allowed_tools: tuple[str, ...] = ()
    claude_sessions_root: Path | None = None
    claude_max_turns: int = 0
    claude_bare: bool = True
    claude_mcp_config_path: Path | None = None
    agy_launch: AgyLaunchSpec | None = None


@dataclass(frozen=True)
class BudgetLifecycleEvent:
    kind: str
    observed_call_count: int
    message: str


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    completed: bool
    exit_code: int | None
    result_status: str | None
    message: str
    metrics: AttemptMetrics | None = None
    escalation_classification: str | None = None
    lifecycle_events: tuple[BudgetLifecycleEvent, ...] = ()


class AgentRunner(Protocol):
    def run(
        self,
        spec: AgentRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        pid_observed: Callable[[int | None], None],
    ) -> AgentRunResult:
        """Run one bounded agent attempt and return a normalized result."""
