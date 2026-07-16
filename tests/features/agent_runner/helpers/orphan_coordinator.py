from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_control_plane.entities.job import JobStore
from agent_control_plane.features.agent_runner import (
    WorkerLease,
    capture_process_identity,
)


def main() -> int:
    database_path = Path(sys.argv[1])
    run_dir = Path(sys.argv[2])
    job_id = sys.argv[3]
    worker_instance_id = sys.argv[4]
    ready_path = Path(sys.argv[5])
    child = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = capture_process_identity(child.pid)
    coordinator_identity = capture_process_identity(os.getpid())
    if identity is None or coordinator_identity is None:
        child.terminate()
        child.wait(timeout=5)
        raise RuntimeError("Could not capture coordinator or child process identity")

    store = JobStore(database_path)
    store.assign_worker(job_id, worker_instance_id, worker_pid=os.getpid())
    with WorkerLease(run_dir, worker_instance_id):
        if not store.update_for_worker(
            job_id,
            worker_instance_id,
            status="running",
            worker_pid=os.getpid(),
            runner_pid=child.pid,
            runner_process_identity=identity.to_json(),
        ):
            raise RuntimeError(f"Lost worker ownership: {job_id}")
        temporary_ready = ready_path.with_suffix(".tmp")
        temporary_ready.write_text(
            json.dumps(
                {
                    "coordinator_identity": coordinator_identity.as_dict(),
                    "identity": identity.as_dict(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary_ready.replace(ready_path)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
