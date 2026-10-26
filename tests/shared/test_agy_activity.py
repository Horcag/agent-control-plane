from __future__ import annotations

import io
import json
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.agy_output import AgyOutput
from agent_control_plane.shared.agy_activity import (
    agy_activity_paths,
    read_agy_activity,
    read_agy_activity_events,
)


def test_attempt_and_job_isolation(tmp_path: Path) -> None:
    first = tmp_path / "attempt-001.log"
    second = tmp_path / "attempt-002.log"
    AgyOutput(first, "one")
    other = AgyOutput(second, "two")
    other.feed(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 1,
                    "state": "active",
                    "tool_name": "run_command",
                },
            }
        )
        + "\n",
        io.StringIO(),
    )
    assert read_agy_activity(first, job_id="one")["tool_calls"] == 0
    assert read_agy_activity(second, job_id="two")["tool_calls"] == 1
    assert not read_agy_activity(first, job_id="two")["available"]
    assert read_agy_activity_events(first, job_id="two")["error"]
    snapshot, _ = agy_activity_paths(first)
    snapshot.write_text('{"schema":1,"attempt_ref":"attempt-002","job_id":"one"}')
    assert not read_agy_activity(first, job_id="one")["available"]


def test_reader_handles_missing_corrupt_and_bounded_pagination(tmp_path: Path) -> None:
    path = tmp_path / "attempt.log"
    assert not read_agy_activity(path)["available"]
    output = AgyOutput(path, "job")
    output.finish(clean=True)
    page = read_agy_activity_events(path, job_id="job", limit=1)
    assert page["truncated"] and page["next_seq"] == 1
    assert read_agy_activity_events(path, job_id="job", after_seq=1)["next_seq"] == 2
    _, events = agy_activity_paths(path)
    events.write_text("{broken")
    assert read_agy_activity_events(path, job_id="job")["error"]


def test_reader_rejects_invalid_current_fields(tmp_path: Path) -> None:
    path = tmp_path / "attempt.log"
    output = AgyOutput(path, "job")
    for key, invalid in (
        ("current_tool_name", "private tool argument"),
        ("current_state", "thinking secret"),
        ("current_step_index", True),
        ("current_step_index", 1_000_000_001),
    ):
        payload = {**output.snapshot, key: invalid}
        agy_activity_paths(path)[0].write_text(json.dumps(payload))
        assert not read_agy_activity(path, job_id="job")["available"]
