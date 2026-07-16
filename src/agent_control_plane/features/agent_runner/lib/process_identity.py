from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

PROCESS_IDENTITY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Durable identity for one OS process instance, not merely a reusable PID."""

    pid: int
    started_key: str
    executable: str

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError("process identity PID must be positive")
        if not self.started_key.strip():
            raise ValueError("process identity started_key must not be empty")
        if not self.executable.strip():
            raise ValueError("process identity executable must not be empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROCESS_IDENTITY_SCHEMA_VERSION,
            "pid": self.pid,
            "started_key": self.started_key,
            "executable": self.executable,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProcessIdentity:
        if payload.get("schema_version") != PROCESS_IDENTITY_SCHEMA_VERSION:
            raise ValueError("unsupported process identity schema_version")
        pid = payload.get("pid")
        started_key = payload.get("started_key")
        executable = payload.get("executable")
        if type(pid) is not int:  # bool is not a valid PID
            raise ValueError("process identity pid must be an integer")
        if not isinstance(started_key, str) or not isinstance(executable, str):
            raise ValueError("process identity fields are malformed")
        return cls(pid=pid, started_key=started_key, executable=executable)

    @classmethod
    def from_json(cls, payload: str) -> ProcessIdentity:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid process identity JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("process identity JSON must be an object")
        return cls.from_dict(parsed)


class ProcessTerminationState(StrEnum):
    TERMINATED = "terminated"
    NOT_FOUND = "not_found"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProcessTerminationResult:
    state: ProcessTerminationState
    pid: int
    message: str


def supports_verified_process_termination() -> bool:
    if os.name == "nt":
        return True
    return bool(
        sys.platform.startswith("linux")
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal")
    )


def process_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def capture_process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        raise ValueError("pid must be positive")
    if os.name == "nt":
        return _capture_windows_process_identity(pid)
    if sys.platform.startswith("linux"):
        return _capture_linux_process_identity(pid)
    return None


def terminate_verified_process(
    expected: ProcessIdentity,
    *,
    timeout_sec: float = 5.0,
) -> ProcessTerminationResult:
    if timeout_sec < 0:
        raise ValueError("timeout_sec must be non-negative")
    if os.name == "nt":
        return _terminate_verified_windows_process(expected, timeout_sec)
    if sys.platform.startswith("linux"):
        return _terminate_verified_linux_process(expected, timeout_sec)
    return ProcessTerminationResult(
        ProcessTerminationState.UNSUPPORTED,
        expected.pid,
        f"Verified process termination is unsupported on {sys.platform}",
    )


def _capture_linux_process_identity(pid: int) -> ProcessIdentity | None:
    proc_root = Path("/proc") / str(pid)
    try:
        stat = (proc_root / "stat").read_text(encoding="utf-8", errors="strict")
        executable = os.readlink(proc_root / "exe")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    closing_paren = stat.rfind(")")
    if closing_paren < 0:
        return None
    fields_after_name = stat[closing_paren + 2 :].split()
    if len(fields_after_name) <= 19:
        return None
    start_ticks = fields_after_name[19]
    return ProcessIdentity(
        pid=pid,
        started_key=f"linux-start-ticks:{start_ticks}",
        executable=_normalize_executable(executable),
    )


def _terminate_verified_linux_process(
    expected: ProcessIdentity,
    timeout_sec: float,
) -> ProcessTerminationResult:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return ProcessTerminationResult(
            ProcessTerminationState.UNSUPPORTED,
            expected.pid,
            "Linux pidfd process termination is unavailable",
        )
    try:
        pidfd = pidfd_open(expected.pid, 0)
    except ProcessLookupError:
        return ProcessTerminationResult(
            ProcessTerminationState.NOT_FOUND,
            expected.pid,
            "Process exited before verified termination",
        )
    except OSError as exc:
        return ProcessTerminationResult(
            ProcessTerminationState.ERROR,
            expected.pid,
            f"Could not open pidfd: {exc}",
        )
    try:
        current = _capture_linux_process_identity(expected.pid)
        if current is None:
            return ProcessTerminationResult(
                ProcessTerminationState.NOT_FOUND,
                expected.pid,
                "Process exited before identity verification",
            )
        if current != expected:
            return ProcessTerminationResult(
                ProcessTerminationState.IDENTITY_MISMATCH,
                expected.pid,
                "PID now belongs to a different process instance",
            )
        try:
            pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            return ProcessTerminationResult(
                ProcessTerminationState.NOT_FOUND,
                expected.pid,
                "Process exited before termination signal",
            )
        except OSError as exc:
            return ProcessTerminationResult(
                ProcessTerminationState.ERROR,
                expected.pid,
                f"Could not terminate verified process: {exc}",
            )
        if not _wait_for_pidfd(pidfd, timeout_sec):
            try:
                pidfd_send_signal(pidfd, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                pass
            except OSError as exc:
                return ProcessTerminationResult(
                    ProcessTerminationState.ERROR,
                    expected.pid,
                    f"Could not kill verified process after timeout: {exc}",
                )
            if not _wait_for_pidfd(pidfd, 2.0):
                return ProcessTerminationResult(
                    ProcessTerminationState.ERROR,
                    expected.pid,
                    "Verified process remained alive after SIGKILL",
                )
        return ProcessTerminationResult(
            ProcessTerminationState.TERMINATED,
            expected.pid,
            "Terminated the exact process instance through pidfd",
        )
    finally:
        os.close(pidfd)


def _wait_for_pidfd(pidfd: int, timeout_sec: float) -> bool:
    import select

    poller = select.poll()  # type: ignore[attr-defined]
    poller.register(pidfd, select.POLLIN)  # type: ignore[attr-defined]
    return bool(poller.poll(max(0, round(timeout_sec * 1000))))


def _windows_process_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        close_handle(handle)


def _capture_windows_process_identity(pid: int) -> ProcessIdentity | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x1000, False, pid)
    if not handle:
        return None
    try:
        return _windows_identity_from_handle(kernel32, handle, pid)
    finally:
        close_handle(handle)


def _windows_identity_from_handle(kernel32: Any, handle: Any, pid: int) -> ProcessIdentity | None:
    import ctypes
    from ctypes import wintypes

    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    exit_code = wintypes.DWORD()
    if not get_exit_code(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
        return None

    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    get_process_times.restype = wintypes.BOOL
    if not get_process_times(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None

    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    query_image.restype = wintypes.BOOL
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not query_image(handle, 0, buffer, ctypes.byref(size)):
        return None
    created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return ProcessIdentity(
        pid=pid,
        started_key=f"windows-filetime:{created}",
        executable=_normalize_executable(buffer.value),
    )


def _terminate_verified_windows_process(
    expected: ProcessIdentity,
    timeout_sec: float,
) -> ProcessTerminationResult:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    access = 0x0001 | 0x00100000 | 0x1000
    handle = open_process(access, False, expected.pid)
    if not handle:
        error = ctypes.get_last_error()
        state = (
            ProcessTerminationState.NOT_FOUND
            if error in {87, 1168}
            else ProcessTerminationState.ERROR
        )
        return ProcessTerminationResult(
            state,
            expected.pid,
            f"Could not open process for verified termination (WinError {error})",
        )
    try:
        current = _windows_identity_from_handle(kernel32, handle, expected.pid)
        if current is None:
            return ProcessTerminationResult(
                ProcessTerminationState.NOT_FOUND,
                expected.pid,
                "Process exited before identity verification",
            )
        if current != expected:
            return ProcessTerminationResult(
                ProcessTerminationState.IDENTITY_MISMATCH,
                expected.pid,
                "PID now belongs to a different process instance",
            )
        terminate_process = kernel32.TerminateProcess
        terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
        terminate_process.restype = wintypes.BOOL
        if not terminate_process(handle, 1):
            error = ctypes.get_last_error()
            return ProcessTerminationResult(
                ProcessTerminationState.ERROR,
                expected.pid,
                f"Could not terminate verified process (WinError {error})",
            )
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        timeout_ms = min(0xFFFFFFFE, max(0, round(timeout_sec * 1000)))
        if wait_for_single_object(handle, timeout_ms) != 0:
            return ProcessTerminationResult(
                ProcessTerminationState.ERROR,
                expected.pid,
                "Verified process did not exit before the timeout",
            )
        return ProcessTerminationResult(
            ProcessTerminationState.TERMINATED,
            expected.pid,
            "Terminated the exact process instance through a verified Windows handle",
        )
    finally:
        close_handle(handle)


def _normalize_executable(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))
