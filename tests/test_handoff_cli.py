from agent_control_plane.app.runtime.cli import _build_parser


def test_inbox_list_parser_supports_subagent_sync_and_all_statuses() -> None:
    args = _build_parser().parse_args(
        [
            "inbox",
            "list",
            "--status",
            "all",
            "--sync-subagents",
            "--since-hours",
            "24",
            "--max-files",
            "100",
            "--parent-thread-id",
            "parent-1",
        ]
    )

    assert args.command == "inbox"
    assert args.inbox_command == "list"
    assert args.status == "all"
    assert args.sync_subagents is True
    assert args.since_hours == 24
    assert args.max_files == 100
    assert args.parent_thread_id == "parent-1"


def test_inbox_resolve_parser_keeps_review_separate_from_plan_acceptance() -> None:
    args = _build_parser().parse_args(
        ["inbox", "resolve", "agent_job:job-1", "--decision", "accepted"]
    )

    assert args.item_id == "agent_job:job-1"
    assert args.decision == "accepted"


def test_slot_checkpoint_parser_requires_the_terminal_job_identity() -> None:
    args = _build_parser().parse_args(["slots", "checkpoint", "app-1", "--job-id", "job-1"])

    assert args.slot_command == "checkpoint"
    assert args.name == "app-1"
    assert args.job_id == "job-1"


def test_reconcile_and_internal_worker_identity_are_explicit_cli_inputs() -> None:
    reconcile = _build_parser().parse_args(["reconcile", "--job-id", "job-1"])
    worker = _build_parser().parse_args(
        [
            "run-job",
            "--job-id",
            "job-1",
            "--worker-instance-id",
            "worker-1",
        ]
    )

    assert reconcile.command == "reconcile"
    assert reconcile.job_id == "job-1"
    assert worker.worker_instance_id == "worker-1"
