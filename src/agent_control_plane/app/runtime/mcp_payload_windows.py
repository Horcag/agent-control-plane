from __future__ import annotations

import codecs
import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_PREVIEW_BYTES = 16 * 1024
MAX_PREVIEW_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
_MIN_UTF8_WINDOW_BYTES = 4


def file_preview(
    path: Path,
    *,
    stable_id: str,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_BYTES,
    include_file_sha256: bool = True,
) -> dict[str, Any]:
    """Read one UTF-8-safe file window and hash the full file only when requested."""
    effective_limit = _validate_limit(limit)
    total_bytes = path.stat().st_size
    effective_offset = _utf8_start_offset(path, _validate_offset(offset, total_bytes), total_bytes)
    with path.open("rb") as handle:
        handle.seek(effective_offset)
        raw = handle.read(effective_limit)
    complete = _complete_utf8_prefix(raw)
    returned_bytes = len(complete)
    next_offset = effective_offset + returned_bytes
    truncated = next_offset < total_bytes
    payload: dict[str, Any] = {
        "id": stable_id,
        "path": str(path),
        "available": True,
        "content": complete.decode("utf-8", errors="replace"),
        "offset": effective_offset,
        "limit": effective_limit,
        "total_bytes": total_bytes,
        "returned_bytes": returned_bytes,
        "sha256": _sha256_file(path) if include_file_sha256 else None,
        "truncated": truncated,
        "next_offset": next_offset if truncated else None,
    }
    if not include_file_sha256:
        payload["window_sha256"] = hashlib.sha256(complete).hexdigest()
    return payload


def missing_file_preview(
    path: Path,
    *,
    stable_id: str,
    message: str,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    effective_limit = _validate_limit(limit)
    return {
        "id": stable_id,
        "path": str(path),
        "available": False,
        "content": "",
        "message": _utf8_prefix(message, effective_limit),
        "offset": 0,
        "limit": effective_limit,
        "total_bytes": 0,
        "returned_bytes": 0,
        "sha256": None,
        "truncated": False,
        "next_offset": None,
    }


def tail_preview(
    path: Path,
    *,
    stable_id: str,
    lines: int,
    cursor: int | None = None,
    limit: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    total_bytes = path.stat().st_size
    start = _tail_start_offset(path, max(1, lines))
    requested_offset = (
        start if cursor is None else max(start, _validate_offset(cursor, total_bytes))
    )
    payload = file_preview(
        path,
        stable_id=stable_id,
        offset=requested_offset,
        limit=limit,
        include_file_sha256=False,
    )
    payload["cursor"] = payload["offset"]
    payload["next_cursor"] = payload["next_offset"]
    payload["tail_start"] = start
    payload["lines"] = max(1, lines)
    payload["total_bytes"] = total_bytes
    return payload


def text_preview(
    content: str,
    *,
    stable_id: str,
    path: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_PREVIEW_BYTES,
    sha256: str | None = None,
) -> dict[str, Any]:
    raw = content.encode("utf-8")
    total_bytes = len(raw)
    effective_offset = _utf8_bytes_start(raw, _validate_offset(offset, total_bytes))
    effective_limit = _validate_limit(limit)
    complete = _complete_utf8_prefix(raw[effective_offset : effective_offset + effective_limit])
    next_offset = effective_offset + len(complete)
    truncated = next_offset < total_bytes
    return {
        "id": stable_id,
        "path": path,
        "available": True,
        "content": complete.decode("utf-8", errors="replace"),
        "offset": effective_offset,
        "limit": effective_limit,
        "total_bytes": total_bytes,
        "returned_bytes": len(complete),
        "sha256": sha256 or hashlib.sha256(raw).hexdigest(),
        "truncated": truncated,
        "next_offset": next_offset if truncated else None,
    }


def serialized_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not _MIN_UTF8_WINDOW_BYTES <= limit <= MAX_PREVIEW_BYTES:
        raise ValueError(
            f"limit must be between {_MIN_UTF8_WINDOW_BYTES} and {MAX_PREVIEW_BYTES} bytes"
        )
    return limit


def _validate_offset(offset: int, total_bytes: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    return min(total_bytes, offset)


def _utf8_start_offset(path: Path, offset: int, total_bytes: int) -> int:
    if offset <= 0 or offset >= total_bytes:
        return offset
    with path.open("rb") as handle:
        handle.seek(offset)
        byte = handle.read(1)
        while byte and byte[0] & 0xC0 == 0x80 and offset < total_bytes:
            offset += 1
            byte = handle.read(1)
    return offset


def _utf8_bytes_start(raw: bytes, offset: int) -> int:
    while offset < len(raw) and raw[offset] & 0xC0 == 0x80:
        offset += 1
    return offset


def _complete_utf8_prefix(raw: bytes) -> bytes:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder.decode(raw, final=False)
    pending, _ = decoder.getstate()
    return raw[: len(raw) - len(pending)] if pending else raw


def _utf8_prefix(value: str, limit: int) -> str:
    raw = value.encode("utf-8")[:limit]
    return _complete_utf8_prefix(raw).decode("utf-8", errors="replace")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_start_offset(path: Path, lines: int) -> int:
    size = path.stat().st_size
    if size == 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(size - 1)
        trailing_newline = handle.read(1) == b"\n"
        needed = lines + int(trailing_newline)
        position = size
        chunk_size = 64 * 1024
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            for index in range(len(chunk) - 1, -1, -1):
                if chunk[index] == 0x0A:
                    needed -= 1
                    if needed == 0:
                        return position + index + 1
    return 0
