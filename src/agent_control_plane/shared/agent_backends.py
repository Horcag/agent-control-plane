from __future__ import annotations

import os

AGY_BACKEND = "agy"
CODEX_BACKEND = "codex"
CLAUDE_BACKEND = "claude"
CODEX_SPARK_BACKEND = "codex-spark"
CLAUDE_CODE_BACKEND = "claude-code"
SUPPORTED_BACKENDS = (AGY_BACKEND, CODEX_BACKEND, CLAUDE_BACKEND)
LEGACY_BACKEND_ALIASES = {
    CODEX_SPARK_BACKEND: CODEX_BACKEND,
    CLAUDE_CODE_BACKEND: CLAUDE_BACKEND,
}

DISABLED_BACKENDS_ENV_VAR = "ACP_DISABLED_BACKENDS"


def normalize_backend(value: str) -> str:
    return LEGACY_BACKEND_ALIASES.get(value, value)


def globally_disabled_backends() -> frozenset[str]:
    """Return backends disabled for every ACP config in this process environment."""
    raw = os.environ.get(DISABLED_BACKENDS_ENV_VAR, "")
    return frozenset(normalize_backend(value.strip()) for value in raw.split(",") if value.strip())
