from __future__ import annotations

import os
from typing import Any

# Liveness lives in `shared` rather than in either feature that needs it. The
# controller's gate-slot broker (`features/result_handoff`) and the agent runner
# both reclaim leases held by dead PIDs, and a feature may not import another
# feature -- so the check has to sit below both of them, not inside one.


def process_is_alive(pid: int | None) -> bool:
    """Whether `pid` names a process that currently exists.

    A non-positive or absent pid is never alive, so callers holding an optional
    pid can pass it through without a guard of their own.
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    windows_ctypes: Any = ctypes
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    still_active = 259
    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
            exit_code.value == still_active
        )
    finally:
        close_handle(handle)
