from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_control_plane.features.agent_runner.lib.codex_process_monitor import (
    CodexProcessMonitor,
)
from agent_control_plane.features.agent_runner.lib.codex_runner import (
    CodexExecRunner,
    _workspace_environment,
)
from agent_control_plane.features.agent_runner.lib.codex_watchdog import (
    dirty_file_markers_from_porcelain,
    porcelain_changed_path,
    productive_log_activity_if_needed,
)
from agent_control_plane.features.agent_runner.lib.runner import AgentRunSpec


class CodexRunnerCommandTest(unittest.TestCase):
    def test_workspace_environment_uses_slot_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "slot"
            local_virtual_env = workspace / ".venv"
            local_scripts = local_virtual_env / ("Scripts" if os.name == "nt" else "bin")
            local_scripts.mkdir(parents=True)
            inherited_virtual_env = root / "controller-venv"
            inherited_scripts = inherited_virtual_env / ("Scripts" if os.name == "nt" else "bin")
            base_path = str(root / "bin")
            inherited_path = os.pathsep.join((str(inherited_scripts), base_path))

            with patch.dict(
                os.environ,
                {
                    "PATH": inherited_path,
                    "VIRTUAL_ENV": str(inherited_virtual_env),
                    "UV_PYTHON": "3.12",
                    "UV_PROJECT_ENVIRONMENT": str(inherited_virtual_env),
                    "PYTHONHOME": str(root / "python-home"),
                    "CONDA_PREFIX": str(root / "conda"),
                },
                clear=True,
            ):
                environment = _workspace_environment(workspace)

            self.assertEqual(environment["VIRTUAL_ENV"], str(local_virtual_env))
            self.assertEqual(environment["UV_PROJECT_ENVIRONMENT"], str(local_virtual_env))
            self.assertNotIn("UV_PYTHON", environment)
            self.assertNotIn("PYTHONHOME", environment)
            self.assertNotIn("CONDA_PREFIX", environment)
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(local_scripts))
            self.assertNotIn(str(inherited_scripts), environment["PATH"].split(os.pathsep))

    def test_workspace_environment_clears_inherited_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace-without-venv"
            workspace.mkdir()
            with patch.dict(
                os.environ,
                {
                    "PATH": "base-bin",
                    "VIRTUAL_ENV": "controller-venv",
                    "UV_PYTHON": "3.12",
                    "UV_PROJECT_ENVIRONMENT": "controller-venv",
                },
                clear=True,
            ):
                environment = _workspace_environment(workspace)

            self.assertNotIn("VIRTUAL_ENV", environment)
            self.assertNotIn("UV_PYTHON", environment)
            self.assertNotIn("UV_PROJECT_ENVIRONMENT", environment)
            self.assertEqual(environment["PATH"], "base-bin")

    def test_build_command_uses_exec_with_configured_writable_sandbox(self) -> None:
        spec = _spec(read_only=False, yolo=False, codex_sandbox_mode="workspace-write")

        command = CodexExecRunner._build_command(spec)

        self.assertEqual(command[0:4], ["codex", "exec", "--model", "gpt-5"])
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("--json", command)
        self.assertIn("--disable", command)
        self.assertIn("image_generation", command)
        self.assertIn("--cd", command)
        self.assertIn(str(spec.workspace_path), command)
        self.assertIn("--output-last-message", command)
        self.assertIn(str(spec.log_path.with_suffix(".last-message.md")), command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn(spec.prompt, command)

    def test_build_command_resumes_same_thread_for_escalation(self) -> None:
        spec = _spec(
            read_only=False,
            yolo=False,
            codex_resume_thread_id="019ef56b-74b4-70e2-9b0d-0e2c0ddfbc9c",
        )

        command = CodexExecRunner._build_command(spec)

        self.assertEqual(command[0:3], ["codex", "exec", "resume"])
        self.assertIn("--model", command)
        self.assertIn("gpt-5", command)
        self.assertIn(spec.codex_resume_thread_id, command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--cd", command)
        self.assertNotIn("--sandbox", command)

    def test_build_command_allows_danger_full_access_sandbox_without_yolo(self) -> None:
        command = CodexExecRunner._build_command(
            _spec(read_only=False, yolo=False, codex_sandbox_mode="danger-full-access")
        )

        self.assertIn("--sandbox", command)
        self.assertIn("danger-full-access", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_build_command_can_disable_configured_mcp_servers(self) -> None:
        command = CodexExecRunner._build_command(
            _spec(
                read_only=False,
                yolo=False,
                codex_disabled_mcp_servers=(
                    "agentbridge_idea_8644",
                    "agentbridge_dataspell_8643",
                ),
            )
        )

        self.assertIn("mcp_servers.agentbridge_idea_8644.enabled=false", command)
        self.assertIn("mcp_servers.agentbridge_dataspell_8643.enabled=false", command)

    def test_build_command_uses_read_only_sandbox_when_requested(self) -> None:
        command = CodexExecRunner._build_command(_spec(read_only=True, yolo=False))

        self.assertIn("read-only", command)
        self.assertNotIn("workspace-write", command)

    def test_yolo_bypasses_sandbox_flags(self) -> None:
        command = CodexExecRunner._build_command(_spec(read_only=False, yolo=True))

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--sandbox", command)

    def test_completed_wait_observes_usage_event_before_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.with_suffix(".events.jsonl").write_text(
                '{"type":"turn.completed","usage":{}}\n',
                encoding="utf-8",
            )
            proc = _FakeProc()
            spec = _spec(read_only=True, yolo=False, log_path=log_path)

            CodexProcessMonitor()._await_completed_process(proc, spec)

            self.assertEqual(proc.wait_timeout, 1.0)
            self.assertFalse(proc.terminated)

    def test_timeout_result_stops_clean_workspace_without_any_activity(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, codex_no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=4.0,
            last_productive_mono=4.0,
            workspace_dirty=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "no_progress_timeout")
        self.assertTrue(proc.terminated)

    def test_timeout_result_stops_when_output_continues_without_progress(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, codex_no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=9.5,
            last_productive_mono=4.0,
            workspace_dirty=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "no_progress_timeout")
        self.assertIn("workspace is clean", result.message)
        self.assertTrue(proc.terminated)

    def test_timeout_result_stops_dirty_workspace_without_file_progress(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, codex_no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=9.5,
            last_productive_mono=4.0,
            workspace_dirty=True,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "no_progress_timeout")
        self.assertIn("workspace is dirty", result.message)
        self.assertTrue(proc.terminated)

    def test_timeout_result_keeps_recent_productive_progress(self) -> None:
        proc = _FakeProc()
        spec = _spec(read_only=False, yolo=False, codex_no_progress_timeout_sec=5)

        result = CodexProcessMonitor()._timeout_result_if_needed(
            proc,
            spec,
            now=10.0,
            deadline_mono=100.0,
            last_output_mono=9.5,
            last_productive_mono=9.5,
            workspace_dirty=True,
        )

        self.assertIsNone(result)
        self.assertFalse(proc.terminated)

    def test_repeated_tool_timeout_stops_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "2026-07-02 ERROR codex_core::tools::router: error=Exit code: 124\n"
                "2026-07-02 ERROR codex_core::tools::router: error=Exit code: 124\n",
                encoding="utf-8",
            )
            proc = _FakeProc()
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            result, scan_size, timeout_count = CodexProcessMonitor()._tool_timeout_result_if_needed(
                proc,
                spec,
                scan_size=0,
                timeout_count=0,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "tool_timeout")
            self.assertIn("Exit code: 124", result.message)
            self.assertEqual(timeout_count, 2)
            self.assertGreater(scan_size, 0)
            self.assertTrue(proc.terminated)

    def test_forbidden_web_search_stops_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text("codex\nweb search: https://example.com/\n", encoding="utf-8")
            proc = _FakeProc()
            spec = _spec(
                read_only=False,
                yolo=False,
                log_path=log_path,
                codex_forbidden_tool_markers=("web_search",),
            )

            result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                proc,
                spec,
                scan_size=0,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "forbidden_tool_usage")
            self.assertIn("web_search", result.message)
            self.assertGreater(scan_size, 0)
            self.assertTrue(proc.terminated)

    def test_forbidden_raw_exec_stops_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text("codex\nexec\npwsh -Command git status\n", encoding="utf-8")
            proc = _FakeProc()
            spec = _spec(
                read_only=False,
                yolo=False,
                log_path=log_path,
                codex_forbidden_tool_markers=("raw_exec",),
            )

            result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                proc,
                spec,
                scan_size=0,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "forbidden_tool_usage")
            self.assertIn("raw_exec", result.message)
            self.assertGreater(scan_size, 0)
            self.assertTrue(proc.terminated)

    def test_forbidden_agentbridge_project_discovery_stops_runner(self) -> None:
        cases = {
            "agentbridge_global_search": "search_text",
            "agentbridge_global_symbols": "search_symbols",
            "agentbridge_global_files": "list_project_files",
            "agentbridge_global_tree": "list_directory_tree",
            "agentbridge_external_attach": "attach_external_dir",
        }
        servers = (
            "agentbridge_idea_8644",
            "agentbridge_idea_64343",
            "agentbridge_dataspell_8643",
        )
        for marker_name, tool_name in cases.items():
            for server in servers:
                with (
                    self.subTest(marker_name=marker_name, server=server),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    log_path = Path(temp) / "attempt-001.log"
                    log_path.write_text(
                        f"mcp: {server}/{tool_name} started\n",
                        encoding="utf-8",
                    )
                    proc = _FakeProc()
                    spec = _spec(
                        read_only=True,
                        yolo=False,
                        log_path=log_path,
                        codex_forbidden_tool_markers=(marker_name,),
                    )

                    result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                        proc,
                        spec,
                        scan_size=0,
                    )

                    self.assertIsNotNone(result)
                    assert result is not None
                    self.assertEqual(result.status, "forbidden_tool_usage")
                    self.assertIn(marker_name, result.message)
                    self.assertGreater(scan_size, 0)
                    self.assertTrue(proc.terminated)

    def test_forbidden_markers_are_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text("codex\nexec\npwsh -Command git status\n", encoding="utf-8")
            proc = _FakeProc()
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            result, scan_size = CodexProcessMonitor()._forbidden_tool_result_if_needed(
                proc,
                spec,
                scan_size=0,
            )

            self.assertIsNone(result)
            self.assertEqual(scan_size, 0)
            self.assertFalse(proc.terminated)

    def test_agentbridge_tool_activity_counts_as_productive_log_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "codex\nmcp: agentbridge_idea_8644/read_file started\n"
                "mcp: agentbridge_idea_8644/read_file (completed)\n",
                encoding="utf-8",
            )
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            productive, scan_size = productive_log_activity_if_needed(
                spec,
                scan_size=0,
            )

            self.assertTrue(productive)
            self.assertGreater(scan_size, 0)

    def test_second_idea_agentbridge_activity_counts_as_productive_log_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "codex\nmcp: agentbridge_idea_64343/read_file started\n",
                encoding="utf-8",
            )
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            productive, scan_size = productive_log_activity_if_needed(spec, scan_size=0)

            self.assertTrue(productive)
            self.assertGreater(scan_size, 0)

    def test_model_manager_noise_is_not_productive_log_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "attempt-001.log"
            log_path.write_text(
                "ERROR codex_models_manager::manager: failed to refresh available models\n",
                encoding="utf-8",
            )
            spec = _spec(read_only=False, yolo=False, log_path=log_path)

            productive, scan_size = productive_log_activity_if_needed(
                spec,
                scan_size=0,
            )

            self.assertFalse(productive)
            self.assertGreater(scan_size, 0)


class CodexRunnerProgressSignatureTest(unittest.TestCase):
    def test_porcelain_changed_path_uses_target_path_for_renames(self) -> None:
        self.assertEqual(
            porcelain_changed_path("R  old/name.txt -> new/name.txt"),
            "new/name.txt",
        )

    def test_dirty_file_markers_include_changed_file_stat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            changed = root / "frontend" / "preview.ts"
            changed.parent.mkdir(parents=True)
            changed.write_text("first", encoding="utf-8")

            markers = dirty_file_markers_from_porcelain(root, "?? frontend/preview.ts")

            self.assertEqual(len(markers), 1)
            self.assertTrue(markers[0].startswith("dirty-file:frontend/preview.ts:"))
            self.assertTrue(markers[0].endswith(":5"))


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.wait_timeout: float | None = None

    def poll(self) -> int | None:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeout = timeout
        return 0

    def kill(self) -> None:
        self.killed = True


def _spec(
    *,
    read_only: bool,
    yolo: bool,
    codex_sandbox_mode: str = "workspace-write",
    codex_disabled_mcp_servers: tuple[str, ...] = (),
    codex_no_progress_timeout_sec: int = 0,
    codex_forbidden_tool_markers: tuple[str, ...] = (),
    codex_resume_thread_id: str | None = None,
    log_path: Path | None = None,
) -> AgentRunSpec:
    return AgentRunSpec(
        backend="codex",
        agy_command="agy",
        codex_command="codex",
        codex_model="gpt-5",
        codex_reasoning_effort="low",
        codex_sandbox_mode=codex_sandbox_mode,
        codex_disabled_mcp_servers=codex_disabled_mcp_servers,
        codex_no_progress_timeout_sec=codex_no_progress_timeout_sec,
        codex_forbidden_tool_markers=codex_forbidden_tool_markers,
        codex_resume_thread_id=codex_resume_thread_id,
        prompt="secret task prompt",
        workspace_path=Path("D:/repo/workspace"),
        result_path=Path("D:/repo/.agent-work/tasks/task-1/result.md"),
        log_path=log_path or Path("D:/repo/runs/job-1/attempt-001.log"),
        print_timeout="10s",
        timeout_sec=30,
        idle_timeout_sec=10,
        yolo=yolo,
        read_only=read_only,
    )


if __name__ == "__main__":
    unittest.main()
