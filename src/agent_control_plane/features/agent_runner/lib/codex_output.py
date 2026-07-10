from __future__ import annotations

import threading
from pathlib import Path
from types import TracebackType
from typing import IO, TextIO

from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.features.agent_runner.lib.codex_telemetry import (
    parse_codex_jsonl,
    render_codex_json_line,
)


class CodexOutputCapture:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.event_log_path = log_path.with_suffix(".events.jsonl")
        self.log: TextIO | None = None
        self._events: TextIO | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> CodexOutputCapture:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("w", encoding="utf-8", errors="replace")
        self._events = self.event_log_path.open("w", encoding="utf-8", errors="replace")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.join()
        if self._events is not None:
            self._events.close()
        if self.log is not None:
            self.log.close()

    def start(self, stream: IO[str]) -> None:
        if self.log is None or self._events is None:
            raise RuntimeError("CodexOutputCapture must be entered before start")
        self._thread = threading.Thread(
            target=self._pump,
            args=(stream,),
            name="codex-jsonl-pump",
            daemon=True,
        )
        self._thread.start()

    def join(self, timeout_sec: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
        if self._events is not None:
            self._events.flush()
        if self.log is not None:
            self.log.flush()

    def metrics(self, *, model: str, duration_sec: float) -> AttemptMetrics:
        self.join()
        return parse_codex_jsonl(
            self.event_log_path,
            model=model,
            duration_sec=duration_sec,
        )

    def _pump(self, stream: IO[str]) -> None:
        log = self.log
        events = self._events
        if log is None or events is None:
            raise RuntimeError("CodexOutputCapture closed before output pump started")
        try:
            for line in stream:
                events.write(line.rstrip("\r\n") + "\n")
                events.flush()
                log.write(render_codex_json_line(line))
                log.flush()
        except OSError as exc:
            log.write(f"\n[codex output pump failed: {exc}]\n")
            log.flush()
        finally:
            stream.close()
