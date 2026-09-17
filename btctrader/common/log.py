"""Logging setup for the btctrader services.

All services run under systemd; journald captures stderr, so no file
handler is configured here.
"""

from __future__ import annotations

import logging
import sys
import time

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


class _UtcFormatter(logging.Formatter):
    """Formatter that renders timestamps as ISO-8601 in UTC."""

    converter = time.gmtime


def setup_logging(name: str, level: str = "INFO") -> logging.Logger:
    """Configure the root logger once (stderr, UTC ISO timestamps) and return a named logger.

    Calling it repeatedly does not add duplicate handlers; only the level is updated.
    """
    root = logging.getLogger()
    level_value = logging.getLevelName(level.upper())
    if not isinstance(level_value, int):
        level_value = logging.INFO
    root.setLevel(level_value)
    has_own_handler = any(getattr(h, "_btctrader", False) for h in root.handlers)
    if not has_own_handler:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_UtcFormatter(_FORMAT, _DATEFMT))
        handler._btctrader = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    # httpx logs every request at INFO; keep our own logs readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger(name)
