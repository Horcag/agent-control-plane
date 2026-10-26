from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_control_plane.features.agent_runner.lib.agy_output import LINE_LIMIT, AgyOutput
from agent_control_plane.shared.agy_activity import read_agy_activity, read_agy_activity_events


def emit(capture: AgyOutput, log: io.StringIO, event: dict) -> None:
    capture.feed(json.dumps(event) + "\n", log)


def test_live_activity_excludes_private_content_and_final_usage_is_not_doubled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attempt-001.log"
    output = AgyOutput(path, "job-1")
    log = io.StringIO()
    conversation = str(uuid4())
    emit(
        output, log, {"event": "init", "conversation_id": conversation, "init": {"cwd": "PRIVATE"}}
    )
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "user_input",
                "text_delta": "PRIVATE",
                "state": "done",
                "step_index": 0,
            },
        },
    )
    for state in ("active", "active", "done"):
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation,
                    "step_type": "tool",
                    "step_index": 1,
                    "state": state,
                    "tool_name": "run_command",
                    "tool_info": {
                        "parameters": {"CommandLine": "PRIVATE"},
                        "output": "PRIVATE Status: completed",
                    },
                },
            },
        )
    for state in ("active", "done"):
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 2,
                    "state": state,
                    "text_delta": "PRIVATE",
                    "thinking": "PRIVATE",
                    "usage": {"thinking_tokens": 7},
                },
            },
        )
    live = read_agy_activity(path, job_id="job-1")
    assert live["available"] and live["stream_state"] == "running"
    assert live["tool_calls"] == live["model_responses"] == 1
    assert live["conversation_id"] == conversation
    emit(
        output,
        log,
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "final",
                "usage": {"thinking_tokens": 10, "input_tokens": 20},
            },
        },
    )
    assert output.finish(clean=True) == "final"
    activity = read_agy_activity(path, job_id="job-1")
    assert activity["usage"]["thinking_tokens"] == 10
    assert activity["usage"]["input_tokens"] == 20
    assert "PRIVATE" not in log.getvalue()
    for artifact in tmp_path.iterdir():
        assert "PRIVATE" not in artifact.read_text()
        assert artifact.stat().st_mode & 0o777 == 0o600
    events = read_agy_activity_events(path, job_id="job-1")
    assert len([event for event in events["events"] if event["kind"] == "tool"]) == 2


@pytest.mark.parametrize(
    "bad", ["malformed\n", '{"event":"result"}', "x" * (LINE_LIMIT + 1) + "\n"]
)
def test_split_lines_and_invalid_eof_fail_closed(tmp_path: Path, bad: str) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    final = (
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "final"}}) + "\n"
    )
    for part in (final[:9], final[9:]):
        output.feed(part, log)
    output.feed(bad, log)
    assert output.finish(clean=True) is None


@pytest.mark.parametrize("change", [False, True])
def test_duplicate_or_conflicting_finals_are_rejected(tmp_path: Path, change: bool) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    for response in ("first", "second" if change else "first"):
        emit(
            output,
            io.StringIO(),
            {"event": "result", "result": {"status": "SUCCESS", "response": response}},
        )
    assert output.finish(clean=True) is None


def test_token_keepalive_does_not_invent_progress(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    event = {
        "event": "step_update",
        "step_update": {"step_type": "agent_response", "step_index": 1, "state": "active"},
    }
    emit(output, io.StringIO(), event)
    assert output.snapshot["current_state"] == "active"
    assert output.snapshot["current_step_index"] == 1
    assert output.snapshot["current_tool_name"] is None
    previous = output.snapshot["last_progress_at"]
    emit(output, io.StringIO(), event)
    assert output.snapshot["last_progress_at"] == previous
    output.finish(clean=True)
    assert output.snapshot["last_progress_at"] == previous


def test_event_artifacts_and_log_lines_are_bounded(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    for index in range(1100):
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": index,
                    "state": "active",
                    "tool_name": "run_command",
                },
            },
        )
    output.finish(clean=True)
    assert len(output.events_path.read_text().splitlines()) <= 1000
    assert len(log.getvalue().splitlines()) <= 1000
    assert output.events_path.stat().st_size < 1_048_576


def test_conversation_conflict_cannot_supply_a_final(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "init", "conversation_id": str(uuid4())})
    emit(
        output,
        log,
        {
            "event": "result",
            "result": {"conversation_id": str(uuid4()), "status": "SUCCESS", "response": "fake"},
        },
    )
    assert output.finish(clean=True) is None


def test_tool_payload_with_result_status_cannot_be_a_final(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    emit(
        output,
        io.StringIO(),
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "step_index": 1,
                "state": "done",
                "tool_name": "run_command",
                "tool_info": {"output": {"status": "SUCCESS", "response": "Status: completed"}},
            },
        },
    )
    assert output.finish(clean=True) is None


def test_current_tool_fields_and_unknown_events_do_not_create_progress(tmp_path: Path) -> None:
    path = tmp_path / "attempt.log"
    output = AgyOutput(path, "job")
    log = io.StringIO()
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "step_index": 2,
                "state": "active",
                "tool_name": "run_command",
            },
        },
    )
    snapshot = read_agy_activity(path, job_id="job")
    assert snapshot["current_tool_name"] == "run_command"
    assert snapshot["current_state"] == "active"
    assert snapshot["current_step_index"] == 2
    previous = snapshot["last_progress_at"]
    emit(output, log, {"event": "heartbeat", "private": "SECRET"})
    output.feed("malformed\n", log)
    assert output.snapshot["last_progress_at"] == previous
    output.finish(clean=True)
    assert output.snapshot["current_tool_name"] is None
    assert output.snapshot["current_state"] == "done"
    assert output.snapshot["current_step_index"] is None
    assert output.snapshot["last_progress_at"] == previous


def test_rolling_retention_keeps_current_progress_and_explicit_gap(tmp_path: Path) -> None:
    path = tmp_path / "attempt.log"
    output = AgyOutput(path, "job")
    log = io.StringIO()
    for index in range(1001):
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": index,
                    "state": "done",
                    "usage": {"input_tokens": 1},
                },
            },
        )
    previous = output.snapshot["last_progress_at"]
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "step_index": 100_000,
                "state": "active",
                "tool_name": "read_file",
            },
        },
    )
    snapshot = read_agy_activity(path, job_id="job")
    assert snapshot["current_step_index"] == 100_000
    assert snapshot["current_tool_name"] == "read_file"
    assert snapshot["last_progress_at"] != previous
    assert not snapshot["usage"]["available"]
    assert len(output.steps) <= 1000 and len(output.step_usage) <= 1000
    page = read_agy_activity_events(path, job_id="job", after_seq=0)
    assert page["truncated"] and page["events"][0]["seq"] > 1
    latest = read_agy_activity_events(path, job_id="job", after_seq=snapshot["seq"] - 1)
    assert latest["events"][-1]["tool_name"] == "read_file"
    assert latest["next_seq"] == snapshot["seq"]
    emit(
        output,
        log,
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "final", "usage": {"input_tokens": 1234}},
        },
    )
    assert output.snapshot["usage"]["available"]
    assert output.snapshot["usage"]["input_tokens"] == 1234


def test_native_captured_uppercase_states_publish_live_tool_and_model_activity(
    tmp_path: Path,
) -> None:
    # Sanitized native probe captured on 2026-10-26; enums remain vendor-authored.
    steps = [
        {"step_index": 0, "state": "DONE", "step_type": "user_input", "tool_name": None},
        {"step_index": 1, "state": "DONE", "step_type": "agent_response", "tool_name": None},
        {"step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command"},
        {"step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command"},
        {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "tool_name": None},
        {"step_index": 3, "state": "DONE", "step_type": "agent_response", "tool_name": None},
    ]
    path = tmp_path / "attempt.log"
    output = AgyOutput(path, "native-probe")
    log = io.StringIO()
    for step in steps:
        emit(output, log, {"event": "step_update", "step_update": step})
        if step["step_type"] == "user_input":
            continue
        live = read_agy_activity(path, job_id="native-probe")
        assert live["available"] and live["stream_state"] == "running"
        assert live["current_state"] == str(step["state"]).lower()
        assert live["current_step_index"] == step["step_index"]
        if step["step_type"] == "tool":
            assert live["current_tool_name"] == "run_command"
            assert live["tool_calls"] == 1
    assert live["tool_calls"] == 1
    assert live["model_responses"] == 2
    events = read_agy_activity_events(path, job_id="native-probe")["events"]
    assert [(event["kind"], event.get("state")) for event in events] == [
        ("started", None),
        ("model", "done"),
        ("tool", "active"),
        ("tool", "done"),
        ("model", "active"),
        ("model", "done"),
    ]
    assert "Tool activity (run_command): active" in log.getvalue()
    assert "Model activity: active" in log.getvalue()
    assert output.results == 0
