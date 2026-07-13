from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def comparable_tokens(self) -> int:
        return self.uncached_input_tokens + self.output_tokens

    def delta(self, earlier: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=max(0, self.input_tokens - earlier.input_tokens),
            cached_input_tokens=max(
                0,
                self.cached_input_tokens - earlier.cached_input_tokens,
            ),
            output_tokens=max(0, self.output_tokens - earlier.output_tokens),
            reasoning_output_tokens=max(
                0,
                self.reasoning_output_tokens - earlier.reasoning_output_tokens,
            ),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "comparable_tokens": self.comparable_tokens,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> TokenUsage | None:
        if not isinstance(value, dict):
            return None
        return cls(
            input_tokens=_integer(value.get("input_tokens")),
            cached_input_tokens=_integer(value.get("cached_input_tokens")),
            output_tokens=_integer(value.get("output_tokens")),
            reasoning_output_tokens=_integer(value.get("reasoning_output_tokens")),
        )


@dataclass(frozen=True)
class SessionUsageSnapshot:
    usage: TokenUsage
    recorded_at: str | None


def latest_session_usage(path: Path) -> SessionUsageSnapshot | None:
    """Read the newest Codex token snapshot without loading a large rollout."""
    for raw_line in _reverse_binary_lines(path):
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        usage = TokenUsage.from_mapping(info.get("total_token_usage"))
        if usage is None:
            usage = TokenUsage.from_mapping(info.get("last_token_usage"))
        if usage is not None:
            return SessionUsageSnapshot(
                usage=usage,
                recorded_at=_optional_text(event.get("timestamp")),
            )
    return None


def _reverse_binary_lines(path: Path, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            remainder = b""
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                block = handle.read(read_size) + remainder
                lines = block.split(b"\n")
                remainder = lines[0]
                for line in reversed(lines[1:]):
                    if line.strip():
                        yield line
            if remainder.strip():
                yield remainder
    except OSError:
        return


def _integer(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
