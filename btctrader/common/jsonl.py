"""JSON lines helpers and atomic JSON file writes."""

from __future__ import annotations

import json
import os
import tempfile
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


TAIL_BLOCK_SIZE = 64 * 1024


def _parse_jsonl_line(raw: bytes) -> dict[str, Any] | None:
    """Decode one line; returns None for blank, invalid or non-object lines."""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def read_jsonl_tail(path: str | Path, n: int = 50) -> list[dict[str, Any]]:
    """Return the last ``n`` valid JSON objects of a JSON lines file, oldest first.

    Reads the file backwards in ``TAIL_BLOCK_SIZE`` blocks, so the cost depends
    on ``n`` and the line length, not on the file size (``decisions.jsonl`` is
    kept for ten years and polled by the dashboard every 30 s).
    Lines that do not parse (for example a truncated last line after a crash)
    are skipped. A missing file yields an empty list.
    """
    p = _to_path(path)
    if n <= 0 or not p.exists():
        return []
    found: list[dict[str, Any]] = []
    with p.open("rb") as fh:
        pos = fh.seek(0, os.SEEK_END)
        pending = b""  # bytes before the first newline of the blocks read so far
        while pos > 0 and len(found) < n:
            step = min(TAIL_BLOCK_SIZE, pos)
            pos -= step
            fh.seek(pos)
            parts = (fh.read(step) + pending).split(b"\n")
            if pos > 0:
                # parts[0] may be the tail of a line that starts in an earlier block
                pending = parts[0]
                complete = parts[1:]
            else:
                pending = b""
                complete = parts
            for raw in reversed(complete):
                obj = _parse_jsonl_line(raw)
                if obj is not None:
                    found.append(obj)
                    if len(found) >= n:
                        break
    found.reverse()
    return found


def atomic_write_json(path: str | Path, obj: dict[str, Any]) -> None:
    """Write ``obj`` as JSON via a temp file in the same directory plus ``os.replace``.

    The temp file is fsynced before the rename, so readers see either the old
    or the complete new file, never a partial one. The result gets the same
    permissions a plain ``open(..., "w")`` would give (``0o666`` minus the
    umask, e.g. ``0640`` under systemd ``UMask=0027``); ``mkstemp`` alone would
    leave every file owner-only (``0600``).
    """
    p = _to_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o666 & ~_current_umask())
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


def _current_umask() -> int:
    """Read the process umask (there is no read-only accessor, so set and restore it)."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


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
