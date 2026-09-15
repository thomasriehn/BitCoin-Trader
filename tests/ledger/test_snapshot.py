"""equity_daily snapshot with balances from Freqtrade or ccxt and benchmark columns."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import respx

from btctrader.common.ftapi import FreqtradeClient
from btctrader.ledger.benchmarks import PriceSeries
from btctrader.ledger.fifo import rebuild
from btctrader.ledger.snapshot import (
    Balances,
    balances_from_exchange,
    balances_from_freqtrade,
    latest_snapshot,
    write_snapshot,
)
from tests.ledger.conftest import ACCOUNT, make_fill, store


def test_balances_from_freqtrade(respx_mock: respx.MockRouter) -> None:
    respx_mock.post("http://ft/api/v1/token/login").mock(
        return_value=httpx.Response(200, json={"access_token": "t"})
    )
    respx_mock.get("http://ft/api/v1/balance").mock(
        return_value=httpx.Response(
            200,
            json={
                "currencies": [
                    {"currency": "EUR", "free": 500.5, "balance": 600.25, "used": 99.75, "est_stake": 600.25},
                    {"currency": "BTC", "free": 0.01, "balance": 0.012, "used": 0.002, "est_stake": 720.0},
                ],
                "total": 1320.25,
                "stake": "EUR",
            },
        )
    )
    client = FreqtradeClient("http://ft", "u", "p")
    assert balances_from_freqtrade(client) == Balances(eur=Decimal("600.25"), btc=Decimal("0.012"))


def test_balances_from_exchange() -> None:
    class Fake:
        def fetch_balance(self, params: Any = None) -> dict[str, Any]:
            return {"total": {"EUR": 12.5, "BTC": 0.5}, "free": {}, "used": {}}

    assert balances_from_exchange(Fake()) == Balances(eur=Decimal("12.5"), btc=Decimal("0.5"))


def test_write_snapshot_row(ledger_conn: sqlite3.Connection) -> None:
    start = date(2026, 1, 1)
    on = date(2026, 1, 15)
    series = PriceSeries({start + timedelta(days=i): Decimal(10000 + 100 * i) for i in range(30)})
    store(
        ledger_conn,
        [
            make_fill("b1", "2026-01-02", "buy", "0.05", "10000", "1.25"),
            make_fill("s1", "2026-01-10", "sell", "0.02", "12000", "0.6"),
            make_fill("s2", "2026-01-20", "sell", "0.01", "13000", "0.3"),  # after the snapshot date
        ],
    )
    rebuild(ledger_conn, ACCOUNT)
    row = write_snapshot(
        ledger_conn,
        account_id=ACCOUNT,
        on=on,
        balances=Balances(eur=Decimal("700"), btc=Decimal("0.03")),
        price=Decimal("11400"),
        series=series,
        benchmark_start=start,
        start_capital=Decimal("1000"),
        dca_weeks=4,
        fee_taker=Decimal("0.0025"),
        dry_run=True,
    )
    assert row["date_utc"] == "2026-01-15"
    assert row["equity_eur"] == "1042"  # 700 + 0.03 x 11400
    assert row["btc_balance"] == "0.03" and row["btc_price_eur"] == "11400"
    assert row["fees_cum_eur"] == "1.85"  # 1.25 + 0.6, s2 is later
    # s1 gain: 240 - 0.4 x 501.25 - 0.6 = 38.90
    assert row["realized_gain_ytd_eur"] == "38.9"
    assert Decimal(row["bh_equity_eur"]) == Decimal("1137.15")  # 0.09975 BTC x 11400
    assert Decimal(row["dca_equity_eur"]) > Decimal("1000")
    assert row["dry_run"] == 1
    stored = latest_snapshot(ledger_conn)
    assert stored is not None and stored["equity_eur"] == "1042"
    # Re-running the same day overwrites the row.
    write_snapshot(
        ledger_conn,
        account_id=ACCOUNT,
        on=on,
        balances=Balances(eur=Decimal("700"), btc=Decimal("0.03")),
        price=Decimal("11500"),
        series=series,
        benchmark_start=start,
        start_capital=Decimal("1000"),
        dca_weeks=4,
        fee_taker=Decimal("0.0025"),
        dry_run=True,
    )
    assert ledger_conn.execute("SELECT COUNT(*) FROM equity_daily").fetchone()[0] == 1
    assert latest_snapshot(ledger_conn)["equity_eur"] == "1045"  # type: ignore[index]
