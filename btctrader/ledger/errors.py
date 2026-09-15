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
