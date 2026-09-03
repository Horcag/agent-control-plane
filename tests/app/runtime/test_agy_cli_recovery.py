from types import SimpleNamespace
from unittest.mock import Mock

from agent_control_plane.app.runtime.job_execution_service import JobExecutionService
from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.features.agent_runner import AgentRunResult, ModelProfile


def test_agy_cli_quota_recovery_retries_same_model_progress_and_excludes_failed_account(
    tmp_path, monkeypatch
):
    switcher = _SyntheticSwitcher(current_ids=["acct-exhausted", "acct-success"])
    monkeypatch.setattr(
        "agent_control_plane.app.runtime.job_execution_service.configured_cli_switcher",
        lambda: switcher,
    )
    runner = _QuotaThenSuccessRunner()
    store = _ExecutionStore(_job(tmp_path))
    service = JobExecutionService(
        config=_config(),
        store=store,
        policy=_AllowPolicy(),
        model_routing=_ModelRouting(),
        guardrails=_CleanGuardrails(),
        finalizer=_Finalizer(store),
        runner_factory=lambda _backend: runner,
        quota_broker=None,
    )

    finished = service.execute("job-agy-recovery")

    assert finished.status == "completed"
    assert [spec.agy_model for spec in runner.specs] == ["gemini-3.8-flash-high"] * 2
    assert "Continue from existing progress after quota recovery" in runner.specs[1].prompt
    assert runner.specs[1].codex_resume_thread_id == "agy-progress-thread"
    assert switcher.switch_calls == [
        {
            "model": "gemini-3.8-flash-high",
            "dry_run": False,
            "failed_account_id": "acct-exhausted",
            "excluded": {"acct-exhausted"},
        }
    ]
    assert store.attempts == [1, 2]
    assert not any(
        "Retrying after agy account auto-switch" in event[1]
        for event in store.events
        if event[0] == "error"
    )


def test_optional_cli_recovery_can_rotate_more_than_once_and_retains_model(tmp_path, monkeypatch):
    switcher = Mock(auto_switch=True)
    switcher.switch.return_value = {"account_id": "next", "verified": True}
    monkeypatch.setattr(
        "agent_control_plane.app.runtime.job_execution_service.configured_cli_switcher",
        lambda: switcher,
    )
    service = object.__new__(JobExecutionService)
    service.store = Mock()
    service.config = SimpleNamespace(
        defaults=SimpleNamespace(agy_model="fallback", auto_switch_agy_on_quota=False)
    )
    job = SimpleNamespace(job_id="test", agy_model="gemini-test")
    log = tmp_path / "attempt.log"
    log.write_text("Individual quota reached. Resets in 10m.")
    failed = {"previous"}
    result = service._auto_switch_agy_after_quota_failure(
        job, log, "", already_used=True, failed_account_id="current", failed_accounts=failed
    )
    assert result and "verified" in result
    assert failed == {"previous", "current"}
    switcher.switch.assert_called_once_with(
        model="gemini-test", dry_run=False, failed_account_id="current", excluded=failed
    )


def test_non_quota_failure_never_switches_account(tmp_path, monkeypatch):
    switcher = Mock(auto_switch=True)
    monkeypatch.setattr(
        "agent_control_plane.app.runtime.job_execution_service.configured_cli_switcher",
        lambda: switcher,
    )
    service = object.__new__(JobExecutionService)
    log = tmp_path / "attempt.log"
    log.write_text("Network connection failed")
    assert (
        service._auto_switch_agy_after_quota_failure(SimpleNamespace(), log, "", already_used=False)
        is None
    )
    switcher.switch.assert_not_called()


def test_unknown_failed_identity_never_blames_current_peer_account(tmp_path, monkeypatch):
    switcher = Mock(auto_switch=True)
    monkeypatch.setattr(
        "agent_control_plane.app.runtime.job_execution_service.configured_cli_switcher",
        lambda: switcher,
    )
    service = object.__new__(JobExecutionService)
    service.store = Mock()
    log = tmp_path / "attempt.log"
    log.write_text("Individual quota reached")
    assert (
        service._auto_switch_agy_after_quota_failure(
            SimpleNamespace(job_id="test"), log, "", already_used=False
        )
        is None
    )
    switcher.switch.assert_not_called()


class _SyntheticSwitcher:
    auto_switch = True

    def __init__(self, *, current_ids):
        self._current_ids = list(current_ids)
        self.switch_calls = []

    def prepare_attempt(self, model):
        return self._current_ids.pop(0)

    def switch(self, **kwargs):
        self.switch_calls.append(kwargs)
        assert kwargs["failed_account_id"] != "acct-success"
        assert kwargs["failed_account_id"] in kwargs["excluded"]
        return {"account_id": "acct-success", "verified": True}


class _QuotaThenSuccessRunner:
    def __init__(self):
        self.specs = []

    def run(self, spec, *, cancel_requested, pid_observed):
        self.specs.append(spec)
        pid_observed(None)
        assert not cancel_requested()
        if len(self.specs) == 1:
            spec.log_path.write_text("Individual quota reached. Resets in 10m.\n", encoding="utf-8")
            return AgentRunResult(
                status="exited_without_result",
                completed=False,
                exit_code=1,
                result_status=None,
                message="agy exited without result",
                metrics=_metrics(thread_id="agy-progress-thread"),
            )
        spec.log_path.write_text("Status: completed\n", encoding="utf-8")
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgentRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="completed after CLI switch",
        )


class _ExecutionStore:
    def __init__(self, job):
        self.job = job
        self.events = []
        self.attempts = []

    def get_job(self, job_id):
        assert job_id == self.job.job_id
        return self.job

    def cancel_requested(self, job_id):
        assert job_id == self.job.job_id
        return False

    def update_job(self, job_id, **values):
        assert job_id == self.job.job_id
        for key, value in values.items():
            setattr(self.job, key, value)
        return self.job

    def start_attempt(self, job_id, attempt_no, log_path):
        assert job_id == self.job.job_id
        self.attempts.append(attempt_no)
        self.job.log_path = log_path

    def finish_attempt(self, job_id, attempt_no, status, **_values):
        assert job_id == self.job.job_id
        assert attempt_no in self.attempts
        self.events.append(("attempt", f"{attempt_no}:{status}"))

    def add_event(self, job_id, level, message):
        assert job_id == self.job.job_id
        self.events.append((level, message))

    def record_attempt_metrics(self, job_id, attempt_no, **_values):
        assert job_id == self.job.job_id
        assert attempt_no == 1

    def set_runner_failure(self, job_id, runner_failure):
        assert job_id == self.job.job_id
        self.job.runner_failure = runner_failure


class _Finalizer:
    quota_broker = None

    def __init__(self, store):
        self.store = store

    def finish(self, job_id, status, last_error=None, *, worker_instance_id=None):
        assert worker_instance_id is None
        return self.store.update_job(job_id, status=status, last_error=last_error)


class _AllowPolicy:
    @staticmethod
    def check_start(_request):
        return SimpleNamespace(ok=True, reasons=())


class _CleanGuardrails:
    @staticmethod
    def workspace_baseline(_job):
        return object()

    @staticmethod
    def route_root_baseline(_job, _route_config):
        return None

    @staticmethod
    def workspace_violation(_job, _baseline):
        return None

    @staticmethod
    def route_root_violation(_job, _baseline):
        return None

    @staticmethod
    def dirty_diff_violation(_job, _baseline, *, max_changed_lines):
        assert max_changed_lines == 100
        return None

    @staticmethod
    def preserve_dirty_state(_job, *, prefix):
        return ""


class _ModelRouting:
    catalog = SimpleNamespace(source="test", version="v1")

    @staticmethod
    def ladder_for_explicit_model(_model, _effort):
        return (ModelProfile("codex-placeholder", "medium"),)


def _job(tmp_path):
    run_dir = tmp_path / "runs" / "job-agy-recovery"
    result_path = tmp_path / "tasks" / "task-agy-recovery" / "result.md"
    prompt_path = run_dir / "prompt.md"
    run_dir.mkdir(parents=True)
    result_path.parent.mkdir(parents=True)
    prompt_path.write_text("Implement the assigned task with existing progress.", encoding="utf-8")
    return SimpleNamespace(
        job_id="job-agy-recovery",
        task_id="task-agy-recovery",
        route="main",
        workspace_path=tmp_path / "workspace",
        expected_branch="main",
        expected_result_status="completed",
        status="running",
        config_path=tmp_path / "config.toml",
        run_dir=run_dir,
        prompt_path=prompt_path,
        result_path=result_path,
        log_path=None,
        runner_pid=None,
        runner_process_identity=None,
        agy_pid=None,
        backend="agy",
        agy_model="gemini-3.8-flash-high",
        codex_model=None,
        codex_reasoning_effort=None,
        codex_quality_tier=None,
        codex_tool_call_budget=None,
        workspace_access="ide_mcp",
        started_at="2026-09-03T00:00:00+00:00",
        timeout_sec=30,
        idle_timeout_sec=30,
        print_timeout="10s",
        max_restarts=0,
        yolo=False,
        allow_dirty=False,
        read_only=False,
        last_error=None,
        runner_failure=None,
    )


def _config():
    return SimpleNamespace(
        agy_command="agy",
        codex_command="codex",
        claude_command="claude",
        routes={},
        defaults=SimpleNamespace(
            agy_model="fallback-agy",
            auto_switch_agy_on_quota=False,
            guardrail_poll_sec=0,
            codex_forbidden_tool_markers=(),
            dirty_diff_max_changed_lines=100,
            codex_model="gpt-placeholder",
            codex_reasoning_effort="medium",
            codex_sandbox_mode="workspace-write",
            codex_disabled_mcp_servers=(),
            no_progress_timeout_sec=60,
            tool_timeout_limit=6,
            tool_call_budget_grace_sec=120,
            invalid_verification_grace_sec=120,
            codex_sessions_root=None,
            claude_permission_mode="default",
            claude_allowed_tools=(),
            claude_sessions_root=None,
            claude_max_turns=0,
            claude_bare=True,
        ),
    )


def _metrics(*, thread_id):
    return AttemptMetrics(
        duration_sec=1.0,
        thread_id=thread_id,
        event_count=1,
        turn_completed=False,
        usage_available=False,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        tool_calls=0,
        failed_tool_calls=0,
        error_events=1,
        tool_counts=(),
        estimated_credits=None,
        estimated_api_usd=None,
        rate_card_version="test",
        event_log_path=None,
    )
