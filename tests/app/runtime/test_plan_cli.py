import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_control_plane.app.runtime.cli import _build_parser, main
from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
from agent_control_plane.app.runtime.plan_cli import read_plan_manifest


def test_root_parser_registers_extracted_demo_and_plan_groups() -> None:
    root_help = " ".join(_build_parser().format_help().split())

    assert "demo" in root_help
    assert "Run or inspect the self-contained offline pipeline demo" in root_help
    assert "plan" in root_help
    assert "Manage durable multi-job supervisor plans" in root_help


def test_plan_summary_parser_accepts_cursor_and_limits() -> None:
    args = _build_parser().parse_args(
        [
            "plan",
            "summary",
            "transfer",
            "--since",
            "7",
            "--event-limit",
            "25",
            "--item-limit",
            "5",
        ]
    )

    assert args.command == "plan"
    assert args.plan_command == "summary"
    assert args.plan_id == "transfer"
    assert args.since == 7
    assert args.event_limit == 25
    assert args.item_limit == 5


def test_plan_manifest_parser_is_available_without_the_cli_composition_module(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "plan.json"
    manifest.write_text('{"plan_id": "transfer", "tasks": []}', encoding="utf-8")

    assert read_plan_manifest(str(manifest)) == {"plan_id": "transfer", "tasks": []}


def test_cli_composition_keeps_legacy_entrypoint_imports() -> None:
    assert callable(_build_parser)
    assert callable(main)


def test_start_parser_can_bind_a_retry_to_a_logical_plan_task() -> None:
    args = _build_parser().parse_args(
        [
            "start",
            "--task-id",
            "schema-repair-r2",
            "--route",
            "dev",
            "--plan-id",
            "transfer",
            "--plan-task-id",
            "schema",
        ]
    )

    assert args.plan_id == "transfer"
    assert args.plan_task_id == "schema"


def test_start_parser_accepts_native_workspace_access() -> None:
    args = _build_parser().parse_args(
        [
            "start",
            "--task-id",
            "native-task",
            "--route",
            "dev",
            "--workspace-access",
            "native",
        ]
    )

    assert args.workspace_access == "native"


def test_list_reports_persisted_workspace_access(capsys) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config_path = root / "workspaces.toml"
        config_path.write_text(_config_text(root), encoding="utf-8")
        control = AgentControlPlane.from_config_path(config_path)
        control.store.create_job(
            job_id="job-native",
            task_id="native-task",
            route="app",
            workspace_path=root / "repo",
            expected_branch="main",
            config_path=config_path,
            run_dir=root / "runs" / "job-native",
            prompt_path=root / "runs" / "job-native" / "prompt.md",
            result_path=root / ".agent-work" / "tasks" / "native-task" / "result.md",
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=0,
            yolo=False,
            allow_dirty=False,
            read_only=False,
            workspace_access="native",
        )

        assert main(["list", "--config", str(config_path)]) == 0
        jobs = json.loads(capsys.readouterr().out)

        assert jobs[0]["workspace_access"] == "native"


def test_plan_watch_parser_requires_a_cursor() -> None:
    args = _build_parser().parse_args(
        ["plan", "watch", "transfer", "--since", "9", "--timeout-sec", "30"]
    )

    assert args.plan_command == "watch"
    assert args.plan_id == "transfer"
    assert args.since == 9
    assert args.timeout_sec == 30


def test_plan_dispatch_and_retry_parsers_expose_one_shot_controls() -> None:
    dispatch = _build_parser().parse_args(["plan", "dispatch", "transfer", "--max-jobs", "3"])
    retry = _build_parser().parse_args(
        ["plan", "retry", "transfer", "schema", "--brief-file", "repair.md"]
    )

    assert dispatch.plan_command == "dispatch"
    assert dispatch.max_jobs == 3
    assert retry.plan_command == "retry"
    assert retry.task_id == "schema"
    assert retry.brief_file == "repair.md"

    add = _build_parser().parse_args(
        [
            "plan",
            "add-task",
            "transfer",
            "--task-id",
            "api",
            "--title",
            "API",
            "--route",
            "app",
            "--brief-file",
            "api.md",
            "--backend",
            "codex",
            "--workspace-access",
            "native",
        ]
    )
    assert add.route == "app"
    assert add.brief_file == "api.md"
    assert add.backend == "codex"


def test_cli_restart_requires_explicit_retry_after_failed_dispatch(tmp_path: Path) -> None:
    config = tmp_path / "workspaces.toml"
    config.write_text(_config_text(tmp_path), encoding="utf-8")
    manifest = tmp_path / "plan.json"
    manifest.write_text(
        json.dumps(
            {
                "plan_id": "restart-drill",
                "title": "Restart drill",
                "tasks": [
                    {
                        "task_id": "dispatch",
                        "title": "Dispatch",
                        "execution": {"route": "missing", "brief": "Fail safely"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    created = _run_cli(
        tmp_path, "plan", "create", "--manifest", str(manifest), "--config", str(config)
    )
    first = _run_cli(tmp_path, "plan", "dispatch", "restart-drill", "--config", str(config))
    second = _run_cli(tmp_path, "plan", "dispatch", "restart-drill", "--config", str(config))
    retried = _run_cli(
        tmp_path, "plan", "retry", "restart-drill", "dispatch", "--config", str(config)
    )
    third = _run_cli(tmp_path, "plan", "dispatch", "restart-drill", "--config", str(config))

    assert created["plan_id"] == "restart-drill"
    assert first["claimed"] == 1
    assert first["failures"][0]["attempt_no"] == 1
    assert second["claimed"] == 0
    assert second["failures"] == []
    assert retried["task"]["state"] == "ready"
    assert retried["task"]["attempt_no"] == 1
    assert third["claimed"] == 1
    assert third["failures"][0]["attempt_no"] == 2


def _run_cli(cwd: Path, *arguments: str) -> dict[str, object]:
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[3] / "src"
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "agent_control_plane.app.runtime.cli", *arguments],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_plan_run_parser_exposes_autonomous_until_review_controls() -> None:
    args = _build_parser().parse_args(
        [
            "plan",
            "run",
            "transfer",
            "--until-review",
            "--max-jobs",
            "3",
            "--poll-interval-sec",
            "2",
            "--timeout-sec",
            "60",
        ]
    )

    assert args.plan_command == "run"
    assert args.plan_id == "transfer"
    assert args.until_review is True
    assert args.max_jobs == 3
    assert args.poll_interval_sec == 2
    assert args.timeout_sec == 60


def test_plan_lifecycle_and_retention_parsers_are_explicit() -> None:
    cancel = _build_parser().parse_args(["plan", "cancel", "transfer"])
    archive = _build_parser().parse_args(["plan", "archive", "transfer"])
    listed = _build_parser().parse_args(["plan", "list", "--include-archived"])
    retention = _build_parser().parse_args(
        ["gc", "--older-than-days", "30", "--limit", "200", "--apply"]
    )

    assert cancel.plan_command == "cancel"
    assert archive.plan_command == "archive"
    assert listed.include_archived is True
    assert retention.command == "gc"
    assert retention.older_than_days == 30
    assert retention.limit == 200
    assert retention.apply is True


def test_plan_manifest_round_trips_through_real_cli(capsys) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config = root / "workspaces.toml"
        config.write_text(_config_text(root), encoding="utf-8")
        manifest = root / "plan.json"
        manifest.write_text(
            json.dumps(
                {
                    "plan_id": "transfer",
                    "title": "Transfer",
                    "tasks": [
                        {"task_id": "schema", "title": "Schema"},
                        {"task_id": "api", "title": "API", "depends_on": ["schema"]},
                    ],
                }
            ),
            encoding="utf-8",
        )

        assert main(["plan", "create", "--manifest", str(manifest), "--config", str(config)]) == 0
        created = json.loads(capsys.readouterr().out)
        assert created["progress"] == "0/2"
        assert created["ready_next"][0]["task_id"] == "schema"

        assert main(["plan", "summary", "transfer", "--config", str(config)]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["cursor"] == created["cursor"]
        assert summary["changes"] == []

        assert (
            main(
                [
                    "plan",
                    "run",
                    "transfer",
                    "--until-review",
                    "--timeout-sec",
                    "1",
                    "--config",
                    str(config),
                ]
            )
            == 0
        )
        run = json.loads(capsys.readouterr().out)
        assert run["reason"] == "manual_dispatch_required"
        assert run["snapshot"]["ready_next"][0]["task_id"] == "schema"


def test_plan_cancel_archive_and_gc_round_trip_through_real_cli(capsys) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config = root / "workspaces.toml"
        config.write_text(_config_text(root), encoding="utf-8")

        assert (
            main(
                [
                    "plan",
                    "create",
                    "--plan-id",
                    "lifecycle",
                    "--title",
                    "Lifecycle",
                    "--config",
                    str(config),
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert main(["plan", "cancel", "lifecycle", "--config", str(config)]) == 0
        cancelled = json.loads(capsys.readouterr().out)
        assert cancelled["snapshot"]["status"] == "cancelled"

        assert main(["plan", "archive", "lifecycle", "--config", str(config)]) == 0
        archived = json.loads(capsys.readouterr().out)
        assert archived["archived_at"] is not None

        assert main(["plan", "list", "--config", str(config)]) == 0
        assert json.loads(capsys.readouterr().out) == []
        assert (
            main(
                [
                    "plan",
                    "list",
                    "--include-archived",
                    "--config",
                    str(config),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)[0]["plan_id"] == "lifecycle"

        assert (
            main(
                [
                    "gc",
                    "--older-than-days",
                    "0",
                    "--apply",
                    "--config",
                    str(config),
                ]
            )
            == 0
        )
        collected = json.loads(capsys.readouterr().out)
        assert collected["applied"]["plans"] == 1


def _config_text(root: Path) -> str:
    path = root.as_posix()
    return f"""
[control]
coordination_root = "{path}/.agent-work"
runs_root = "{path}/runs"
database = "{path}/runs/jobs.sqlite3"
worktree_root = "{path}/worktrees"
worktree_base = "{path}/repo"
slot_root = "{path}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"
max_restarts = 0
yolo = false
allow_dirty = false
prepare_slots = false

[routes.app]
path = "{path}/repo"
required_branch = "main"
"""
