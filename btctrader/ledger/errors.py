"""Exception types of the ledger package."""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for ledger failures."""


class ChainError(LedgerError):
    """The SHA-256 hash chain over the fills table is broken (tampering or corruption)."""


class FifoError(LedgerError):
    """The FIFO rebuild found an inconsistency (for example a sell without enough lots)."""


class BenchmarkError(LedgerError):
    """A benchmark cannot be computed (missing price data)."""


def ccxt_errors() -> tuple[type[BaseException], ...]:
    """Exception types raised by ccxt, or an empty tuple when ccxt is not installed.

    Lets callers write ``except ccxt_errors() as exc`` without importing the heavy
    ccxt package at module import time (dry-run mode never needs it).
    """
    try:
        import ccxt
    except ImportError:  # pragma: no cover - ccxt is a declared dependency
        return ()
    return (ccxt.BaseError,)
