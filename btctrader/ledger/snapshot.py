"""Daily equity snapshot (``equity_daily``) with benchmarks and tax figures.

Balances come from ccxt ``fetch_balance`` (exchange mode) or from the Freqtrade
``/balance`` endpoint (dry-run mode); the price from the public Bitvavo API.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Protocol

from btctrader.common.ftapi import FreqtradeClient
from btctrader.ledger.benchmarks import PriceSeries, compute_benchmarks
from btctrader.ledger.errors import LedgerError, ccxt_errors
from btctrader.ledger.fills import dec, fmt, q8

log = logging.getLogger(__name__)

Q2 = Decimal("0.01")
ZERO = Decimal(0)


class BalanceExchange(Protocol):
    def fetch_balance(self, params: Any = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Balances:
    eur: Decimal
    btc: Decimal


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_EVEN)


def balances_from_freqtrade(client: FreqtradeClient) -> Balances:
    """Total EUR and BTC balance from ``GET /balance`` (``currencies[].balance``)."""
    data = client.balance()
    eur = ZERO
    btc = ZERO
    for entry in data.get("currencies", []):
        currency = str(entry.get("currency", "")).upper()
        total = dec(entry.get("balance", 0), f"balance {currency}")
        if currency == "EUR":
            eur += total
        elif currency == "BTC":
            btc += total
    return Balances(eur=q8(eur), btc=q8(btc))


def balances_from_exchange(exchange: BalanceExchange) -> Balances:
    """Total EUR and BTC balance from ccxt ``fetch_balance()["total"]``."""
    try:
        data = exchange.fetch_balance({})
    except ccxt_errors() as exc:
        raise LedgerError(f"exchange balance failed: {type(exc).__name__}: {exc}") from exc
    total = data.get("total") or {}
    eur = dec(total.get("EUR", 0) or 0, "EUR balance")
    btc = dec(total.get("BTC", 0) or 0, "BTC balance")
    return Balances(eur=q8(eur), btc=q8(btc))


def fees_cum(conn: sqlite3.Connection, account_id: str, until: date) -> Decimal:
    """Sum of ``fee_eur`` over all fills up to and including ``until`` (UTC date)."""
    next_day = date.fromordinal(until.toordinal() + 1).isoformat()
    rows = conn.execute(
        "SELECT fee_eur FROM fills WHERE account_id = ? AND ts_utc < ?", (account_id, next_day)
    ).fetchall()
    return sum((Decimal(r["fee_eur"]) for r in rows), ZERO)


def realized_gain_ytd(conn: sqlite3.Connection, account_id: str, until: date) -> Decimal:
    """Taxable section 23 gain (after sell fees) of the calendar year of ``until``, up to that day."""
    next_day = date.fromordinal(until.toordinal() + 1).isoformat()
    rows = conn.execute(
        "SELECT gain_eur FROM disposals WHERE account_id = ? AND taxable = 1 "
        "AND disposed_at >= ? AND disposed_at < ?",
        (account_id, f"{until.year:04d}-01-01", next_day),
    ).fetchall()
    return sum((Decimal(r["gain_eur"]) for r in rows), ZERO)


def write_snapshot(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    on: date,
    balances: Balances,
    price: Decimal,
    series: PriceSeries,
    benchmark_start: date,
    start_capital: Decimal,
    dca_weeks: int,
    fee_taker: Decimal,
    dry_run: bool,
) -> dict[str, str | int]:
    """Compute and upsert the ``equity_daily`` row for ``on``; returns the stored row."""
    equity = balances.eur + balances.btc * price
    bench = compute_benchmarks(series, benchmark_start, on, start_capital, dca_weeks, fee_taker)
    row: dict[str, str | int] = {
        "date_utc": on.isoformat(),
        "eur_balance": fmt(q8(balances.eur)),
        "btc_balance": fmt(q8(balances.btc)),
        "btc_price_eur": fmt(price),
        "equity_eur": fmt(q2(equity)),
        "bh_equity_eur": fmt(q2(bench.bh_equity_eur)),
        "dca_equity_eur": fmt(q2(bench.dca_equity_eur)),
        "fees_cum_eur": fmt(q2(fees_cum(conn, account_id, on))),
        "realized_gain_ytd_eur": fmt(q2(realized_gain_ytd(conn, account_id, on))),
        "dry_run": 1 if dry_run else 0,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "date_utc")
    with conn:
        conn.execute(
            f"INSERT INTO equity_daily ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(date_utc) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
    log.info(
        "snapshot %s: equity=%s bh=%s dca=%s fees=%s gain_ytd=%s",
        row["date_utc"],
        row["equity_eur"],
        row["bh_equity_eur"],
        row["dca_equity_eur"],
        row["fees_cum_eur"],
        row["realized_gain_ytd_eur"],
    )
    return row


def latest_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM equity_daily ORDER BY date_utc DESC LIMIT 1").fetchone()
    return dict(row) if row is not None else None
