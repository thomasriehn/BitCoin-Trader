"""Dry-run sync from a fixture Freqtrade DB and exchange sync via a fake ccxt client."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from btctrader.common.config import load_settings
from btctrader.ledger.errors import ChainError, LedgerError
from btctrader.ledger.fills import get_state, load_fills, verify_chain
from btctrader.ledger.sync import (
    fetch_exchange_fills,
    fill_from_ccxt_trade,
    fills_from_freqtrade_db,
    run_sync,
)
from tests.ledger.conftest import ACCOUNT
from tests.ledger.ft_fixture import create_ft_db


@pytest.fixture
def ft_db(tmp_path: Path) -> Path:
    return create_ft_db(tmp_path / "tradesv3.dryrun.sqlite")


def _settings(tmp_path: Path, **extra: str) -> Any:
    env = {
        "LEDGER_DB_PATH": str(tmp_path / "ledger.sqlite"),
        "LEDGER_ACCOUNT_ID": ACCOUNT,
        "LEDGER_SOURCE": "freqtrade-db",
        "BTCTRADER_ENV_FILE": str(tmp_path / "missing.env") if False else "",
        **extra,
    }
    return load_settings(env)


def test_fills_from_freqtrade_db(ft_db: Path) -> None:
    fills = fills_from_freqtrade_db(ft_db, ACCOUNT)
    assert [f.exchange_trade_id for f in fills] == [
        "ft-dry_run_buy_1",
        "ft-dry_run_sell_1",
        "ft-dry_run_sell_2",
        "ft-dry_run_buy_2",
    ]
    buy = fills[0]
    assert buy.source == "freqtrade-db" and buy.dry_run == 1 and buy.maker_taker is None
    assert buy.ts_utc == "2026-03-01T00:05:00Z"
    assert buy.side == "buy" and buy.amount_btc == Decimal("0.01") and buy.price_eur == Decimal("40000")
    assert buy.gross_eur == Decimal("400")
    assert buy.fee_eur == Decimal("1")  # 0.01 x 40000 x 0.0025
    assert buy.fee_currency == "EUR" and buy.net_eur == Decimal("401")
    assert (buy.strategy, buy.signal_reason, buy.client_order_id) == ("BtcTrend", "trend_up", "trade-1")
    assert buy.reconciled_with_bot_db == 1
    partial = fills[1]
    assert partial.side == "sell" and partial.signal_reason == "partial_exit"
    assert partial.fee_eur == Decimal("0.42")  # 0.004 x 42000 x 0.0025
    assert partial.net_eur == Decimal("167.58")
    final = fills[2]
    assert final.signal_reason == "exit_signal"
    second_buy = fills[3]
    assert second_buy.price_eur == Decimal("45000.5")  # average preferred over price
    assert second_buy.fee_eur == Decimal("0.6750075")  # 0.01 x 45000.5 x 0.0015


def test_run_sync_from_freqtrade_db_is_idempotent(
    ft_db: Path, ledger_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, FT_DB_PATH=str(ft_db))
    result = run_sync(ledger_conn, settings)
    assert result.source == "freqtrade-db" and result.fetched == 4
    assert result.upsert.inserted == 4
    assert result.fifo is not None
    assert len(result.fifo.lots) == 2 and len(result.fifo.disposals) == 2
    assert result.fifo.btc_held == Decimal("0.01")
    assert result.last_fill_ts == "2026-05-01T00:05:00Z"
    assert get_state(ledger_conn, "last_fill_ts") == "2026-05-01T00:05:00Z"
    assert verify_chain(ledger_conn, ACCOUNT) == []

    again = run_sync(ledger_conn, settings)
    assert again.upsert.inserted == 0 and again.upsert.unchanged == 4
    assert ledger_conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 4
    assert ledger_conn.execute("SELECT COUNT(*) FROM disposals").fetchone()[0] == 2
    gains = [Decimal(r[0]) for r in ledger_conn.execute("SELECT gain_eur FROM disposals ORDER BY id")]
    # sell 1: 168 - 0.4 x 401 - 0.42 = 7.18 ; sell 2: 264 - 0.6 x 401 - 0.66 = 22.74
    assert gains == [Decimal("7.18"), Decimal("22.74")]


def test_run_sync_refuses_broken_chain(ft_db: Path, ledger_conn: sqlite3.Connection, tmp_path: Path) -> None:
    settings = _settings(tmp_path, FT_DB_PATH=str(ft_db))
    run_sync(ledger_conn, settings)
    with ledger_conn:
        ledger_conn.execute("UPDATE fills SET price_eur = '1' WHERE exchange_trade_id = 'ft-dry_run_buy_1'")
    with pytest.raises(ChainError):
        run_sync(ledger_conn, settings)


def test_missing_ft_db_raises(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="not found"):
        fills_from_freqtrade_db(tmp_path / "nope.sqlite", ACCOUNT)


# -- exchange ------------------------------------------------------------------------


def _ccxt_trade(
    trade_id: str,
    ts: int,
    side: str,
    amount: str,
    price: str,
    fee: str,
    fee_cur: str = "EUR",
    taker: bool = True,
) -> dict[str, Any]:
    """Shape of ``ccxt.bitvavo.parse_trade`` output (unified floats, raw strings in ``info``)."""
    return {
        "info": {
            "id": trade_id,
            "orderId": f"order-{trade_id}",
            "timestamp": ts,
            "market": "BTC-EUR",
            "side": side,
            "amount": amount,
            "price": price,
            "taker": taker,
            "fee": fee,
            "feeCurrency": fee_cur,
            "settled": True,
        },
        "id": trade_id,
        "symbol": "BTC/EUR",
        "timestamp": ts,
        "datetime": None,
        "order": f"order-{trade_id}",
        "type": None,
        "side": side,
        "takerOrMaker": "taker" if taker else "maker",
        "price": float(price),
        "amount": float(amount),
        "cost": float(amount) * float(price),
        "fee": {"cost": float(fee), "currency": fee_cur},
        "fees": [{"cost": float(fee), "currency": fee_cur}],
    }


class FakeExchange:
    """Mimics Bitvavo ``GET /trades``: newest first, filtered by ``start`` (since) and ``end`` (until)."""

    def __init__(self, trades: list[dict[str, Any]]) -> None:
        self.trades = sorted(trades, key=lambda t: t["timestamp"], reverse=True)
        self.calls: list[tuple[int | None, int | None, int | None]] = []

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):  # type: ignore[no-untyped-def]
        assert symbol == "BTC/EUR"
        until = (params or {}).get("until")
        self.calls.append((since, limit, until))
        rows = [
            t
            for t in self.trades
            if (since is None or t["timestamp"] >= since) and (until is None or t["timestamp"] <= until)
        ]
        return rows[: limit or 500]


def test_fill_from_ccxt_trade_uses_raw_strings() -> None:
    trade = _ccxt_trade(
        "f1", 1_767_225_600_000, "buy", "0.01234567", "61234.5", "0.00001234", "BTC", taker=False
    )
    fill = fill_from_ccxt_trade(trade, ACCOUNT)
    assert fill.source == "exchange" and fill.dry_run == 0
    assert fill.ts_utc == "2026-01-01T00:00:00Z"
    assert fill.exchange_order_id == "order-f1" and fill.maker_taker == "maker"
    assert fill.amount_btc == Decimal("0.01234567") and fill.price_eur == Decimal("61234.5")
    assert fill.fee_currency == "BTC" and fill.fee_amount == Decimal("0.00001234")
    assert fill.fee_eur == Decimal("0.75563373")  # 0.00001234 x 61234.5 rounded to 8 places
    assert fill.pair == "BTC/EUR"


def test_fetch_exchange_fills_pages_and_dedups() -> None:
    base = 1_767_225_600_000
    trades = [_ccxt_trade(f"f{i}", base + i * 1000, "buy", "0.001", "60000", "0.15") for i in range(5)]
    exchange = FakeExchange(trades)
    fills = fetch_exchange_fills(exchange, ACCOUNT, since_ms=None, page_limit=2)
    assert [f.exchange_trade_id for f in fills] == ["f0", "f1", "f2", "f3", "f4"]
    assert exchange.calls[0] == (None, 2, None)
    assert exchange.calls[1] == (None, 2, base + 3000 - 1)  # walks backwards below the oldest trade seen
    assert len(exchange.calls) == 3


def test_run_sync_exchange_mode(ledger_conn: sqlite3.Connection, tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, LEDGER_SOURCE="exchange", BITVAVO_API_KEY_RO="k", BITVAVO_API_SECRET_RO="s"
    )
    base = 1_767_225_600_000
    exchange = FakeExchange(
        [
            _ccxt_trade("f1", base, "buy", "0.02", "50000", "2.5"),
            _ccxt_trade("f2", base + 86_400_000, "sell", "0.01", "55000", "1.375"),
        ]
    )
    result = run_sync(ledger_conn, settings, exchange=exchange)
    assert result.upsert.inserted == 2 and result.fifo is not None
    assert result.fifo.btc_held == Decimal("0.01")
    fills = load_fills(ledger_conn, ACCOUNT)
    assert all(f.source == "exchange" and f.dry_run == 0 for f in fills)
    # Second run continues from the last fill timestamp minus an overlap window.
    run_sync(ledger_conn, settings, exchange=exchange)
    assert exchange.calls[-1][0] is not None and exchange.calls[-1][0] < base
    assert ledger_conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
