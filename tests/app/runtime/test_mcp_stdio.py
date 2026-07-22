from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    pytest.skip("mcp is required for MCP stdio integration", allow_module_level=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for MCP stdio integration")
def test_real_stdio_server_reloads_changed_slot_config_without_stale_sqlite_write(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_slot = tmp_path / "slots-old" / "acp-1"
    new_slot = tmp_path / "slots-new" / "acp-1"
    _initialize_repository(repo)
    _create_worktree(repo, old_slot)
    _create_worktree(repo, new_slot)
    config_path = tmp_path / "config" / "workspaces.toml"
    _write_config(config_path, old_slot)

    async def exercise() -> tuple[list[float], dict[object, object]]:
        project_root = Path(__file__).resolve().parents[3]
        python_path = os.pathsep.join(
            value for value in (str(project_root / "src"), os.environ.get("PYTHONPATH")) if value
        )
        server = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_control_plane.app.runtime.mcp_server",
                "--config",
                str(config_path),
            ],
            cwd=project_root,
            env={**os.environ, "PYTHONPATH": python_path},
        )
        timings: list[float] = []
        results: dict[object, object] = {}
        try:
            async with (
                stdio_client(server) as (read, write),
                ClientSession(read, write) as session,
            ):
                with anyio.fail_after(5):
                    await session.initialize()
                smoke = await _call_tool(session, "agent_smoke", {}, timings)
                unscoped = await session.call_tool("agent_slots_list", {})
                assert unscoped.isError is True
                await _call_tool(session, "agent_slots_list", {"route": "acp"}, timings)
                await _call_tool(session, "agent_slots_list", {"all_routes": True}, timings)
                await _call_tool(
                    session,
                    "agent_slots_cleanup",
                    {"max_per_route": 1, "apply": False, "route": "acp"},
                    timings,
                )
                _write_config(config_path, new_slot)
                reloaded_slots = await _call_tool(
                    session,
                    "agent_slots_list",
                    {"route": "acp"},
                    timings,
                )
                reloaded_smoke = await _call_tool(session, "agent_smoke", {}, timings)
                with pytest.raises(TimeoutError):
                    with anyio.fail_after(0.01):
                        await session.call_tool("agent_smoke", {})
                results.update(
                    {
                        "smoke": smoke,
                        "slots": reloaded_slots,
                        "reloaded_smoke": reloaded_smoke,
                    }
                )
                await anyio.sleep(0.5)
        except BaseExceptionGroup as group:
            # The deliberately cancelled 0.01s call_tool above leaves a late server response
            # in flight; if it lands while the stdio client is tearing down, stdout_reader
            # raises BrokenResourceError on the closed memory stream. That shutdown race is
            # not what this test asserts, so swallow it — but only once every result has
            # been collected, and only if nothing else went wrong in the task group.
            _broken, rest = group.split(anyio.BrokenResourceError)
            if rest is not None or not results:
                raise
        return timings, results

    timings, results = anyio.run(exercise)

    assert all(duration < 5 for duration in timings)
    assert (
        results["smoke"]["config_fingerprint_loaded"]
        == results["smoke"]["config_fingerprint_current"]
    )
    assert results["reloaded_smoke"]["config_reloaded"] is True
    assert results["reloaded_smoke"]["reload_required"] is False
    assert results["slots"]["result"][0]["path"] == str(new_slot.resolve())
    assert results["smoke"]["codex_model_catalog"]["status"] == "missing"
    assert set(results["smoke"]["codex_model_catalog"]["profile_resolution_errors"]) == {
        "mechanical",
        "balanced",
        "deep",
    }
    assert results["smoke"]["codex_quality_profiles"] == {}
    assert "status" in results["smoke"]
    assert "failures" in results["smoke"]
    assert "model_control_scope" in results["smoke"]
    with sqlite3.connect(tmp_path / "runs" / "jobs.sqlite3") as database:
        slot_count = database.execute("select count(*) from slots").fetchone()[0]
    assert slot_count == 0


async def _call_tool(
    session: ClientSession,
    name: str,
    arguments: dict[str, object],
    timings: list[float],
) -> dict[object, object]:
    started = time.monotonic()
    with anyio.fail_after(5):
        result = await session.call_tool(name, arguments)
    timings.append(time.monotonic() - started)
    assert result.isError is False
    assert result.structuredContent is not None
    return result.structuredContent


def _write_config(config_path: Path, slot_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    slot_relative = slot_path.relative_to(config_path.parent.parent).as_posix()
    slot_root_relative = slot_path.parent.relative_to(config_path.parent.parent).as_posix()
    config_path.write_text(
        f'''[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "slots"
worktree_base = "repo"
slot_root = "{slot_root_relative}"
agy_command = "agy"

[control.model_catalog]
cache_path = "{(config_path.parent / "missing-models_cache.json").as_posix()}"

[routes.acp]
path = "repo"
required_branch = "main"

[slots."acp-1"]
route = "acp"
path = "{slot_relative}"
''',
        encoding="utf-8",
    )


def _initialize_repository(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=ACP Test",
        "-c",
        "user.email=acp@example.test",
        "commit",
        "-m",
        "seed",
    )


def _create_worktree(repo: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(path), "HEAD")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
