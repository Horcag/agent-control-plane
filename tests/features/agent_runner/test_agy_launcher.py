from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_control_plane.app.runtime.cli import main
from agent_control_plane.app.runtime.job_execution_service import JobExecutionService
from agent_control_plane.features.agent_runner.lib import agy_launcher
from agent_control_plane.features.agent_runner.lib.agy_launcher import (
    LEGACY_PROJECT_WRAPPER,
    AgyLauncherError,
    build_agy_launch,
    migrate_project_wrapper,
    render_project_wrapper,
    resolve_agy_launcher,
    restore_managed_config,
    rollback_project_wrapper,
    validate_migration_binding,
)
from agent_control_plane.features.agent_runner.lib.model_routing import ModelProfile
from agent_control_plane.features.agent_runner.lib.pty_runner import PtyAgyRunner
from agent_control_plane.shared.config import load_config


def _executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_wrapper(
    wrapper: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if os.name == "nt":
        sh = shutil.which("sh")
        if not sh:
            pytest.skip("sh is required to run POSIX wrapper on Windows")
        full_env = {**os.environ, **env} if env is not None else None
        return subprocess.run([sh, str(wrapper), *args], check=True, env=full_env)
    return subprocess.run([str(wrapper), *args], check=True, env=env)


def test_proxy_is_preferred_over_absolute_fallback_even_with_hostile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _executable(tmp_path / "real-agy")
    proxy = _executable(tmp_path / "proxy-agy")
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    _executable(hostile / "agy", "#!/bin/sh\nexit 99\n")
    monkeypatch.setenv("PATH", str(hostile))

    assert resolve_agy_launcher(proxy, real) == proxy


def test_absent_proxy_uses_absolute_fallback_but_invalid_proxy_fails_closed(tmp_path: Path) -> None:
    real = _executable(tmp_path / "real-agy")

    assert resolve_agy_launcher(None, real) == real
    with pytest.raises(AgyLauncherError, match="proxy launcher"):
        resolve_agy_launcher(tmp_path / "missing-proxy", real)
    with pytest.raises(AgyLauncherError, match="absolute"):
        resolve_agy_launcher(None, Path("agy"))


def test_managed_mode_never_falls_back_and_receipt_is_not_conversation_proof(
    tmp_path: Path,
) -> None:
    adapter = _executable(tmp_path / "adapter")

    launch = build_agy_launch(
        agy_command="/ignored/real-agy",
        mode="managed",
        adapter=adapter,
        job_id="job-7",
        attempt_ref="attempt-001",
    )

    assert launch.executable == str(adapter)
    assert launch.add_new_project is True
    assert launch.receipt()["managed"] is True
    assert "conversation_id" not in launch.receipt()
    with pytest.raises(AgyLauncherError, match="requires an explicit adapter"):
        build_agy_launch(
            agy_command="/real-agy",
            mode="managed",
            adapter=None,
            job_id="job-7",
            attempt_ref="attempt-001",
        )
    with pytest.raises(AgyLauncherError, match="managed AGY adapter"):
        build_agy_launch(
            agy_command="/real-agy",
            mode="managed",
            adapter=tmp_path / "missing-adapter",
            job_id="job-7",
            attempt_ref="attempt-001",
        )


def test_unmanaged_mode_preserves_explicit_command_without_managed_claim() -> None:
    launch = build_agy_launch(
        agy_command="C:/operator-selected/agy.exe",
        mode="unmanaged",
        adapter=None,
        job_id="job-8",
        attempt_ref="attempt-001",
    )

    assert launch.executable == "C:/operator-selected/agy.exe"
    assert launch.add_new_project is False
    assert launch.receipt()["adapter_path"] is None


def test_project_wrapper_preserves_new_project_and_argument_boundaries(tmp_path: Path) -> None:
    captured = tmp_path / "captured.txt"
    launcher = _executable(
        tmp_path / "launcher",
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{captured.as_posix()}"\n',
    )
    wrapper = tmp_path / "agy-project"
    wrapper.write_bytes(render_project_wrapper(launcher))
    wrapper.chmod(0o755)

    _run_wrapper(
        wrapper,
        ["space value", "$(not-a-command)", 'quote"value'],
        env={"PATH": str(tmp_path / "hostile")},
    )

    assert captured.read_text(encoding="utf-8").splitlines() == [
        "--new-project",
        "space value",
        "$(not-a-command)",
        'quote"value',
    ]


def test_project_wrapper_safely_quotes_hostile_executable_path(tmp_path: Path) -> None:
    captured = tmp_path / "captured.txt"
    launcher_name = (
        "launcher $ ` ' quote space"
        if os.name == "nt"
        else "launcher $ ` back\\slash ' quote space"
    )
    launcher = _executable(
        tmp_path / launcher_name,
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{captured.as_posix()}"\n',
    )
    wrapper = tmp_path / "agy-project"
    wrapper.write_bytes(render_project_wrapper(launcher))
    wrapper.chmod(0o755)

    _run_wrapper(wrapper, ["$(not-a-command)"])

    assert captured.read_text(encoding="utf-8").splitlines() == [
        "--new-project",
        "$(not-a-command)",
    ]


def test_migration_dry_run_apply_and_repeat_are_idempotent(tmp_path: Path) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    wrapper.chmod(0o751)
    launcher = _executable(tmp_path / "real-agy")
    expected = hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest()

    dry_run = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=expected,
        launcher=launcher,
        apply=False,
    )
    assert dry_run.changed is False
    assert wrapper.read_bytes() == LEGACY_PROJECT_WRAPPER

    applied = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=expected,
        launcher=launcher,
        apply=True,
    )
    assert applied.changed is True
    assert (
        applied.backup_path is not None
        and applied.backup_path.read_bytes() == LEGACY_PROJECT_WRAPPER
    )
    assert applied.backup_path is not None
    if os.name != "nt":
        assert stat.S_IMODE(applied.backup_path.stat().st_mode) == 0o751
        assert stat.S_IMODE(wrapper.stat().st_mode) == 0o751

    repeated = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=applied.after_sha256,
        launcher=launcher,
        apply=True,
    )
    assert repeated.changed is False


def test_rollback_restores_the_legitimate_migration_backup(tmp_path: Path) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    wrapper.chmod(0o751)
    launcher = _executable(tmp_path / "real-agy")
    migrated = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest(),
        launcher=launcher,
        apply=True,
    )
    assert migrated.backup_path is not None

    receipt = rollback_project_wrapper(
        wrapper=wrapper,
        backup_path=migrated.backup_path,
        expected_current_sha256=migrated.after_sha256,
    )

    assert receipt.changed is True
    assert wrapper.read_bytes() == LEGACY_PROJECT_WRAPPER
    if os.name != "nt":
        assert stat.S_IMODE(wrapper.stat().st_mode) == 0o751


@pytest.mark.parametrize("tamper", ["rename", "recognized-bytes"])
def test_rollback_rejects_renamed_or_tampered_backup_without_touching_wrapper(
    tmp_path: Path, tamper: str
) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    wrapper.chmod(0o751)
    launcher = _executable(tmp_path / "real-agy")
    migrated = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest(),
        launcher=launcher,
        apply=True,
    )
    assert migrated.backup_path is not None
    original_live_bytes = wrapper.read_bytes()
    original_live_mode = stat.S_IMODE(wrapper.stat().st_mode)
    backup = migrated.backup_path

    if tamper == "rename":
        renamed = backup.with_name(f"{backup.name}.renamed")
        backup.rename(renamed)
        backup = renamed
    else:
        backup.write_bytes(render_project_wrapper(launcher))

    with pytest.raises(AgyLauncherError, match="digest"):
        rollback_project_wrapper(
            wrapper=wrapper,
            backup_path=backup,
            expected_current_sha256=migrated.after_sha256,
        )

    assert wrapper.read_bytes() == original_live_bytes
    assert stat.S_IMODE(wrapper.stat().st_mode) == original_live_mode


def test_migration_rejects_symlinks_and_changed_or_unknown_wrappers(tmp_path: Path) -> None:
    launcher = _executable(tmp_path / "real-agy")
    target = tmp_path / "target"
    target.write_bytes(LEGACY_PROJECT_WRAPPER)
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.symlink_to(target)
    expected = hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest()

    with pytest.raises(AgyLauncherError, match="symlink"):
        migrate_project_wrapper(
            wrapper=wrapper,
            project_path=tmp_path,
            config_path=tmp_path / "workspaces.toml",
            expected_sha256=expected,
            launcher=launcher,
            apply=True,
        )

    wrapper.unlink()
    wrapper.write_text("#!/bin/sh\necho drift\n", encoding="utf-8")
    with pytest.raises(AgyLauncherError, match="expected sha256"):
        migrate_project_wrapper(
            wrapper=wrapper,
            project_path=tmp_path,
            config_path=tmp_path / "workspaces.toml",
            expected_sha256=expected,
            launcher=launcher,
            apply=True,
        )


def test_migration_rejects_symlink_parent_before_path_resolution(tmp_path: Path) -> None:
    real_project = tmp_path / "real-project"
    wrapper = real_project / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    project_link = tmp_path / "project-link"
    project_link.symlink_to(real_project, target_is_directory=True)
    config = tmp_path / "workspaces.toml"
    config.write_text("[control]\n", encoding="utf-8")
    launcher = _executable(tmp_path / "real-agy")

    with pytest.raises(AgyLauncherError, match="symlink"):
        migrate_project_wrapper(
            wrapper=project_link / ".agent-work" / "bin" / "agy-project",
            project_path=project_link,
            config_path=config,
            expected_sha256=hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest(),
            launcher=launcher,
            apply=True,
        )


def test_rollback_requires_current_hash_and_never_clobbers_late_edits(tmp_path: Path) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    launcher = _executable(tmp_path / "real-agy")
    before = hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest()
    applied = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=before,
        launcher=launcher,
        apply=True,
    )
    assert applied.backup_path is not None
    wrapper.write_text("late edit", encoding="utf-8")

    with pytest.raises(AgyLauncherError, match="expected sha256"):
        rollback_project_wrapper(
            wrapper=wrapper,
            backup_path=applied.backup_path,
            expected_current_sha256=applied.after_sha256,
        )
    assert wrapper.read_text(encoding="utf-8") == "late edit"


def test_rollback_rejects_a_symlinked_backup_input(tmp_path: Path) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    launcher = _executable(tmp_path / "real-agy")
    applied = migrate_project_wrapper(
        wrapper=wrapper,
        project_path=tmp_path,
        config_path=tmp_path / "workspaces.toml",
        expected_sha256=hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest(),
        launcher=launcher,
        apply=True,
    )
    assert applied.backup_path is not None
    linked_backup = wrapper.parent / "linked-backup"
    linked_backup.symlink_to(applied.backup_path)

    with pytest.raises(AgyLauncherError, match="symlink"):
        rollback_project_wrapper(
            wrapper=wrapper,
            backup_path=linked_backup,
            expected_current_sha256=applied.after_sha256,
        )


def test_migration_rejects_failed_readback_without_replacing_the_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    launcher = _executable(tmp_path / "real-agy")
    expected = hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest()
    monkeypatch.setattr(agy_launcher.os, "replace", lambda _source, _target: None)

    with pytest.raises(AgyLauncherError, match="read-back"):
        migrate_project_wrapper(
            wrapper=wrapper,
            project_path=tmp_path,
            config_path=tmp_path / "workspaces.toml",
            expected_sha256=expected,
            launcher=launcher,
            apply=True,
        )
    assert wrapper.read_bytes() == LEGACY_PROJECT_WRAPPER


def test_cli_migration_has_an_explicit_dry_run_and_apply_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    launcher = _executable(tmp_path / "real-agy")
    config = tmp_path / "config" / "workspaces.toml"
    config.parent.mkdir()
    config.write_text(
        "\n".join(
            [
                "[control]",
                'coordination_root = ".acp"',
                'runs_root = "runs"',
                'database = "runs/jobs.sqlite3"',
                'worktree_root = "worktrees"',
                'worktree_base = "."',
                'slot_root = "slots"',
                f'agy_command = "{launcher.as_posix()}"',
                "[routes.main]",
                'path = "."',
                'required_branch = "main"',
            ]
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest()
    config_digest = hashlib.sha256(config.read_bytes()).hexdigest()
    args = [
        "agy-wrapper",
        "migrate",
        "--config",
        str(config),
        "--project",
        str(tmp_path),
        "--expected-sha256",
        digest,
        "--expected-config-sha256",
        config_digest,
        "--expected-agy-command",
        str(launcher),
        "--launch-mode",
        "unmanaged",
    ]

    assert main(args) == 0
    preview = capsys.readouterr().out
    assert '"applied": false' in preview
    assert wrapper.read_bytes() == LEGACY_PROJECT_WRAPPER

    assert main([*args, "--apply"]) == 0
    assert '"applied": true' in capsys.readouterr().out


def test_cli_rejects_symlinked_project_before_loading_its_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    linked_project = tmp_path / "linked-project"
    linked_project.symlink_to(project, target_is_directory=True)

    assert (
        main(
            [
                "agy-wrapper",
                "migrate",
                "--project",
                str(linked_project),
                "--expected-sha256",
                "0" * 64,
                "--expected-config-sha256",
                "0" * 64,
                "--expected-agy-command",
                "/safe/agy",
                "--launch-mode",
                "unmanaged",
            ]
        )
        == 2
    )


def test_cli_rejects_symlinked_config_before_loading_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target_config = tmp_path / "target.toml"
    target_config.write_text("[control]\n", encoding="utf-8")
    linked_config = tmp_path / "linked.toml"
    linked_config.symlink_to(target_config)

    assert (
        main(
            [
                "agy-wrapper",
                "migrate",
                "--config",
                str(linked_config),
                "--project",
                str(project),
                "--expected-sha256",
                "0" * 64,
                "--expected-config-sha256",
                "0" * 64,
                "--expected-agy-command",
                "/safe/agy",
                "--launch-mode",
                "unmanaged",
            ]
        )
        == 2
    )


def test_migration_binding_rejects_config_drift_and_unconfigured_project(tmp_path: Path) -> None:
    config = tmp_path / "workspaces.toml"
    config.write_text("agy_command = '/safe/agy'\n", encoding="utf-8")
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    (tmp_path / "not-configured").mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()

    with pytest.raises(AgyLauncherError, match="configured ACP route"):
        validate_migration_binding(
            project_path=tmp_path / "not-configured",
            config_path=config,
            expected_config_sha256=digest,
            expected_agy_command="/safe/agy",
            configured_project_paths=(tmp_path / "configured",),
            actual_agy_command="/safe/agy",
        )

    config.write_text("agy_command = '/changed/agy'\n", encoding="utf-8")
    with pytest.raises(AgyLauncherError, match="config does not match"):
        validate_migration_binding(
            project_path=configured,
            config_path=config,
            expected_config_sha256=digest,
            expected_agy_command="/safe/agy",
            configured_project_paths=(configured,),
            actual_agy_command="/safe/agy",
        )


def test_public_config_service_and_pty_share_managed_launch_contract(tmp_path: Path) -> None:
    adapter = _executable(tmp_path / "adapter")
    config = tmp_path / "config" / "workspaces.toml"
    config.parent.mkdir()
    config.write_text(
        "\n".join(
            [
                "[control]",
                'coordination_root = ".agent-work"',
                'runs_root = "runs"',
                'database = "runs/jobs.sqlite3"',
                'worktree_root = "worktrees"',
                'worktree_base = "."',
                'slot_root = "slots"',
                'agy_command = "/must-not-be-selected/agy"',
                'agy_launch_mode = "managed"',
                f'agy_proxy_launcher = "{adapter.as_posix()}"',
                "[routes.main]",
                'path = "."',
                'required_branch = "main"',
            ]
        ),
        encoding="utf-8",
    )
    control = load_config(config)
    service = object.__new__(JobExecutionService)
    service.config = control
    service._claude_binding = lambda _job: (None, ())
    service._disabled_mcp_servers = lambda _job: ()
    service._effective_forbidden_markers = lambda _job, _state: ()
    job = SimpleNamespace(
        backend="agy",
        job_id="job-9",
        agy_model=None,
        workspace_path=tmp_path,
        result_path=tmp_path / "result.md",
        print_timeout="10s",
        timeout_sec=30,
        idle_timeout_sec=10,
        yolo=False,
        read_only=False,
        codex_tool_call_budget=None,
        workspace_access="native",
        task_id="task-9",
    )

    spec = service._agent_run_spec(
        job,
        SimpleNamespace(attempt_prompt="task", resume_thread_id=None),
        ModelProfile(model="unused", reasoning_effort="low"),
        tmp_path / "attempt-001.log",
    )

    assert spec.agy_launch is not None
    assert spec.agy_launch.receipt()["adapter_path"] == str(adapter)
    assert PtyAgyRunner._build_command(spec).count("--new-project") == 1
    assert PtyAgyRunner._build_command(spec)[0] == str(adapter)

    config.write_text(
        config.read_text(encoding="utf-8").replace("agy_proxy_launcher = ", "# "), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="requires agy_proxy_launcher"):
        load_config(config)


def test_managed_config_preserves_key_spelling_spacing_comments_and_crlf(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter"
    content = (
        b'["control"]\r\n'
        b'  "agy_launch_mode"  =  "unmanaged#literal"  # mode comment\r\n'
        b"\tagy_proxy_launcher\t=\t'old#adapter'\t# adapter comment\r\n"
    )

    updated = agy_launcher._managed_config_bytes(content, adapter)

    assert (
        updated
        == (
            '["control"]\r\n'
            '  "agy_launch_mode"  =  "managed"  # mode comment\r\n'
            f"\tagy_proxy_launcher\t=\t{agy_launcher._toml_string(str(adapter))}\t# adapter comment\r\n"
        ).encode()
    )


def test_cli_configure_migrate_generate_and_restore_guarded_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    wrapper = project / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    wrapper.chmod(0o751)
    fresh = tmp_path / "fresh-project"
    fresh.mkdir()
    captured = tmp_path / "adapter-argv.txt"
    adapter = _executable(
        tmp_path / "adapter",
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{captured.as_posix()}"\n',
    )
    config = tmp_path / "config" / "workspaces.toml"
    config.parent.mkdir()
    config.write_text(
        "\n".join(
            [
                '["control"]',
                'coordination_root = ".acp"',
                'runs_root = "runs"',
                'database = "runs/jobs.sqlite3"',
                'worktree_root = "worktrees"',
                f'worktree_base = "{project.as_posix()}"',
                'slot_root = "slots"',
                "# preserve this comment and quoted table spelling",
                "[routes.main]",
                f'path = "{project.as_posix()}"',
                'required_branch = "main"',
                "[routes.fresh]",
                f'path = "{fresh.as_posix()}"',
                'required_branch = "main"',
            ]
        ),
        encoding="utf-8",
    )
    original = config.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    configure = [
        "agy-wrapper",
        "configure",
        "--config",
        str(config),
        "--project",
        str(project),
        "--expected-config-sha256",
        original_hash,
        "--expected-agy-command",
        "agy",
        "--adapter",
        str(adapter),
    ]

    assert main(configure) == 0
    configure_preview = json.loads(capsys.readouterr().out)
    assert configure_preview["applied"] is False
    assert config.read_bytes() == original

    assert main([*configure, "--apply"]) == 0
    configured = json.loads(capsys.readouterr().out)
    configured_hash = configured["after_sha256"]
    assert configured["target_mode"] == "managed"
    assert load_config(config).agy_command == "agy"
    assert load_config(config).agy_proxy_launcher == adapter

    migrate = [
        "agy-wrapper",
        "migrate",
        "--config",
        str(config),
        "--project",
        str(project),
        "--expected-sha256",
        hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest(),
        "--expected-config-sha256",
        configured_hash,
        "--expected-agy-command",
        "agy",
        "--launch-mode",
        "managed",
        "--adapter",
        str(adapter),
    ]
    assert main(migrate) == 0
    capsys.readouterr()
    assert main([*migrate, "--apply"]) == 0
    migrated = json.loads(capsys.readouterr().out)
    _run_wrapper(wrapper, ["safe argument"])
    assert captured.read_text(encoding="utf-8").splitlines() == ["--new-project", "safe argument"]

    generated_wrapper = fresh / ".agent-work" / "bin" / "agy-project"
    generate = [
        "agy-wrapper",
        "generate",
        "--config",
        str(config),
        "--project",
        str(fresh),
        "--expected-config-sha256",
        configured_hash,
        "--expected-agy-command",
        "agy",
    ]
    assert main(generate) == 0
    assert not generated_wrapper.exists()
    capsys.readouterr()
    assert main([*generate, "--apply"]) == 0
    generated = json.loads(capsys.readouterr().out)
    assert generated["applied"] is True
    assert main([*generate, "--apply"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "agy-wrapper",
                "rollback",
                "--config",
                str(config),
                "--project",
                str(project),
                "--backup",
                migrated["backup_path"],
                "--expected-current-sha256",
                migrated["after_sha256"],
            ]
        )
        == 0
    )
    assert wrapper.read_bytes() == LEGACY_PROJECT_WRAPPER
    adapter.unlink()
    assert (
        main(
            [
                "agy-wrapper",
                "restore-config",
                "--config",
                str(config),
                "--project",
                str(project),
                "--backup",
                configured["backup_path"],
                "--expected-current-sha256",
                configured_hash,
            ]
        )
        == 0
    )
    assert config.read_bytes() == original


def test_cli_restore_config_recovers_without_loading_an_unavailable_adapter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config" / "workspaces.toml"
    config.parent.mkdir()
    original = (
        "[control]\n"
        'coordination_root = ".acp"\n'
        'runs_root = "runs"\n'
        'database = "runs/jobs.sqlite3"\n'
        'worktree_root = "worktrees"\n'
        f'worktree_base = "{project.as_posix()}"\n'
        'slot_root = "slots"\n'
        "[routes.main]\n"
        f'path = "{project.as_posix()}"\n'
        'required_branch = "main"\n'
    ).encode()
    config.write_bytes(original)
    backup = config.with_name(
        f"{config.name}.acp-agy-v2-{hashlib.sha256(original).hexdigest()[:12]}.bak"
    )
    backup.write_bytes(original)
    missing_adapter = tmp_path / "missing-adapter"
    missing_adapter.symlink_to(tmp_path / "replacement-adapter")
    configured = original.replace(
        b'slot_root = "slots"\n',
        b'slot_root = "slots"\nagy_launch_mode = "managed"\n'
        + f'agy_proxy_launcher = "{missing_adapter.as_posix()}"\n'.encode(),
    )
    config.write_bytes(configured)
    configured_hash = hashlib.sha256(configured).hexdigest()
    restore = [
        "agy-wrapper",
        "restore-config",
        "--config",
        str(config),
        "--project",
        str(project),
        "--backup",
        str(backup),
        "--expected-current-sha256",
        configured_hash,
    ]

    assert main([*restore[:-1], "0" * 64]) == 2
    assert config.read_bytes() == configured
    backup_link = backup.parent / "linked-backup"
    backup_link.symlink_to(backup)
    linked_backup_restore = [*restore]
    linked_backup_restore[linked_backup_restore.index("--backup") + 1] = str(backup_link)
    assert main(linked_backup_restore) == 2
    assert config.read_bytes() == configured
    assert main(restore) == 0
    assert config.read_bytes() == original
    capsys.readouterr()

    config_link = config.parent / "linked-workspaces.toml"
    config_link.symlink_to(config)
    assert main([*restore[:3], str(config_link), *restore[4:]]) == 2


@pytest.mark.parametrize("tamper", ["rename", "bytes"])
def test_restore_managed_config_binds_backup_name_to_content_digest(
    tmp_path: Path, tamper: str
) -> None:
    config = tmp_path / "workspaces.toml"
    project = tmp_path / "project"
    project.mkdir()
    original = b"[control]\ncoordination_root = '.acp'\n"
    config.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    backup = config.with_name(f"{config.name}.acp-agy-v2-{digest[:12]}.bak")
    backup.write_bytes(original)
    configured = b"[control]\ncoordination_root = 'changed'\n"
    config.write_bytes(configured)
    if tamper == "rename":
        renamed = config.with_name(f"{config.name}.acp-agy-v2-backup.bak")
        backup.rename(renamed)
        backup = renamed
    else:
        backup.write_bytes(b"[control]\ncoordination_root = 'tampered'\n")

    with pytest.raises(AgyLauncherError, match="digest"):
        restore_managed_config(
            config_path=config,
            project_path=project,
            backup_path=backup,
            expected_current_sha256=hashlib.sha256(configured).hexdigest(),
        )
    assert config.read_bytes() == configured


def test_managed_config_rejects_adapter_and_parent_symlinks(tmp_path: Path) -> None:
    target = _executable(tmp_path / "target-adapter")
    direct_link = tmp_path / "adapter-link"
    direct_link.symlink_to(target)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(tmp_path, target_is_directory=True)
    for adapter in (direct_link, linked_parent / "target-adapter"):
        config = tmp_path / f"{adapter.name}.toml"
        config.write_text(
            "\n".join(
                [
                    "[control]",
                    'coordination_root = ".agent-work"',
                    'runs_root = "runs"',
                    'database = "runs/jobs.sqlite3"',
                    'worktree_root = "worktrees"',
                    'worktree_base = "."',
                    'slot_root = "slots"',
                    'agy_launch_mode = "managed"',
                    f'agy_proxy_launcher = "{adapter.as_posix()}"',
                    "[routes.main]",
                    'path = "."',
                    'required_branch = "main"',
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must not traverse a symlink"):
            load_config(config)


def test_migration_rechecks_config_after_backup_before_wrapper_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / ".agent-work" / "bin" / "agy-project"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(LEGACY_PROJECT_WRAPPER)
    launcher = _executable(tmp_path / "adapter")
    config = tmp_path / "workspaces.toml"
    config.write_text("[control]\n", encoding="utf-8")
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    original_backup = agy_launcher._write_backup

    def backup_then_edit(*args, **kwargs):
        backup = original_backup(*args, **kwargs)
        config.write_text("[control]\nlate_edit = true\n", encoding="utf-8")
        return backup

    monkeypatch.setattr(agy_launcher, "_write_backup", backup_then_edit)
    with pytest.raises(AgyLauncherError, match="config does not match"):
        migrate_project_wrapper(
            wrapper=wrapper,
            project_path=tmp_path,
            config_path=config,
            expected_sha256=hashlib.sha256(LEGACY_PROJECT_WRAPPER).hexdigest(),
            expected_config_sha256=config_hash,
            launcher=launcher,
            apply=True,
        )
    assert wrapper.read_bytes() == LEGACY_PROJECT_WRAPPER
