from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.shared.agent_backends import (
    AGY_BACKEND,
    CODEX_BACKEND,
    CODEX_SPARK_BACKEND,
    SUPPORTED_BACKENDS,
    normalize_backend,
)

__all__ = [
    "AGY_BACKEND",
    "CODEX_BACKEND",
    "CODEX_SPARK_BACKEND",
    "SUPPORTED_BACKENDS",
    "AgentRunResult",
    "AgentRunSpec",
    "AgentRunner",
    "normalize_backend",
]


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
    codex_no_progress_timeout_sec: int = 0
    codex_forbidden_tool_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    completed: bool
    exit_code: int | None
    result_status: str | None
    message: str
    metrics: AttemptMetrics | None = None


class AgentRunner(Protocol):
    def run(
        self,
        spec: AgentRunSpec,
        *,
        cancel_requested: Callable[[], bool],
        pid_observed: Callable[[int | None], None],
    ) -> AgentRunResult:
        """Run one bounded agent attempt and return a normalized result."""
