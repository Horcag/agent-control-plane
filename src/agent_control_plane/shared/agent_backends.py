from __future__ import annotations

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


def normalize_backend(value: str) -> str:
    return LEGACY_BACKEND_ALIASES.get(value, value)
