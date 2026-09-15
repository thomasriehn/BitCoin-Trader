"""JSON lines helpers and atomic JSON file writes."""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO


def _to_path(path: str | Path) -> Path:
    return Path(path)


def append_jsonl(path: str | Path, obj: dict[str, Any]) -> None:
    """Append one JSON object as a single line. Creates the parent directory."""
    p = _to_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    with p.open("a+b") as fh:
        # If a previous writer crashed mid-line, start on a fresh line so the new
        # record stays readable (the truncated line is skipped by read_jsonl_tail).
        if not _ends_with_newline(fh):
            fh.write(b"\n")
        fh.write(line.encode("utf-8"))
        fh.write(b"\n")
        fh.flush()


def _ends_with_newline(fh: BinaryIO) -> bool:
    """True if the (append-mode) file is empty or its last byte is a newline."""
    try:
        size = fh.seek(0, os.SEEK_END)
        if size == 0:
            return True
        fh.seek(size - 1)
        last = fh.read(1)
        fh.seek(0, os.SEEK_END)
        return last == b"\n"
    except OSError:
        return True


def read_jsonl_tail(path: str | Path, n: int = 50) -> list[dict[str, Any]]:
    """Return the last ``n`` valid JSON objects of a JSON lines file, oldest first.

    Lines that do not parse (for example a truncated last line after a crash)
    are skipped. A missing file yields an empty list.
    """
    p = _to_path(path)
    if n <= 0 or not p.exists():
        return []
    tail: deque[dict[str, Any]] = deque(maxlen=n)
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                tail.append(obj)
    return list(tail)


def atomic_write_json(path: str | Path, obj: dict[str, Any]) -> None:
    """Write ``obj`` as JSON via a temp file in the same directory plus ``os.replace``.

    The temp file is fsynced before the rename, so readers see either the old
    or the complete new file, never a partial one.
    """
    p = _to_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Directory fsync is best effort (not supported on every filesystem).
        pass


def read_json(path: str | Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``. Returns None if missing, invalid or not an object."""
    p = _to_path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj
