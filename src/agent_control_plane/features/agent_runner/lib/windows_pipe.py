"""Windows print-mode transport without ConPTY's VT rewriting or ambiguous EOF."""

from __future__ import annotations

import codecs

# Subprocess execution is limited to the configured agent argv without a shell.
import subprocess  # nosec B404
from io import BufferedReader
from typing import cast

PROCESS_EXIT_TIMEOUT_SEC = 2.0


def _stop_owned_process(process: subprocess.Popen[bytes], *, force: bool) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=PROCESS_EXIT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        if not force:
            raise
        process.kill()
        process.wait(timeout=PROCESS_EXIT_TIMEOUT_SEC)


class WindowsPipeProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            raise RuntimeError("AGY stdout pipe is unavailable")
        self._process = process
        self._stdout = cast(BufferedReader, process.stdout)
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._eof = False
        self.pid: int | None = process.pid

    @classmethod
    def spawn(
        cls,
        argv: list[str],
        *,
        cwd: str | None = None,
        dimensions: tuple[int, int] = (40, 180),
    ) -> WindowsPipeProcess:
        del dimensions
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # The configured argv is passed directly, without shell interpolation.
            shell=False,  # nosec B603
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        try:
            return cls(process)
        except BaseException:
            try:
                _stop_owned_process(process, force=True)
            finally:
                if process.stdout is not None:
                    process.stdout.close()
            raise

    @property
    def exitstatus(self) -> int | None:
        return self._process.poll()

    def isalive(self) -> bool:
        return self._process.poll() is None

    def read(self, size: int) -> str:
        while not self._eof:
            raw = self._stdout.read1(size)
            self._eof = not raw
            text = self._decoder.decode(raw, final=self._eof)
            if text:
                return text
        raise EOFError("AGY pipe closed")

    def terminate(self, force: bool = False) -> None:
        _stop_owned_process(self._process, force=force)

    def close(self) -> None:
        """Close the owned pipe only after the runner's reader has stopped."""
        self._stdout.close()
