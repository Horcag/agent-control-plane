from __future__ import annotations

AGY_BACKEND = "agy"
CODEX_BACKEND = "codex"
CODEX_SPARK_BACKEND = "codex-spark"
SUPPORTED_BACKENDS = (AGY_BACKEND, CODEX_BACKEND)
LEGACY_BACKEND_ALIASES = {CODEX_SPARK_BACKEND: CODEX_BACKEND}


def normalize_backend(value: str) -> str:
    return LEGACY_BACKEND_ALIASES.get(value, value)
