from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.runner import AgyLaunchSpec

WRAPPER_VERSION = "v2"
WRAPPER_RELATIVE_PATH = Path(".agent-work/bin/agy-project")
LEGACY_PROJECT_WRAPPER = (
    b'#!/usr/bin/env bash\nset -euo pipefail\nexec /home/nikit/.local/bin/agy --new-project "$@"\n'
)
_OWNED_V1_RE = re.compile(
    rb"\A#!/bin/sh\n# agent-control-plane agy-project-wrapper: v1\n"
    rb'exec "([^"\n]+)" --new-project "\$@"\n\Z'
)
_OWNED_V2_RE = re.compile(
    rb"\A#!/bin/sh\n# agent-control-plane agy-project-wrapper: v2\n"
    rb"exec ('(?:[^']|'\"'\"')+') --new-project \"\$@\"\n\Z"
)


class AgyLauncherError(ValueError):
    """A configured AGY launcher or managed project wrapper is unsafe to use."""


@dataclass(frozen=True)
class AgyWrapperReceipt:
    wrapper_path: Path
    project_path: Path
    config_path: Path
    before_sha256: str
    after_sha256: str
    launcher_path: Path | None
    backup_path: Path | None
    changed: bool
    applied: bool

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "version": "agy-wrapper-receipt-v1",
            "wrapper_path": str(self.wrapper_path),
            "project_path": str(self.project_path),
            "config_path": str(self.config_path),
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "launcher_path": str(self.launcher_path) if self.launcher_path else None,
            "launcher_sha256": _file_sha256(self.launcher_path) if self.launcher_path else None,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "changed": self.changed,
            "applied": self.applied,
            "recovery": (
                "Run agy-wrapper rollback with this backup and after_sha256 only while the "
                "wrapper still has that hash. Per-file replacement is atomic where the "
                "filesystem supports rename; a multi-project migration is not transactional."
            ),
        }


@dataclass(frozen=True)
class AgyConfigReceipt:
    config_path: Path
    project_path: Path
    before_sha256: str
    after_sha256: str
    adapter_path: Path | None
    backup_path: Path | None
    target_mode: str
    changed: bool
    applied: bool

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "version": "agy-config-receipt-v1",
            "config_path": str(self.config_path),
            "project_path": str(self.project_path),
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "adapter_path": str(self.adapter_path) if self.adapter_path else None,
            "adapter_sha256": _file_sha256(self.adapter_path) if self.adapter_path else None,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "target_mode": self.target_mode,
            "changed": self.changed,
            "applied": self.applied,
            "recovery": (
                "Run agy-wrapper restore-config with this backup and after_sha256 only while "
                "the config still has that hash. Config and wrapper changes are separate "
                "single-file transactions."
            ),
        }


def build_agy_launch(
    *,
    agy_command: str,
    mode: str,
    adapter: Path | None,
    job_id: str,
    attempt_ref: str,
) -> AgyLaunchSpec:
    """Construct the sole typed launch contract used by AGY PTY spawning.

    Unmanaged mode preserves an explicitly configured command for compatibility and does not
    claim adapter mediation. Managed mode admits only a validated explicit adapter; it never
    falls back to the real AGY command. ``launch_id`` is local launch bookkeeping, not a
    verified conversation or CONNECT identity.
    """
    if mode == "unmanaged":
        return AgyLaunchSpec(
            executable=agy_command,
            managed=False,
            add_new_project=False,
            adapter_path=None,
            adapter_sha256=None,
            launch_id=f"agy:{job_id}:{attempt_ref}",
            job_id=job_id,
            attempt_ref=attempt_ref,
        )
    if mode != "managed":
        raise AgyLauncherError(f"unknown AGY launch mode: {mode}")
    if adapter is None:
        raise AgyLauncherError("managed AGY launch requires an explicit adapter")
    selected = _validated_launcher(adapter, "managed AGY adapter")
    return AgyLaunchSpec(
        executable=str(selected),
        managed=True,
        add_new_project=True,
        adapter_path=selected,
        adapter_sha256=_file_sha256(selected),
        launch_id=f"agy:{job_id}:{attempt_ref}",
        job_id=job_id,
        attempt_ref=attempt_ref,
    )


def resolve_agy_launcher(proxy_launcher: Path | None, fallback_launcher: Path | str) -> Path:
    """Resolve an explicitly selected launcher without consulting PATH."""
    if proxy_launcher is not None:
        return _validated_launcher(proxy_launcher, "proxy launcher")
    return _validated_launcher(Path(fallback_launcher), "AGY fallback launcher")


def render_project_wrapper(launcher: Path | str) -> bytes:
    resolved = _validated_launcher(Path(launcher), "AGY launcher")
    return (
        "#!/bin/sh\n"
        f"# agent-control-plane agy-project-wrapper: {WRAPPER_VERSION}\n"
        f'exec {_shell_quote(str(resolved))} --new-project "$@"\n'
    ).encode()


def validate_migration_binding(
    *,
    project_path: Path,
    config_path: Path,
    expected_config_sha256: str,
    expected_agy_command: str,
    configured_project_paths: tuple[Path, ...],
    actual_agy_command: str,
) -> tuple[Path, Path]:
    """Bind a migration to an unchanged ACP config and a declared route/worktree base."""
    project = _canonical_absolute_path(project_path, "project path")
    config = _canonical_absolute_path(config_path, "config path")
    allowed = {
        _canonical_absolute_path(path, "configured project path")
        for path in configured_project_paths
    }
    if project not in allowed:
        raise AgyLauncherError("project path is not a configured ACP route or worktree_base")
    config_bytes, _ = _read_regular_file(config, "config")
    _require_hash(expected_config_sha256, _sha256(config_bytes), "config")
    if not _commands_equivalent(actual_agy_command, expected_agy_command):
        raise AgyLauncherError("configured agy_command differs from the expected value")
    return project, config


def _commands_equivalent(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    if actual.replace("\\", "/") == expected.replace("\\", "/"):
        return True
    try:
        return Path(actual) == Path(expected)
    except (ValueError, TypeError, OSError):
        return False


def validate_configured_project(
    project_path: Path, configured_project_paths: tuple[Path, ...]
) -> Path:
    """Require a raw project path to be one of the routes served by this ACP config."""
    project = _canonical_absolute_path(project_path, "project path")
    allowed = {
        _canonical_absolute_path(path, "configured project path")
        for path in configured_project_paths
    }
    if project not in allowed:
        raise AgyLauncherError("project path is not a configured ACP route or worktree_base")
    return project


def validate_recovery_binding(
    *, project_path: Path, config_path: Path, expected_current_sha256: str
) -> tuple[Path, Path]:
    """Bind config recovery to a managed config without admitting its live adapter."""
    project = _canonical_absolute_path(project_path, "project path")
    config = _canonical_absolute_path(config_path, "config path")
    current, _ = _read_regular_file(config, "config")
    _require_hash(expected_current_sha256, _sha256(current), "config")
    try:
        parsed = tomllib.loads(current.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AgyLauncherError("config is not valid TOML") from exc
    control = parsed.get("control")
    routes = parsed.get("routes")
    if not isinstance(control, dict) or not isinstance(routes, dict):
        raise AgyLauncherError("config does not declare [control] and [routes]")
    if control.get("agy_launch_mode") != "managed":
        raise AgyLauncherError("config recovery requires managed AGY launch mode")
    if not isinstance(control.get("agy_proxy_launcher"), str) or not control["agy_proxy_launcher"]:
        raise AgyLauncherError("managed config recovery requires agy_proxy_launcher")
    allowed: set[Path] = set()
    for name, route in routes.items():
        if not isinstance(name, str) or not isinstance(route, dict):
            raise AgyLauncherError("config routes must be TOML tables")
        route_path = _recovery_route_path(route.get("path"), config, f"routes.{name}.path")
        allowed.add(route_path)
        worktree_base = route.get("worktree_base", route.get("path"))
        allowed.add(_recovery_route_path(worktree_base, config, f"routes.{name}.worktree_base"))
    if project not in allowed:
        raise AgyLauncherError("project path is not a configured ACP route or worktree_base")
    return project, config


def _recovery_route_path(value: object, config: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgyLauncherError(f"{label} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config.parent / path
    return _canonical_absolute_path(path, label)


def validate_migration_path(path: Path, label: str) -> Path:
    """Reject relative paths and symlink traversal before a migration uses a path."""
    return _canonical_absolute_path(path, label)


def migrate_project_wrapper(
    *,
    wrapper: Path,
    project_path: Path,
    config_path: Path,
    expected_sha256: str,
    expected_config_sha256: str | None = None,
    launcher: Path | str,
    apply: bool,
) -> AgyWrapperReceipt:
    """Preview or replace one exact recognized project wrapper.

    This offers a single-file atomic replacement only. Callers must retain each receipt when
    migrating more than one project because an honest cross-project transaction is unavailable.
    """
    wrapper, project_path, config_path = _migration_paths(wrapper, project_path, config_path)
    with _config_lock(config_path):
        _require_config_hash(config_path, expected_config_sha256)
        old_bytes, old_mode = _read_regular_file(wrapper, "project wrapper")
        before = _sha256(old_bytes)
        _require_hash(expected_sha256, before, "project wrapper")
        if not _is_recognized_wrapper(old_bytes):
            raise AgyLauncherError("project wrapper is not an exact legacy or ACP-owned wrapper")

        resolved_launcher = _validated_launcher(Path(launcher), "AGY launcher")
        if resolved_launcher == wrapper:
            raise AgyLauncherError("AGY launcher must not be the project wrapper")
        replacement = render_project_wrapper(resolved_launcher)
        after = _sha256(replacement)
        _require_config_hash(config_path, expected_config_sha256)
        _validated_launcher(resolved_launcher, "AGY launcher")
        if old_bytes == replacement:
            return AgyWrapperReceipt(
                wrapper,
                project_path,
                config_path,
                before,
                after,
                resolved_launcher,
                None,
                False,
                apply,
            )
        if not apply:
            return AgyWrapperReceipt(
                wrapper,
                project_path,
                config_path,
                before,
                after,
                resolved_launcher,
                _backup_path(wrapper, before),
                False,
                False,
            )

        backup = _write_backup(wrapper, old_bytes, old_mode, before)
        _replace_if_unchanged(
            wrapper,
            replacement,
            old_mode,
            before,
            pre_replace=lambda: _recheck_config_and_launcher(
                config_path, expected_config_sha256, resolved_launcher
            ),
        )
        return AgyWrapperReceipt(
            wrapper, project_path, config_path, before, after, resolved_launcher, backup, True, True
        )


def configure_managed_project(
    *,
    config_path: Path,
    project_path: Path,
    expected_config_sha256: str,
    adapter: Path,
    apply: bool,
) -> AgyConfigReceipt:
    """Preview or narrowly configure managed AGY launch without rewriting unrelated TOML."""
    config = _canonical_absolute_path(config_path, "config")
    project = _canonical_absolute_path(project_path, "project path")
    selected = _validated_launcher(adapter, "managed AGY adapter")
    with _config_lock(config):
        before_bytes, before_mode = _read_regular_file(config, "config")
        before = _sha256(before_bytes)
        _require_hash(expected_config_sha256, before, "config")
        replacement = _managed_config_bytes(before_bytes, selected)
        after = _sha256(replacement)
        _require_config_hash(config, expected_config_sha256)
        _validated_launcher(selected, "managed AGY adapter")
        if replacement == before_bytes:
            return AgyConfigReceipt(
                config, project, before, after, selected, None, "managed", False, apply
            )
        if not apply:
            return AgyConfigReceipt(
                config,
                project,
                before,
                after,
                selected,
                _config_backup_path(config, before),
                "managed",
                False,
                False,
            )
        backup = _write_config_backup(config, before_bytes, before_mode, before)
        _require_config_hash(config, expected_config_sha256)
        _validated_launcher(selected, "managed AGY adapter")
        _replace_config_if_unchanged(config, replacement, before_mode, before)
        return AgyConfigReceipt(
            config, project, before, after, selected, backup, "managed", True, True
        )


def restore_managed_config(
    *, config_path: Path, project_path: Path, backup_path: Path, expected_current_sha256: str
) -> AgyConfigReceipt:
    """Restore a config backup only when its current bytes still match the supplied hash."""
    config = _canonical_absolute_path(config_path, "config")
    project = _canonical_absolute_path(project_path, "project path")
    backup = _canonical_absolute_path(backup_path, "config backup")
    if backup.parent != config.parent or not backup.name.startswith(f"{config.name}.acp-agy-v"):
        raise AgyLauncherError("config backup is not an ACP backup beside the config")
    with _config_lock(config):
        current, current_mode = _read_regular_file(config, "config")
        before = _sha256(current)
        _require_hash(expected_current_sha256, before, "config")
        original, original_mode = _read_regular_file(backup, "config backup")
        _validate_toml(original, "config backup")
        after = _sha256(original)
        if backup != _config_backup_path(config, after):
            raise AgyLauncherError("config backup filename does not match its content digest")
        _replace_config_if_unchanged(
            config, original, current_mode, before, replacement_mode=original_mode
        )
    return AgyConfigReceipt(config, project, before, after, None, backup, "unmanaged", True, True)


def generate_project_wrapper(
    *,
    wrapper: Path,
    project_path: Path,
    config_path: Path,
    expected_config_sha256: str,
    launcher: Path | str,
    apply: bool,
) -> AgyWrapperReceipt:
    """Generate only a missing wrapper or prove an exact owned wrapper is already current."""
    project = _canonical_absolute_path(project_path, "project path")
    config = _canonical_absolute_path(config_path, "config")
    expected_wrapper = project / WRAPPER_RELATIVE_PATH
    raw_wrapper = Path(wrapper).expanduser()
    if raw_wrapper != expected_wrapper:
        raise AgyLauncherError(f"project wrapper must be exactly {expected_wrapper}")
    _validate_optional_wrapper_parent(project)
    resolved_launcher = _validated_launcher(Path(launcher), "AGY launcher")
    if resolved_launcher == expected_wrapper:
        raise AgyLauncherError("AGY launcher must not be the project wrapper")
    replacement = render_project_wrapper(resolved_launcher)
    after = _sha256(replacement)
    with _config_lock(config):
        _require_config_hash(config, expected_config_sha256)
        _validated_launcher(resolved_launcher, "AGY launcher")
        if expected_wrapper.exists():
            old_bytes, _old_mode = _read_regular_file(expected_wrapper, "project wrapper")
            before = _sha256(old_bytes)
            if old_bytes != replacement or not _is_recognized_wrapper(old_bytes):
                raise AgyLauncherError("project wrapper exists but is not the exact owned content")
            _require_config_hash(config, expected_config_sha256)
            return AgyWrapperReceipt(
                expected_wrapper,
                project,
                config,
                before,
                after,
                resolved_launcher,
                None,
                False,
                apply,
            )
        before = ""
        if not apply:
            return AgyWrapperReceipt(
                expected_wrapper,
                project,
                config,
                before,
                after,
                resolved_launcher,
                None,
                False,
                False,
            )
        _require_config_hash(config, expected_config_sha256)
        _validated_launcher(resolved_launcher, "AGY launcher")
        _ensure_wrapper_parent(project)
        _require_config_hash(config, expected_config_sha256)
        _validated_launcher(resolved_launcher, "AGY launcher")
        _create_wrapper(expected_wrapper, replacement)
    return AgyWrapperReceipt(
        expected_wrapper, project, config, before, after, resolved_launcher, None, True, True
    )


def rollback_project_wrapper(
    *, wrapper: Path, backup_path: Path, expected_current_sha256: str
) -> AgyWrapperReceipt:
    """Restore a verified migration backup without overwriting late edits or symlinks."""
    wrapper = _canonical_absolute_path(wrapper, "project wrapper")
    backup_path = _canonical_absolute_path(backup_path, "project wrapper backup")
    if backup_path.parent != wrapper.parent or not backup_path.name.startswith(
        f"{wrapper.name}.acp-agy-v"
    ):
        raise AgyLauncherError("project wrapper backup is not an ACP backup beside the wrapper")
    current, current_mode = _read_regular_file(wrapper, "project wrapper")
    before = _sha256(current)
    _require_hash(expected_current_sha256, before, "project wrapper")
    backup, backup_mode = _read_regular_file(backup_path, "project wrapper backup")
    if not _is_recognized_wrapper(backup):
        raise AgyLauncherError("project wrapper backup is not an exact legacy or ACP-owned wrapper")
    after = _sha256(backup)
    if backup_path != _backup_path(wrapper, after):
        raise AgyLauncherError("project wrapper backup filename does not match its content digest")
    _replace_if_unchanged(wrapper, backup, current_mode, before, replacement_mode=backup_mode)
    return AgyWrapperReceipt(
        wrapper,
        wrapper.parents[2],
        Path("<recovery>"),
        before,
        after,
        None,
        backup_path,
        True,
        True,
    )


def _managed_config_bytes(content: bytes, adapter: Path) -> bytes:
    """Change only the two managed-launch keys in a single unambiguous control table."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgyLauncherError("config must be UTF-8 to preserve its existing TOML text") from exc
    header = re.compile(r'(?m)^\[(?:control|"control")\][^\r\n]*(?:\r?\n|$)')
    matches = tuple(header.finditer(text))
    if len(matches) != 1:
        raise AgyLauncherError("config must contain exactly one [control] table")
    start = matches[0].end()
    next_table = re.search(r"(?m)^\[", text[start:])
    end = start + next_table.start() if next_table else len(text)
    control = text[start:end]
    updated = _set_control_key(control, "agy_launch_mode", "managed")
    updated = _set_control_key(updated, "agy_proxy_launcher", str(adapter))
    result = text[:start] + updated + text[end:]
    _validate_toml(result.encode("utf-8"), "updated config")
    return result.encode("utf-8")


def _set_control_key(control: str, key: str, value: str) -> str:
    pattern = re.compile(
        rf'(?m)^(?P<prefix>[ \t]*(?:{re.escape(key)}|"{re.escape(key)}")[ \t]*=)'
        r"(?P<value_and_suffix>[^\r\n]*)(?P<ending>\r?\n|$)"
    )
    matches = tuple(pattern.finditer(control))
    if len(matches) > 1:
        raise AgyLauncherError(f"[control].{key} is ambiguous")
    rendered = f"{key} = {_toml_string(value)}"
    if matches:
        match = matches[0]
        old_value, suffix = _split_toml_comment(match.group("value_and_suffix"))
        leading = old_value[: len(old_value) - len(old_value.lstrip(" \t"))]
        trailing = old_value[len(old_value.rstrip(" \t")) :]
        return (
            control[: match.start()]
            + match.group("prefix")
            + leading
            + _toml_string(value)
            + trailing
            + suffix
            + match.group("ending")
            + control[match.end() :]
        )
    newline = "\r\n" if "\r\n" in control else "\n"
    prefix = "" if not control or control.endswith(("\n", "\r")) else newline
    return control + prefix + rendered + newline


def _split_toml_comment(value_and_suffix: str) -> tuple[str, str]:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value_and_suffix):
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#":
            return value_and_suffix[:index], value_and_suffix[index:]
    return value_and_suffix, ""


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate_toml(content: bytes, label: str) -> None:
    try:
        parsed = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AgyLauncherError(f"{label} is not valid TOML") from exc
    control = parsed.get("control")
    if not isinstance(control, dict):
        raise AgyLauncherError(f"{label} does not contain a [control] table")
    if control.get("agy_launch_mode") == "managed" and not control.get("agy_proxy_launcher"):
        raise AgyLauncherError(f"{label} managed mode requires agy_proxy_launcher")


@contextmanager
def _config_lock(config_path: Path) -> Iterator[None]:
    """Use a short-lived adjacent lock to serialize config and wrapper publication."""
    lock = config_path.with_name(f".{config_path.name}.acp-agy.lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AgyLauncherError(f"config operation is already in progress: {config_path}") from exc
    except OSError as exc:
        raise AgyLauncherError(f"could not acquire config operation lock: {config_path}") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        with suppress(OSError):
            lock.unlink()


def _require_config_hash(config_path: Path, expected: str | None) -> None:
    if expected is None:
        return
    content, _ = _read_regular_file(config_path, "config")
    _require_hash(expected, _sha256(content), "config")


def _recheck_config_and_launcher(config: Path, expected: str | None, launcher: Path) -> None:
    _require_config_hash(config, expected)
    _validated_launcher(launcher, "AGY launcher")


def _config_backup_path(config: Path, digest: str) -> Path:
    return config.with_name(f"{config.name}.acp-agy-{WRAPPER_VERSION}-{digest[:12]}.bak")


def _write_config_backup(config: Path, content: bytes, mode: int, digest: str) -> Path:
    current, current_mode = _read_regular_file(config, "config")
    if current != content or current_mode != mode:
        raise AgyLauncherError("config changed before backup; refusing to replace it")
    backup = _config_backup_path(config, digest)
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        existing, existing_mode = _read_regular_file(backup, "config backup")
        if existing != content or existing_mode != mode:
            raise AgyLauncherError(
                f"backup path already differs from original config: {backup}"
            ) from None
        return backup
    except OSError as exc:
        raise AgyLauncherError(f"could not create config backup: {backup}") from exc
    try:
        os.chmod(backup, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        written, written_mode = _read_regular_file(backup, "config backup")
        if written != content or written_mode != mode:
            raise AgyLauncherError("config backup failed read-back verification")
        _fsync_directory(config.parent)
    except OSError as exc:
        raise AgyLauncherError(f"could not write config backup: {backup}") from exc
    return backup


def _replace_config_if_unchanged(
    config: Path,
    replacement: bytes,
    expected_mode: int,
    expected_before: str,
    *,
    replacement_mode: int | None = None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{config.name}.", dir=config.parent)
    temporary = Path(temporary_name)
    mode = replacement_mode if replacement_mode is not None else expected_mode
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        current, current_mode = _read_regular_file(config, "config")
        if _sha256(current) != expected_before or current_mode != expected_mode:
            raise AgyLauncherError("config changed after validation; refusing to clobber it")
        _canonical_absolute_path(config, "config")
        os.replace(temporary, config)
        written, written_mode = _read_regular_file(config, "replaced config")
        if written != replacement or written_mode != mode:
            raise AgyLauncherError("config replacement failed read-back verification")
        _fsync_directory(config.parent)
    except OSError as exc:
        raise AgyLauncherError(f"could not atomically replace config: {config}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _create_wrapper(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    except FileExistsError as exc:
        raise AgyLauncherError("project wrapper appeared during generation") from exc
    except OSError as exc:
        raise AgyLauncherError(f"could not create project wrapper: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        written, mode = _read_regular_file(path, "generated project wrapper")
        if written != content or (os.name != "nt" and not (stat.S_IXUSR & mode)):
            raise AgyLauncherError("generated project wrapper failed read-back verification")
        _fsync_directory(path.parent)
    except OSError as exc:
        raise AgyLauncherError(f"could not write project wrapper: {path}") from exc


def _ensure_wrapper_parent(project: Path) -> Path:
    parent = project
    for name in WRAPPER_RELATIVE_PATH.parent.parts:
        parent = parent / name
        try:
            parent.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise AgyLauncherError(f"could not create project wrapper parent: {parent}") from exc
        _canonical_absolute_path(parent, "project wrapper parent")
    return parent


def _validate_optional_wrapper_parent(project: Path) -> None:
    parent = project
    for name in WRAPPER_RELATIVE_PATH.parent.parts:
        parent = parent / name
        try:
            status = parent.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AgyLauncherError(f"project wrapper parent is unavailable: {parent}") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise AgyLauncherError(f"project wrapper parent is unsafe: {parent}")


def _migration_paths(
    wrapper: Path, project_path: Path, config_path: Path
) -> tuple[Path, Path, Path]:
    project = _canonical_absolute_path(project_path, "project path")
    config = _canonical_absolute_path(config_path, "config path", require_exists=False)
    expected_wrapper = project / WRAPPER_RELATIVE_PATH
    resolved_wrapper = _canonical_absolute_path(wrapper, "project wrapper")
    if resolved_wrapper != expected_wrapper:
        raise AgyLauncherError(f"project wrapper must be exactly {expected_wrapper}")
    return resolved_wrapper, project, config


def _canonical_absolute_path(path: Path, label: str, *, require_exists: bool = True) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise AgyLauncherError(f"{label} must be an absolute path")
    if ".." in expanded.parts:
        raise AgyLauncherError(f"{label} must not contain parent traversal")
    chain = tuple(reversed((expanded, *expanded.parents)))
    for index, candidate in enumerate(chain):
        try:
            status = candidate.lstat()
        except OSError as exc:
            if not require_exists and index == len(chain) - 1:
                continue
            raise AgyLauncherError(f"{label} parent is unavailable: {candidate}") from exc
        if stat.S_ISLNK(status.st_mode):
            raise AgyLauncherError(f"{label} must not traverse a symlink: {candidate}")
    return expanded.resolve(strict=False)


def _validated_launcher(path: Path, label: str) -> Path:
    expanded = _canonical_absolute_path(path, label)
    try:
        status = expanded.lstat()
    except OSError as exc:
        raise AgyLauncherError(f"{label} is unavailable: {expanded}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise AgyLauncherError(f"{label} must be a regular non-symlink file: {expanded}")
    if not os.access(expanded, os.X_OK):
        raise AgyLauncherError(f"{label} is not executable: {expanded}")
    return expanded


def _is_recognized_wrapper(content: bytes) -> bool:
    if content == LEGACY_PROJECT_WRAPPER:
        return True
    match = _OWNED_V1_RE.fullmatch(content)
    if match is not None:
        return Path(match.group(1).decode("utf-8")).is_absolute()
    match = _OWNED_V2_RE.fullmatch(content)
    if match is None:
        return False
    decoded = match.group(1)[1:-1].replace(b"'\"'\"'", b"'")
    return Path(decoded.decode("utf-8")).is_absolute()


def _shell_quote(value: str) -> str:
    if "\x00" in value or "\n" in value:
        raise AgyLauncherError(
            "AGY launcher path cannot be represented safely in a project wrapper"
        )
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _read_regular_file(path: Path, label: str) -> tuple[bytes, int]:
    _canonical_absolute_path(path, label)
    try:
        status = path.lstat()
    except OSError as exc:
        raise AgyLauncherError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(status.st_mode):
        raise AgyLauncherError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(status.st_mode):
        raise AgyLauncherError(f"{label} must be a regular file: {path}")
    try:
        return path.read_bytes(), stat.S_IMODE(status.st_mode)
    except OSError as exc:
        raise AgyLauncherError(f"could not read {label}: {path}") from exc


def _backup_path(wrapper: Path, digest: str) -> Path:
    return wrapper.with_name(f"{wrapper.name}.acp-agy-{WRAPPER_VERSION}-{digest[:12]}.bak")


def _write_backup(wrapper: Path, content: bytes, mode: int, digest: str) -> Path:
    current, current_mode = _read_regular_file(wrapper, "project wrapper")
    if current != content or current_mode != mode:
        raise AgyLauncherError("project wrapper changed before backup; refusing to replace it")
    backup = _backup_path(wrapper, digest)
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        existing, existing_mode = _read_regular_file(backup, "project wrapper backup")
        if existing != content or existing_mode != mode:
            raise AgyLauncherError(
                f"backup path already differs from original wrapper: {backup}"
            ) from None
        return backup
    except OSError as exc:
        raise AgyLauncherError(f"could not create project wrapper backup: {backup}") from exc
    try:
        os.chmod(backup, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        written, written_mode = _read_regular_file(backup, "project wrapper backup")
        if written != content or written_mode != mode:
            raise AgyLauncherError("project wrapper backup failed read-back verification")
        _fsync_directory(wrapper.parent)
    except OSError as exc:
        raise AgyLauncherError(f"could not write project wrapper backup: {backup}") from exc
    return backup


def _replace_if_unchanged(
    path: Path,
    replacement: bytes,
    expected_mode: int,
    expected_before: str,
    *,
    replacement_mode: int | None = None,
    pre_replace: Callable[[], None] | None = None,
) -> None:
    _canonical_absolute_path(path, "project wrapper")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    mode = replacement_mode if replacement_mode is not None else expected_mode
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        current, current_mode = _read_regular_file(path, "project wrapper")
        if _sha256(current) != expected_before or current_mode != expected_mode:
            raise AgyLauncherError(
                "project wrapper changed after validation; refusing to clobber it"
            )
        _canonical_absolute_path(path, "project wrapper")
        if pre_replace is not None:
            pre_replace()
        os.replace(temporary, path)
        written, written_mode = _read_regular_file(path, "replaced project wrapper")
        if written != replacement or written_mode != mode:
            raise AgyLauncherError("project wrapper replacement failed read-back verification")
        _fsync_directory(path.parent)
    except OSError as exc:
        raise AgyLauncherError(f"could not atomically replace project wrapper: {path}") from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_hash(expected: str, actual: str, label: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AgyLauncherError(
            "expected sha256 must be a lowercase 64-character hexadecimal digest"
        )
    if expected != actual:
        raise AgyLauncherError(
            f"{label} does not match the expected sha256; refusing to clobber it"
        )


def _file_sha256(path: Path) -> str:
    content, _ = _read_regular_file(path, "AGY launcher")
    return _sha256(content)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
