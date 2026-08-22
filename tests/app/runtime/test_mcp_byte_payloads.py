from __future__ import annotations

import hashlib
import json

import pytest

from agent_control_plane.app.runtime import mcp_byte_windows
from agent_control_plane.app.runtime.mcp_byte_windows import (
    MAX_PREVIEW_BYTES,
    file_preview,
    missing_file_preview,
    serialized_bytes,
    tail_preview,
)
from agent_control_plane.app.runtime.mcp_payloads import (
    MAX_RESPONSE_BYTES,
    compact_checkpoint,
    compact_review_item,
    compact_summary,
)


def test_file_preview_is_byte_bounded_hashed_and_unicode_safe(tmp_path) -> None:
    path = tmp_path / "result.md"
    content = ("a" * (MAX_PREVIEW_BYTES - 2)) + "😀" + ("z" * (5 * 1024 * 1024))
    path.write_text(content, encoding="utf-8")

    payload = file_preview(path, stable_id="job-large", limit=MAX_PREVIEW_BYTES)
    resumed = file_preview(
        path,
        stable_id="job-large",
        offset=payload["next_offset"],
        limit=8,
    )

    assert payload["returned_bytes"] <= MAX_PREVIEW_BYTES
    assert payload["available"] is True
    assert len(payload["content"].encode("utf-8")) == payload["returned_bytes"]
    assert not payload["content"].endswith("�")
    assert payload["total_bytes"] == len(content.encode("utf-8"))
    assert payload["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert payload["truncated"] is True
    assert resumed["content"].startswith("😀")


def test_file_preview_aligns_offset_without_splitting_code_points(tmp_path) -> None:
    path = tmp_path / "unicode.txt"
    path.write_text("😀tail", encoding="utf-8")

    payload = file_preview(path, stable_id="unicode", offset=1, limit=4)

    assert payload["offset"] == 4
    assert payload["limit"] == 4
    assert payload["content"] == "tail"
    assert payload["truncated"] is False


@pytest.mark.parametrize("offset", [True, "0", -1])
def test_file_preview_rejects_invalid_offsets(tmp_path, offset) -> None:
    path = tmp_path / "result.md"
    path.write_text("result", encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match="offset"):
        file_preview(path, stable_id="job", offset=offset)


@pytest.mark.parametrize("limit", [True, "16", 3, MAX_PREVIEW_BYTES + 1])
def test_file_preview_rejects_invalid_limits(tmp_path, limit) -> None:
    path = tmp_path / "result.md"
    path.write_text("result", encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match="limit"):
        file_preview(path, stable_id="job", limit=limit)


def test_file_preview_allows_offset_beyond_eof(tmp_path) -> None:
    path = tmp_path / "result.md"
    path.write_text("result", encoding="utf-8")

    payload = file_preview(path, stable_id="job", offset=10_000)

    assert payload["content"] == ""
    assert payload["offset"] == payload["total_bytes"]


def test_missing_file_preview_separates_diagnostic_from_durable_content(tmp_path) -> None:
    path = tmp_path / "missing.md"

    payload = missing_file_preview(
        path,
        stable_id="job-missing",
        message=f"Result file does not exist yet: {path}",
    )

    assert payload["available"] is False
    assert payload["content"] == ""
    assert payload["returned_bytes"] == payload["total_bytes"] == 0
    assert payload["sha256"] is None
    assert str(path) in payload["message"]


def test_tail_preview_bounds_a_multi_megabyte_single_line(tmp_path, monkeypatch) -> None:
    path = tmp_path / "attempt.log"
    path.write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
    monkeypatch.setattr(
        mcp_byte_windows,
        "_sha256_file",
        lambda _path: pytest.fail("tail preview must not hash the full file"),
    )

    payload = tail_preview(path, stable_id="job-log", lines=80)

    assert payload["tail_start"] == 0
    assert payload["returned_bytes"] == MAX_PREVIEW_BYTES
    assert payload["next_cursor"] == MAX_PREVIEW_BYTES
    assert payload["sha256"] is None
    assert payload["window_sha256"] == hashlib.sha256(b"x" * MAX_PREVIEW_BYTES).hexdigest()
    assert payload["truncated"] is True
    assert serialized_bytes(payload) < MAX_RESPONSE_BYTES


def test_compact_review_item_omits_commands_and_bounds_hostile_check_bundle() -> None:
    huge_command = "python " + ("x" * (1024 * 1024))
    checks = [
        {
            "command": huge_command,
            "cwd": ".",
            "outcome": "passed",
            "exit_code": 0,
            "summary": "ok",
            "raw_output": "secret raw output",
        }
        for _ in range(50_000)
    ]
    item = {
        "item_id": "agent_job:large",
        "result_path": "/tasks/large/result.md",
        "result_text": "Status: completed\n" + ("detail" * 100_000),
        "result_sha256": "a" * 64,
        "verification_bundle": {
            "review_ready": True,
            "result": {"status": "completed", "format_valid": True},
            "result_contract": {
                "expected_status": "completed",
                "reported_status": "completed",
                "matches": True,
            },
            "worker_quality": {"status": "passed", "required": True},
            "quality_contract": {"policy": "controller", "sha256": "c" * 64},
            "worker_verification": {
                "state": "valid",
                "schema_version": 1,
                "sha256": "b" * 64,
                "payload": {"checks": checks},
            },
        },
        "verification_json": {"checks": checks},
    }

    payload = compact_review_item(item)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert serialized_bytes(payload) < MAX_RESPONSE_BYTES
    assert len(payload["result"]["content"].encode("utf-8")) <= MAX_PREVIEW_BYTES
    assert payload["verification_summary"]["worker_verification"]["checks"]["total"] == 50_000
    assert payload["verification_summary"]["worker_verification"]["checks"]["truncated"]
    assert payload["verification_summary"]["result_contract"]["matches"] is True
    assert payload["verification_summary"]["worker_quality"]["status"] == "passed"
    assert payload["verification_summary"]["quality_contract"]["policy"] == "controller"
    assert "command" not in encoded
    assert "raw_output" not in encoded
    assert "verification_bundle" not in payload
    assert "verification_json" not in payload
    assert "result_text" not in payload


def test_compact_checkpoint_uses_same_inbox_projection_and_bounds_slot_dirty_status() -> None:
    payload = compact_checkpoint(
        {
            "slot": {
                "name": "acp-2",
                "path": "/slots/acp-2",
                "dirty": "M " + ("x" * (2 * 1024 * 1024)),
                "problems": [],
            },
            "inbox": {
                "item_id": "agent_job:checkpoint",
                "result_text": "Status: completed\n",
                "verification_bundle": {"review_ready": True},
            },
        }
    )

    assert payload["inbox"]["id"] == "agent_job:checkpoint"
    assert payload["slot"]["dirty"]["returned_bytes"] <= MAX_PREVIEW_BYTES
    assert serialized_bytes(payload) < MAX_RESPONSE_BYTES


def test_compact_summary_bounds_variable_metadata_and_keeps_preview_envelopes() -> None:
    preview = {"content": "x" * MAX_PREVIEW_BYTES, "returned_bytes": MAX_PREVIEW_BYTES}
    payload = compact_summary(
        {
            "job_id": "job-large",
            "last_error": "e" * (2 * 1024 * 1024),
            "forbidden_changes": [f"M file-{index}" for index in range(50_000)],
            "latest_attempt_metrics": {f"tool-{index}": "m" * 1024 for index in range(50_000)},
            "dirty_status": preview,
            "log_tail": preview,
        }
    )

    assert payload["job_id"] == "job-large"
    assert payload["dirty_status"] is preview
    assert payload["log_tail"] is preview
    assert payload["forbidden_changes_total"] == 50_000
    assert payload["forbidden_changes_truncated"] is True
    assert serialized_bytes(payload) < MAX_RESPONSE_BYTES
