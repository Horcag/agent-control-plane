from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_globally_disabled_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep backend-policy tests independent from the operator's machine guard."""
    monkeypatch.delenv("ACP_DISABLED_BACKENDS", raising=False)


@pytest.fixture(autouse=True)
def isolate_port_assignments_path(tmp_path: Path) -> Generator[Path, None, None]:
    assignments_path = tmp_path / "port-assignments.json"
    with patch(
        "agent_control_plane.shared.config.port_assignments_path",
        return_value=assignments_path,
    ):
        yield assignments_path


@pytest.fixture(autouse=True)
def isolate_known_configs_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    """Keep throwaway test configs out of the operator's real known-config index.

    Set through the environment rather than by patching, because tests that spawn a
    real server subprocess register their config from another process, which no
    in-process patch can reach. Config discovery reads this index on every resolution,
    so a leaked temp path does not just bloat a file - it can win a real lookup.
    """
    index_path = tmp_path / "known-configs.json"
    monkeypatch.setenv("ACP_KNOWN_CONFIGS_PATH", str(index_path))
    yield index_path


@pytest.fixture(autouse=True)
def cleanup_test_subprocesses() -> Generator[None, None, None]:
    tracked_processes: list[subprocess.Popen[Any]] = []
    orig_init = subprocess.Popen.__init__

    def tracking_popen_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        tracked_processes.append(self)

    with patch.object(subprocess.Popen, "__init__", tracking_popen_init):
        try:
            yield
        finally:
            for proc in tracked_processes:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        try:
                            proc.kill()
                            proc.wait(timeout=1)
                        except Exception:  # noqa: BLE001
                            pass
