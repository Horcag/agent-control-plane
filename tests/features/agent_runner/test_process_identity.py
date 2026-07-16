from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

from agent_control_plane.features.agent_runner import (
    ProcessTerminationState,
    capture_process_identity,
    supports_verified_process_termination,
    terminate_verified_process,
)
from agent_control_plane.features.agent_runner.lib.codex_process_monitor import (
    terminate_spawned_process,
)


@pytest.mark.skipif(
    not supports_verified_process_termination(),
    reason="OS has no safe exact-process termination primitive",
)
def test_verified_termination_refuses_reused_pid_identity() -> None:
    executable = getattr(sys, "_base_executable", sys.executable)
    process = subprocess.Popen(  # nosec B603
        [executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = capture_process_identity(process.pid)
    assert identity is not None
    try:
        mismatched = replace(identity, started_key=identity.started_key + "-reused")

        refused = terminate_verified_process(mismatched)

        assert refused.state is ProcessTerminationState.IDENTITY_MISMATCH
        assert capture_process_identity(identity.pid) == identity

        terminated = terminate_verified_process(identity)

        assert terminated.state is ProcessTerminationState.TERMINATED
        process.wait(timeout=5)
        assert capture_process_identity(identity.pid) is None
    finally:
        if capture_process_identity(identity.pid) == identity:
            terminate_verified_process(identity)
        if process.poll() is None:
            terminate_spawned_process(process)
