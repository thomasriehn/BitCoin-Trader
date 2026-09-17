"""Shared fixtures for the ledger tests."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

import pytest

from btctrader.common.db import connect
from btctrader.ledger.fills import Fill, upsert_fills

ACCOUNT = "test-account"


@pytest.fixture
def ledger_conn(tmp_path: Path) -> Iterable[sqlite3.Connection]:
    conn = connect(tmp_path / "ledger.sqlite")
    yield conn
    conn.close()


def make_fill(
    trade_id: str,
    ts: str,
    side: str,
    amount: str,
    price: str,
    fee: str = "0",
    fee_currency: str = "EUR",
    *,
    account: str = ACCOUNT,
    source: str = "freqtrade-db",
    dry_run: bool = True,
    **kwargs: object,
) -> Fill:
    """Build a fill with sensible test defaults. ``ts`` may be a date (midnight UTC) or full ISO."""
    if len(ts) == 10:
        ts = ts + "T12:00:00Z"
    return Fill.build(
        source=source,
        account_id=account,
        exchange_trade_id=trade_id,
        ts_utc=ts,
        side=side,
        amount_btc=Decimal(amount),
        price_eur=Decimal(price),
        fee_amount=Decimal(fee),
        fee_currency=fee_currency,
        dry_run=dry_run,
        **kwargs,  # type: ignore[arg-type]
    )


def store(conn: sqlite3.Connection, fills: Iterable[Fill]) -> None:
    upsert_fills(conn, fills)


@pytest.fixture(autouse=True)
def _drop_cli_log_handlers() -> Iterable[None]:
    """Remove the stderr handler that ``setup_logging`` adds, so it never outlives pytest's capture."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_btctrader", False):
            root.removeHandler(handler)
