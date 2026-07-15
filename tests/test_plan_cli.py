import json
import tempfile
from pathlib import Path

from agent_control_plane.app.runtime.cli import _build_parser, main
from agent_control_plane.app.runtime.orchestrator import AgentControlPlane


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
    dispatch = _build_parser().parse_args(
        ["plan", "dispatch", "transfer", "--max-jobs", "3"]
    )
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
