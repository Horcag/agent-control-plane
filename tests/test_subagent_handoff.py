from __future__ import annotations

import json
from pathlib import Path

from agent_control_plane.features.result_handoff import scan_codex_subagent_completions


def test_scanner_imports_completed_subagent_after_parent_turn_is_gone(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rollout = sessions / "2026" / "07" / "15" / "rollout-subagent.jsonl"
    _write_rollout(
        rollout,
        thread_id="subagent-1",
        parent_thread_id="parent-aborted",
        cwd=workspace,
        terminal=True,
        result="review verdict\n" + "x" * 100,
    )

    completions = scan_codex_subagent_completions(
        sessions,
        workspace_roots={"app": workspace},
        max_files=20,
        tail_bytes=4096,
    )

    assert len(completions) == 1
    completion = completions[0]
    assert completion.thread_id == "subagent-1"
    assert completion.parent_thread_id == "parent-aborted"
    assert completion.agent_path == "/root/reviewer"
    assert completion.route == "app"
    assert completion.result.startswith("review verdict")
    assert completion.rollout_path == rollout

    assert (
        scan_codex_subagent_completions(
            sessions,
            workspace_roots={"app": workspace},
            parent_thread_id="another-parent",
            max_files=20,
        )
        == []
    )


def test_scanner_filters_running_and_out_of_scope_subagents(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _write_rollout(
        sessions / "running.jsonl",
        thread_id="running",
        parent_thread_id="parent",
        cwd=workspace,
        terminal=False,
        result="not done",
    )
    _write_rollout(
        sessions / "outside.jsonl",
        thread_id="outside",
        parent_thread_id="parent",
        cwd=outside,
        terminal=True,
        result="done elsewhere",
    )

    assert (
        scan_codex_subagent_completions(
            sessions,
            workspace_roots={"app": workspace},
            max_files=20,
        )
        == []
    )


def test_scanner_reads_parent_and_agent_identity_from_nested_spawn_metadata(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_rollout(
        sessions / "nested.jsonl",
        thread_id="nested-agent",
        parent_thread_id="nested-parent",
        cwd=workspace,
        terminal=True,
        result="nested result",
        nested_identity_only=True,
    )

    completions = scan_codex_subagent_completions(
        sessions,
        workspace_roots={"app": workspace},
        parent_thread_id="nested-parent",
        max_files=20,
    )

    assert len(completions) == 1
    assert completions[0].parent_thread_id == "nested-parent"
    assert completions[0].agent_path == "/root/reviewer"
    assert completions[0].agent_nickname == "Reviewer"


def _write_rollout(
    path: Path,
    *,
    thread_id: str,
    parent_thread_id: str,
    cwd: Path,
    terminal: bool,
    result: str,
    nested_identity_only: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": "2026-07-15T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "cwd": str(cwd),
                "thread_source": "subagent",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_thread_id,
                            "agent_path": "/root/reviewer",
                            "agent_nickname": "Reviewer",
                        }
                    }
                },
            },
        },
        {
            "timestamp": "2026-07-15T10:01:00Z",
            "type": "event_msg",
            "payload": {"type": "token_count"},
        },
    ]
    if not nested_identity_only:
        metadata = events[0]["payload"]
        assert isinstance(metadata, dict)
        metadata.update(
            {
                "parent_thread_id": parent_thread_id,
                "agent_path": "/root/reviewer",
                "agent_nickname": "Reviewer",
            }
        )
    if terminal:
        events.append(
            {
                "timestamp": "2026-07-15T10:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": result,
                    "completed_at": 1,
                },
            }
        )
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
