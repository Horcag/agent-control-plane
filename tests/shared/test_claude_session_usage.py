import json
import sys
from pathlib import Path

from agent_control_plane.shared.claude_session_usage import (
    claude_project_dir_name,
    claude_session_path,
    claude_usage_from_mapping,
    latest_claude_session_usage,
)


def _assistant_line(
    message_id: str,
    *,
    usage: dict[str, int],
    timestamp: str = "2026-07-20T10:00:00.000Z",
    sidechain: bool = False,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": f"uuid-{message_id}",
            "timestamp": timestamp,
            "isSidechain": sidechain,
            "message": {"id": message_id, "role": "assistant", "usage": usage},
        }
    )


def test_project_dir_name_replaces_non_alphanumeric_characters(tmp_path) -> None:
    workspace = tmp_path / "My Repo.v2"
    name = claude_project_dir_name(workspace)
    assert name.endswith("My-Repo-v2")
    assert not any(char in name for char in (" ", ".", "\\", "/", ":"))
    if sys.platform == "win32":
        assert claude_project_dir_name(Path("C:/Users/nikit")) == "C--Users-nikit"


def test_session_path_is_keyed_by_sanitized_workspace_and_session_id(tmp_path) -> None:
    workspace = tmp_path / "repo"
    path = claude_session_path(tmp_path, workspace, "abc-123")
    assert path == tmp_path / claude_project_dir_name(workspace) / "abc-123.jsonl"


def test_usage_mapping_treats_cached_tokens_as_subset_of_input() -> None:
    usage = claude_usage_from_mapping(
        {
            "input_tokens": 100,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
            "output_tokens": 40,
        }
    )
    assert usage is not None
    assert usage.input_tokens == 1050
    assert usage.cached_input_tokens == 900
    assert usage.uncached_input_tokens == 150
    assert usage.output_tokens == 40
    assert usage.reasoning_output_tokens == 0
    assert usage.comparable_tokens == 190


def test_latest_session_usage_sums_per_request_usage(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
                _assistant_line(
                    "msg_1",
                    usage={
                        "input_tokens": 10,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 5,
                        "output_tokens": 20,
                    },
                ),
                _assistant_line(
                    "msg_2",
                    usage={
                        "input_tokens": 4,
                        "cache_read_input_tokens": 130,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 7,
                    },
                    timestamp="2026-07-20T10:05:00.000Z",
                ),
            ]
        ),
        encoding="utf-8",
    )
    snapshot = latest_claude_session_usage(transcript)
    assert snapshot is not None
    assert snapshot.usage.input_tokens == 115 + 134
    assert snapshot.usage.cached_input_tokens == 230
    assert snapshot.usage.output_tokens == 27
    assert snapshot.recorded_at == "2026-07-20T10:05:00.000Z"


def test_latest_session_usage_dedupes_repeated_message_ids(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 5,
    }
    transcript.write_text(
        "\n".join(
            [
                _assistant_line("msg_1", usage=usage),
                _assistant_line("msg_1", usage=usage),
            ]
        ),
        encoding="utf-8",
    )
    snapshot = latest_claude_session_usage(transcript)
    assert snapshot is not None
    assert snapshot.usage.input_tokens == 10
    assert snapshot.usage.output_tokens == 5


def test_latest_session_usage_skips_sidechain_records(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 5,
    }
    transcript.write_text(
        "\n".join(
            [
                _assistant_line("msg_main", usage=usage),
                _assistant_line("msg_side", usage=usage, sidechain=True),
            ]
        ),
        encoding="utf-8",
    )
    snapshot = latest_claude_session_usage(transcript)
    assert snapshot is not None
    assert snapshot.usage.input_tokens == 10


def test_latest_session_usage_returns_none_for_missing_or_empty_file(tmp_path) -> None:
    assert latest_claude_session_usage(tmp_path / "missing.jsonl") is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("not json\n", encoding="utf-8")
    assert latest_claude_session_usage(empty) is None
