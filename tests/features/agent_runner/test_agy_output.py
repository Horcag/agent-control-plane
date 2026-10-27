from __future__ import annotations

import io
import json
import os
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
        if os.name != "nt":  # Windows mode bits do not describe filesystem ACLs.
            assert artifact.stat().st_mode & 0o777 == 0o600
    events = read_agy_activity_events(path, job_id="job-1")
    assert len([event for event in events["events"] if event["kind"] == "tool"]) == 2


@pytest.mark.parametrize(
    "bad",
    ["malformed\n", '{"event":"result"}', "x" * (LINE_LIMIT + 1) + "\n"],
    ids=["malformed", "truncated", "oversized"],
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
    assert output.failure_message is None


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


@pytest.mark.parametrize(
    ("status", "response", "message"),
    [
        # Sanitized native print-timeout probe on 2026-10-27: exit zero, SUCCESS, empty response.
        ("SUCCESS", "", "AGY CLI returned SUCCESS with an empty final response"),
        ("SUCCESS", " \r\n", "AGY CLI returned SUCCESS with an empty final response"),
        ("PRIVATE_STATUS", "PRIVATE_RESPONSE", "AGY CLI returned a non-success result status"),
    ],
)
def test_structured_failure_diagnostic_requires_clean_eof_and_excludes_payloads(
    tmp_path: Path, status: str, response: str, message: str
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "result", "result": {"status": status, "response": response}})
    assert output.failure_message is None
    assert output.finish(clean=True) is None
    assert output.failure_message == message
    assert output.snapshot["stream_state"] == "error"
    assert "PRIVATE" not in log.getvalue()
    for artifact in tmp_path.iterdir():
        assert "PRIVATE" not in artifact.read_text()


@pytest.mark.parametrize(
    "suffix",
    [
        "malformed\n",
        '{"event":"result"',
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": ""}}) + "\n",
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "final"}})
        + "\n",
    ],
    ids=["malformed", "truncated", "duplicate", "conflicting"],
)
def test_invalid_stream_cannot_supply_structured_failure_diagnostic(
    tmp_path: Path, suffix: str
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "result", "result": {"status": "SUCCESS", "response": ""}})
    output.feed(suffix, log)
    assert output.finish(clean=True) is None
    assert output.failure_message is None


@pytest.mark.parametrize(
    "result",
    [{"status": "", "response": ""}, {"status": False, "response": ""}, {"status": "FAILURE"}],
)
def test_malformed_result_cannot_supply_structured_failure_diagnostic(
    tmp_path: Path, result: dict
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    emit(output, io.StringIO(), {"event": "result", "result": result})
    assert output.finish(clean=True) is None
    assert output.failure_message is None
    assert output.progress_sequence == 0


def test_unclean_eof_cannot_supply_structured_failure_diagnostic(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    emit(
        output,
        io.StringIO(),
        {"event": "result", "result": {"status": "SUCCESS", "response": ""}},
    )
    assert output.finish(clean=False) is None
    assert output.failure_message is None


@pytest.mark.parametrize("step_type", ["agent_response", "tool"])
@pytest.mark.parametrize("source", ["content", "usage"])
def test_active_content_and_usage_progress_are_coalesced_without_private_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step_type: str, source: str
) -> None:
    from agent_control_plane.features.agent_runner.lib import agy_output

    clock = [0.0]
    monkeypatch.setattr(agy_output.time, "monotonic", lambda: clock[0])
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()

    def update(text: str, tokens: int) -> None:
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "step_type": step_type,
                    "step_index": 1,
                    "state": "ACTIVE",
                    "text_delta": text,
                    "tool_info": {"output": text},
                    "usage": {"thinking_tokens": tokens},
                },
            },
        )

    update("PRIVATE first", 1)
    initial_seq = output.snapshot["seq"]
    started = output.snapshot["action_started_at"]
    for index in range(1, 101):
        clock[0] = index / 10
        update(
            f"PRIVATE delta {index}" if source == "content" else "PRIVATE first",
            index + 1 if source == "usage" else 1,
        )
    assert output.progress_sequence == 101
    assert output.snapshot["seq"] <= initial_seq + 5
    assert output.snapshot["action_started_at"] == started
    assert output.snapshot["usage"]["thinking_tokens"] == (101 if source == "usage" else 1)
    assert len(output.progress_filters[(step_type, 1)]) == 8192
    assert output.snapshot["tool_calls" if step_type == "tool" else "model_responses"] == 1
    for artifact in tmp_path.iterdir():
        assert "PRIVATE" not in artifact.read_text()
    assert "PRIVATE" not in log.getvalue()


def test_replays_usage_regressions_terminal_backlog_and_input_are_not_progress(
    tmp_path: Path,
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    for text, tokens in (("PRIVATE A", 2), ("PRIVATE B", 3)):
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 10,
                    "state": "ACTIVE",
                    "text_delta": text,
                    "usage": {"output_tokens": tokens},
                },
            },
        )
    previous = output.progress_sequence
    for step in (
        {
            "step_type": "agent_response",
            "step_index": 10,
            "state": "ACTIVE",
            "text_delta": "PRIVATE A",
            "usage": {"output_tokens": 2},
        },
        {
            "step_type": "agent_response",
            "step_index": 10,
            "state": "ACTIVE",
            "text_delta": "PRIVATE B",
            "usage": {"output_tokens": 3},
        },
        {"step_type": "tool", "step_index": 9, "state": "DONE", "usage": {"output_tokens": 100}},
        {
            "step_type": "user_input",
            "step_index": 100,
            "state": "DONE",
            "text_delta": "PRIVATE new input",
        },
    ):
        emit(output, log, {"event": "step_update", "step_update": step})
    emit(output, log, {"event": "heartbeat"})
    assert output.progress_sequence == previous
    assert output.snapshot["usage"]["output_tokens"] == 3
    assert output.snapshot["current_step_index"] == 10
    assert output.snapshot["current_state"] == "active"


def test_quiet_active_operation_is_unknown_until_genuine_progress(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "init", "conversation_id": str(uuid4())})
    step = {"step_type": "tool", "step_index": 1, "state": "ACTIVE", "tool_name": "run_command"}
    emit(output, log, {"event": "step_update", "step_update": step})
    previous = output.progress_sequence
    progress_at = output.snapshot["last_progress_at"]
    started = output.snapshot["action_started_at"]
    output.note_quiet(log)
    quiet_seq = output.snapshot["seq"]
    output.note_quiet(log)
    emit(output, log, {"event": "step_update", "step_update": step})
    assert output.quiet and output.has_active_operation
    assert output.progress_sequence == previous
    assert output.snapshot["last_progress_at"] == progress_at
    assert output.snapshot["seq"] == quiet_seq
    assert output.snapshot["action_started_at"] == started
    live = read_agy_activity(tmp_path / "attempt.log", job_id="job")
    assert live["available"] and "No recent progress observed" in live["current_action"]
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                **{key: value for key, value in step.items() if key != "tool_name"},
                "tool_info": {"output": "PRIVATE progress"},
            },
        },
    )
    assert not output.quiet
    assert output.progress_sequence == previous + 1
    assert output.snapshot["current_action"] == "Tool activity"
    assert output.snapshot["action_started_at"] == started

    assert output.snapshot["current_tool_name"] == "run_command"
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "step_index": 2,
                "state": "ACTIVE",
            },
        },
    )
    assert output.snapshot["current_tool_name"] == "tool"


@pytest.mark.parametrize("foreign", ["job", "attempt", "conversation"])
def test_foreign_identity_cannot_refresh_progress(tmp_path: Path, foreign: str) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    conversation = str(uuid4())
    emit(output, log, {"event": "init", "conversation_id": conversation})
    event = {
        "event": "step_update",
        "step_update": {"step_type": "tool", "step_index": 1, "state": "ACTIVE"},
    }
    if foreign == "conversation":
        event["step_update"]["conversation_id"] = str(uuid4())
    elif foreign == "job":
        event["job_id"] = "another-job"
    else:
        event["attempt_ref"] = "another-attempt"
    emit(output, log, event)
    assert output.progress_sequence == 0
    assert not output.has_active_operation
    assert output.invalid


def test_content_filter_tracks_more_than_64_updates_without_crediting_replay(
    tmp_path: Path,
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    step = {"step_type": "agent_response", "step_index": 1, "state": "ACTIVE"}
    for index in range(65):
        emit(
            output,
            log,
            {"event": "step_update", "step_update": {**step, "text_delta": f"PRIVATE {index}"}},
        )
    assert output.progress_sequence == 65
    emit(output, log, {"event": "step_update", "step_update": {**step, "text_delta": "PRIVATE 0"}})
    assert output.progress_sequence == 65
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {**step, "text_delta": "PRIVATE 0", "usage": {"thinking_tokens": 1}},
        },
    )
    assert output.progress_sequence == 66
    assert len(output.progress_filters[("agent_response", 1)]) == 8192


def test_content_filter_tracks_thousand_updates_and_rejects_their_replays(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    step = {"step_type": "agent_response", "step_index": 1, "state": "ACTIVE"}
    for index in range(1000):
        emit(
            output,
            log,
            {"event": "step_update", "step_update": {**step, "text_delta": f"PRIVATE {index}"}},
        )
    previous = output.progress_sequence
    assert 990 < previous <= 1000
    assert len(output.progress_filters[("agent_response", 1)]) == 8192
    for index in range(1000):
        emit(
            output,
            log,
            {"event": "step_update", "step_update": {**step, "text_delta": f"PRIVATE {index}"}},
        )
    assert output.progress_sequence == previous


def test_step_eviction_bounds_filters_and_stale_step_cannot_recreate_one(tmp_path: Path) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    for index in range(1001):
        emit(
            output,
            log,
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": index,
                    "state": "ACTIVE",
                    "tool_info": {"output": f"PRIVATE {index}"},
                },
            },
        )
    assert len(output.progress_filters) == 1000
    assert ("tool", 0) not in output.progress_filters
    assert sum(len(value) for value in output.progress_filters.values()) == 8192 * 1000
    previous = output.progress_sequence
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "step_index": 0,
                "state": "ACTIVE",
                "tool_info": {"output": "PRIVATE 0"},
            },
        },
    )
    assert output.progress_sequence == previous
    assert ("tool", 0) not in output.progress_filters


@pytest.mark.parametrize("ending", ["result", "eof"])
def test_initialized_turn_survives_done_gap_and_ends_at_result_or_eof(
    tmp_path: Path, ending: str
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "init", "conversation_id": str(uuid4())})
    emit(
        output,
        log,
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "step_index": 1,
                "state": "DONE",
                "tool_name": "run_command",
            },
        },
    )
    assert output.has_inflight_turn
    assert not output.has_active_operation
    previous = output.progress_sequence
    output.note_quiet(log)
    assert output.quiet
    assert output.snapshot["current_state"] == "done"
    assert output.progress_sequence == previous
    if ending == "result":
        emit(output, log, {"event": "result", "result": {"status": "SUCCESS", "response": "final"}})
    else:
        output.finish(clean=True)
    assert not output.has_inflight_turn


@pytest.mark.parametrize(
    "event",
    [
        {"event": "init"},
        {"event": "init", "conversation_id": "not-a-uuid"},
        {"event": "init", "conversation_id": str(uuid4()), "job_id": "foreign-job"},
        {"event": "init", "conversation_id": str(uuid4()), "attempt_ref": "foreign-attempt"},
    ],
)
def test_missing_malformed_or_foreign_init_cannot_arm_inflight_turn(
    tmp_path: Path, event: dict
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, event)
    output.note_quiet(log)
    assert not output.has_inflight_turn
    assert not output.quiet
    assert output.invalid


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"status": "SUCCESS"},
        {"status": "SUCCESS", "response": 1},
        {"status": False, "response": "final"},
    ],
)
def test_malformed_current_result_ends_inflight_turn_without_crediting_progress(
    tmp_path: Path, result: object
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "init", "conversation_id": str(uuid4())})
    assert output.has_inflight_turn
    emit(output, log, {"event": "result", "result": result})
    assert not output.has_inflight_turn
    assert output.progress_sequence == 0
    assert output.invalid
    assert output.finish(clean=True) is None


@pytest.mark.parametrize("foreign", ["job", "attempt", "conversation"])
def test_foreign_result_cannot_end_current_inflight_turn(tmp_path: Path, foreign: str) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(output, log, {"event": "init", "conversation_id": str(uuid4())})
    event = {"event": "result", "result": {"status": "SUCCESS", "response": "final"}}
    if foreign == "conversation":
        event["result"]["conversation_id"] = str(uuid4())
    elif foreign == "job":
        event["job_id"] = "foreign-job"
    else:
        event["attempt_ref"] = "foreign-attempt"
    emit(output, log, event)
    assert output.has_inflight_turn
    assert output.results == 0
    assert output.progress_sequence == 0
    assert output.invalid


def test_native_history_publishes_only_bounded_counters_and_never_supplies_result(
    tmp_path: Path,
) -> None:
    output = AgyOutput(tmp_path / "attempt.log", "job")
    log = io.StringIO()
    emit(
        output,
        log,
        {
            "event": "init",
            "conversation_id": str(uuid4()),
            "init": {"cwd": str(tmp_path), "model": "PRIVATE"},
        },
    )
    assert output.native_cwd == str(tmp_path)
    output.note_native_history(4, 5, log)
    previous = output.progress_sequence
    for index, count in ((4, 5), (3, 4), (5, 5), (False, 2), (1_000_000_001, 1)):
        output.note_native_history(index, count, log)
    assert output.progress_sequence == previous
    live = read_agy_activity(tmp_path / "attempt.log", job_id="job")
    assert live["available"] and live["current_action"] == "Native history advanced"
    assert (live["native_step_index"], live["native_step_count"]) == (4, 5)
    events = read_agy_activity_events(tmp_path / "attempt.log", job_id="job")["events"]
    assert events[-1]["kind"] == "native_history"
    assert events[-1]["native_step_count"] == 5
    assert "tool_name" not in events[-1] and "state" not in events[-1]
    assert "PRIVATE" not in log.getvalue()
    assert output.finish(clean=True) is None
    output.note_native_history(6, 7, log)
    assert output.progress_sequence == previous
