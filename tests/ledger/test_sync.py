"""Dry-run sync from a fixture Freqtrade DB and exchange sync via a fake ccxt client."""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from btctrader.common.config import load_settings
from btctrader.ledger.errors import ChainError, LedgerError
from btctrader.ledger.fills import get_state, load_fills, verify_chain
from btctrader.ledger.sync import (
    STATE_FIFO_ERROR,
    STATE_FIFO_ERROR_UTC,
    fetch_exchange_fills,
    fill_from_ccxt_trade,
    fills_from_freqtrade_db,
    run_sync,
)
from tests.ledger.conftest import ACCOUNT, make_fill, store
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
        "ft-dry_run_buy_4",
    ]
    buy = fills[0]
    assert buy.source == "freqtrade-db" and buy.dry_run == 1 and buy.maker_taker is None
    assert buy.ts_utc == "2026-03-01T00:05:00.000000Z"
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
    # Partially filled entry that Freqtrade cancelled: the filled part is a real buy.
    partial_buy = fills[4]
    assert partial_buy.side == "buy" and partial_buy.amount_btc == Decimal("0.004")
    assert partial_buy.price_eur == Decimal("46000") and partial_buy.client_order_id == "trade-2"


def test_run_sync_from_freqtrade_db_is_idempotent(
    ft_db: Path, ledger_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, FT_DB_PATH=str(ft_db))
    result = run_sync(ledger_conn, settings)
    assert result.source == "freqtrade-db" and result.fetched == 5
    assert result.upsert.inserted == 5
    assert result.fifo is not None and result.fifo_error is None
    assert len(result.fifo.lots) == 3 and len(result.fifo.disposals) == 2
    assert result.fifo.btc_held == Decimal("0.014")
    assert result.last_fill_ts == "2026-05-02T00:05:00.000000Z"
    assert get_state(ledger_conn, "last_fill_ts") == "2026-05-02T00:05:00.000000Z"
    assert verify_chain(ledger_conn, ACCOUNT) == []

    again = run_sync(ledger_conn, settings)
    assert again.upsert.inserted == 0 and again.upsert.unchanged == 5
    assert ledger_conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 5
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


def test_dust_eaten_btc_fee_is_warned(ft_db: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Freqtrade fee in BTC without ft_fee_base means the fee was eaten into dust: warn, book EUR."""
    conn = sqlite3.connect(ft_db)
    with conn:
        conn.execute("UPDATE trades SET fee_open_currency = 'BTC' WHERE id = 2")
    conn.close()
    with caplog.at_level(logging.WARNING, logger="btctrader.ledger.sync"):
        fills = fills_from_freqtrade_db(ft_db, ACCOUNT)
    warned = [r.message for r in caplog.records if "eaten into dust" in r.message]
    assert len(warned) == 2 and all("dry_run_buy_" in w for w in warned)
    assert all(f.fee_currency == "EUR" for f in fills)


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
    assert fill.ts_utc == "2026-01-01T00:00:00.000000Z"
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
    assert exchange.calls[2] == (None, 2, base + 1000 - 1)  # short page ends the backward walk
    assert exchange.calls[3] == (base + 4000 + 1, 2, None)  # forward probe above the newest trade seen
    assert len(exchange.calls) == 4


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


class OldestFirstExchange(FakeExchange):
    """Same endpoint, but the server hands out the *oldest* ``limit`` trades of the window."""

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):  # type: ignore[no-untyped-def]
        until = (params or {}).get("until")
        self.calls.append((since, limit, until))
        rows = [
            t
            for t in sorted(self.trades, key=lambda t: t["timestamp"])
            if (since is None or t["timestamp"] >= since) and (until is None or t["timestamp"] <= until)
        ]
        return rows[: limit or 500]


def test_fetch_exchange_fills_is_complete_when_server_returns_oldest_first() -> None:
    base = 1_767_225_600_000
    trades = [_ccxt_trade(f"f{i}", base + i * 1000, "buy", "0.001", "60000", "0.15") for i in range(7)]
    exchange = OldestFirstExchange(trades)
    fills = fetch_exchange_fills(exchange, ACCOUNT, since_ms=None, page_limit=3)
    assert [f.exchange_trade_id for f in fills] == [f"f{i}" for i in range(7)]
    # Backward walk gets f0..f2, finds nothing older, then the forward pass fetches the rest.
    assert exchange.calls[0] == (None, 3, None)
    assert exchange.calls[1] == (None, 3, base - 1)
    assert exchange.calls[2] == (base + 2000 + 1, 3, None)


def test_same_second_fills_are_ordered_by_time(ledger_conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A buy at .000 and a sell at .250 of the same second: buy first (string order said otherwise)."""
    settings = _settings(
        tmp_path, LEDGER_SOURCE="exchange", BITVAVO_API_KEY_RO="k", BITVAVO_API_SECRET_RO="s"
    )
    base = 1_767_225_600_000
    exchange = FakeExchange(
        [
            _ccxt_trade("buy1", base, "buy", "0.02", "50000", "2.5"),
            _ccxt_trade("sell1", base + 250, "sell", "0.01", "50100", "1.25"),
        ]
    )
    result = run_sync(ledger_conn, settings, exchange=exchange)
    assert result.fifo_error is None and result.fifo is not None
    assert result.fifo.btc_held == Decimal("0.01")
    fills = load_fills(ledger_conn, ACCOUNT)
    assert [f.ts_utc for f in fills] == ["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.250000Z"]
    assert result.last_fill_ts == "2026-01-01T00:00:00.250000Z"


def test_run_sync_refuses_to_mix_dry_run_and_live_fills(
    ft_db: Path, ledger_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Go-live with the same LEDGER_ACCOUNT_ID must fail instead of merging simulated lots."""
    run_sync(ledger_conn, _settings(tmp_path, FT_DB_PATH=str(ft_db)))
    live = _settings(tmp_path, LEDGER_SOURCE="exchange", BITVAVO_API_KEY_RO="k", BITVAVO_API_SECRET_RO="s")
    exchange = FakeExchange([_ccxt_trade("f1", 1_767_225_600_000, "buy", "0.02", "50000", "2.5")])
    with pytest.raises(LedgerError, match="another kind.*LEDGER_ACCOUNT_ID"):
        run_sync(ledger_conn, live, exchange=exchange)
    assert exchange.calls == []  # refused before touching the exchange
    assert ledger_conn.execute("SELECT COUNT(*) FROM fills WHERE dry_run = 0").fetchone()[0] == 0
    # The other direction is refused as well.
    other = _settings(
        tmp_path,
        LEDGER_ACCOUNT_ID="live",
        LEDGER_SOURCE="exchange",
        BITVAVO_API_KEY_RO="k",
        BITVAVO_API_SECRET_RO="s",
    )
    run_sync(ledger_conn, other, exchange=exchange)
    with pytest.raises(LedgerError, match="another kind"):
        run_sync(ledger_conn, _settings(tmp_path, LEDGER_ACCOUNT_ID="live", FT_DB_PATH=str(ft_db)))


def test_run_sync_records_fifo_error_and_keeps_previous_lots(
    ft_db: Path, ledger_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, FT_DB_PATH=str(ft_db))
    run_sync(ledger_conn, settings)
    assert ledger_conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 3
    # A sell before any buy (e.g. BTC deposited from elsewhere) makes the FIFO fail.
    store(ledger_conn, [make_fill("stray-sell", "2026-01-05", "sell", "0.001", "40000")])
    result = run_sync(ledger_conn, settings)
    assert result.fifo is None and result.fifo_error is not None
    assert "exceeds lots held" in result.fifo_error
    assert get_state(ledger_conn, STATE_FIFO_ERROR) == result.fifo_error
    assert get_state(ledger_conn, STATE_FIFO_ERROR_UTC)
    # Fills are stored, the previous FIFO tables are untouched, last_fill_ts still advances.
    assert ledger_conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 6
    assert ledger_conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 3
    assert result.last_fill_ts == "2026-05-02T00:05:00.000000Z"
    # Removing the cause (here: a matching buy) clears the error on the next sync.
    store(ledger_conn, [make_fill("stray-buy", "2026-01-04", "buy", "0.001", "39000")])
    fixed = run_sync(ledger_conn, settings)
    assert fixed.fifo_error is None and fixed.fifo is not None and len(fixed.fifo.lots) == 4
    assert get_state(ledger_conn, STATE_FIFO_ERROR) is None
    assert get_state(ledger_conn, STATE_FIFO_ERROR_UTC) is None


def test_ccxt_errors_become_ledger_errors() -> None:
    """Network, rate-limit and auth failures of ccxt end as LedgerError (one 'Fehler:' line, no traceback)."""
    import ccxt

    from btctrader.ledger.snapshot import balances_from_exchange

    class Broken:
        def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):  # type: ignore[no-untyped-def]
            raise ccxt.NetworkError("bitvavo GET /trades timed out")

        def fetch_balance(self, params=None):  # type: ignore[no-untyped-def]
            raise ccxt.AuthenticationError("bitvavo 403 invalid key")

    with pytest.raises(LedgerError, match="NetworkError.*timed out"):
        fetch_exchange_fills(Broken(), ACCOUNT)
    with pytest.raises(LedgerError, match="AuthenticationError"):
        balances_from_exchange(Broken())
