import json
from pathlib import Path

import pytest

from agent_control_plane.features.agent_runner.lib.claude_model_catalog import (
    build_claude_model_catalog,
)
from agent_control_plane.features.agent_runner.lib.claude_telemetry import (
    claude_turn_completed,
    extract_claude_final_message,
    parse_claude_jsonl,
    render_claude_json_line,
    scan_claude_tool_constraints,
)
from agent_control_plane.shared.claude_session_usage import claude_session_path
from agent_control_plane.shared.config import (
    ClaudeModelCatalogConfig,
    CodexModelMetadataConfig,
    CodexTokenRateConfig,
)

_USAGE = {
    "input_tokens": 100,
    "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 50,
    "output_tokens": 40,
}


def _events_file(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "attempt-001.events.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _init_event(session_id: str = "sess-1") -> dict:
    return {"type": "system", "subtype": "init", "session_id": session_id, "model": "m"}


def _assistant_event(
    blocks: list[dict], *, message_id: str = "msg_1", usage: dict | None = None
) -> dict:
    message: dict = {"id": message_id, "role": "assistant", "content": blocks}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "message": message, "session_id": "sess-1"}


def _result_event(**overrides) -> dict:
    event = {
        "type": "result",
        "subtype": "success",
        "total_cost_usd": 1.25,
        "usage": _USAGE,
        "num_turns": 3,
        "session_id": "sess-1",
    }
    event.update(overrides)
    return event


def test_result_event_provides_usage_cost_and_session_identity(tmp_path) -> None:
    path = _events_file(tmp_path, [_init_event(), _result_event()])
    metrics = parse_claude_jsonl(path, model="claude-opus-4-8", duration_sec=2.0)
    assert metrics.turn_completed is True
    assert metrics.usage_available is True
    assert metrics.thread_id == "sess-1"
    assert metrics.input_tokens == 1050
    assert metrics.cached_input_tokens == 900
    assert metrics.output_tokens == 40
    assert metrics.reasoning_output_tokens == 0
    assert metrics.estimated_api_usd == pytest.approx(1.25)
    assert metrics.rate_card_version == "claude-code-cli"
    assert metrics.cache_creation_input_tokens == 50


def test_result_event_cache_creation_is_additive_detail_not_folded_out_of_input(
    tmp_path,
) -> None:
    path = _events_file(
        tmp_path,
        [
            _init_event(),
            _result_event(
                usage={
                    "input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 50,
                    "output_tokens": 5,
                }
            ),
        ],
    )
    metrics = parse_claude_jsonl(path, model="claude-opus-4-8", duration_sec=1.0)
    assert metrics.cache_creation_input_tokens == 50
    assert metrics.input_tokens == 60


def test_success_subtype_with_is_error_true_does_not_complete_the_turn(tmp_path) -> None:
    path = _events_file(tmp_path, [_init_event(), _result_event(is_error=True)])
    metrics = parse_claude_jsonl(path, model="claude-opus-4-8", duration_sec=1.0)
    assert metrics.turn_completed is False
    assert metrics.error_events == 1


def test_assistant_usage_sum_is_the_fallback_when_result_missing(tmp_path) -> None:
    path = _events_file(
        tmp_path,
        [
            _init_event(),
            _assistant_event([], message_id="msg_1", usage=_USAGE),
            _assistant_event([], message_id="msg_1", usage=_USAGE),
            _assistant_event(
                [],
                message_id="msg_2",
                usage={
                    "input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 5,
                },
            ),
        ],
    )
    metrics = parse_claude_jsonl(path, model="claude-opus-4-8", duration_sec=1.0)
    assert metrics.turn_completed is False
    assert metrics.usage_available is True
    assert metrics.input_tokens == 1050 + 10
    assert metrics.output_tokens == 45
    assert metrics.cache_creation_input_tokens == 50


def test_tool_use_blocks_are_counted_with_mcp_naming_and_failures(tmp_path) -> None:
    path = _events_file(
        tmp_path,
        [
            _init_event(),
            _assistant_event(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                    {"type": "tool_use", "id": "toolu_2", "name": "mcp__idea__search", "input": {}},
                    {"type": "tool_use", "id": "toolu_2", "name": "mcp__idea__search", "input": {}},
                ]
            ),
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True},
                        {"type": "tool_result", "tool_use_id": "toolu_2"},
                    ]
                },
            },
            _result_event(subtype="error_max_turns", total_cost_usd=None, usage=None),
        ],
    )
    metrics = parse_claude_jsonl(path, model="claude-opus-4-8", duration_sec=1.0)
    assert metrics.tool_calls == 2
    assert dict(metrics.tool_counts) == {"Bash": 1, "mcp:idea/search": 1}
    assert metrics.failed_tool_calls == 1
    assert metrics.error_events == 1
    assert metrics.turn_completed is False


def test_rate_card_reprice_is_used_when_cli_cost_is_absent(tmp_path) -> None:
    catalog = build_claude_model_catalog(
        ClaudeModelCatalogConfig(
            models=(
                CodexModelMetadataConfig(
                    model="claude-opus-4-8",
                    premium=False,
                    quota_domain=None,
                    capacity_units=(),
                    credit_rate=None,
                    api_usd_rate=CodexTokenRateConfig(input=5.0, cached_input=0.5, output=25.0),
                    rate_card_version="2026-07",
                    rate_card_source="operator",
                ),
            ),
        )
    )
    path = _events_file(tmp_path, [_result_event(total_cost_usd=None)])
    metrics = parse_claude_jsonl(
        path,
        model="claude-opus-4-8",
        duration_sec=1.0,
        catalog=catalog,
    )
    expected = (150 * 5.0 + 900 * 0.5 + 40 * 25.0) / 1_000_000
    assert metrics.estimated_api_usd == pytest.approx(expected)
    assert metrics.rate_card_version == "2026-07"


def test_session_transcript_recovery_when_stream_has_no_usage(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sessions_root = tmp_path / "projects"
    transcript = claude_session_path(sessions_root, workspace, "sess-1")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "timestamp": "2026-07-20T10:00:00.000Z",
                "message": {"id": "msg_1", "usage": _USAGE},
            }
        ),
        encoding="utf-8",
    )
    path = _events_file(tmp_path, [_init_event()])
    metrics = parse_claude_jsonl(
        path,
        model="claude-opus-4-8",
        duration_sec=1.0,
        sessions_root=sessions_root,
        workspace_path=workspace,
    )
    assert metrics.usage_available is True
    assert metrics.input_tokens == 1050
    assert metrics.cache_creation_input_tokens == 50


def test_turn_completed_requires_a_result_event(tmp_path) -> None:
    incomplete = _events_file(tmp_path, [_init_event(), _assistant_event([])])
    assert claude_turn_completed(incomplete) is False
    complete = _events_file(tmp_path, [_init_event(), _result_event()])
    assert claude_turn_completed(complete) is True


def test_tool_constraint_scan_enforces_budget_incrementally(tmp_path) -> None:
    path = _events_file(
        tmp_path,
        [
            _assistant_event(
                [{"type": "tool_use", "id": f"toolu_{index}", "name": "Read", "input": {}}]
            )
            for index in range(3)
        ],
    )
    violation, scan_size, count = scan_claude_tool_constraints(path, 0, 0, tool_call_budget=2)
    assert violation is not None and "budget of 2 exceeded" in violation
    assert count == 3
    violation, _, count = scan_claude_tool_constraints(path, scan_size, count, tool_call_budget=0)
    assert violation is None


def test_render_produces_watchdog_compatible_markers() -> None:
    exec_line = render_claude_json_line(
        json.dumps(
            _assistant_event(
                [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}]
            )
        )
    )
    assert exec_line == "exec\npytest\n[requested]\n"
    search_line = render_claude_json_line(
        json.dumps(
            _assistant_event(
                [{"type": "tool_use", "id": "t2", "name": "WebSearch", "input": {"query": "q"}}]
            )
        )
    )
    assert search_line.startswith("web search: q")
    assert render_claude_json_line(json.dumps({"type": "stream_event"})) == ""


def test_extract_final_message_prefers_the_result_event_text(tmp_path) -> None:
    path = _events_file(
        tmp_path,
        [
            _init_event(),
            _assistant_event([{"type": "text", "text": "thinking out loud"}]),
            _result_event(result="Status: completed\n\nFinal answer."),
        ],
    )
    assert extract_claude_final_message(path) == "Status: completed\n\nFinal answer."


def test_extract_final_message_falls_back_to_last_assistant_text(tmp_path) -> None:
    path = _events_file(
        tmp_path,
        [
            _init_event(),
            _assistant_event([{"type": "text", "text": "first"}]),
            _assistant_event([{"type": "text", "text": "Status: completed final"}]),
            # result event without a textual `result` field must not win
            _result_event(),
        ],
    )
    assert extract_claude_final_message(path) == "Status: completed final"


def test_extract_final_message_returns_none_without_text(tmp_path) -> None:
    path = _events_file(tmp_path, [_init_event()])
    assert extract_claude_final_message(path) is None
    assert extract_claude_final_message(tmp_path / "missing.jsonl") is None
