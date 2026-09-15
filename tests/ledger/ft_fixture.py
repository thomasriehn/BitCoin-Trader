"""Tiny Freqtrade dry-run database for tests.

``freqtrade`` is not importable from the project venv, so the schema below is
hand-written and mirrors the DDL that Freqtrade 2026.8
(``freqtrade/persistence/trade_model.py``, SQLAlchemy 2.0) generates for the
``trades`` and ``orders`` tables: same column names, types and NOT NULL
constraints for every column the ledger reads, datetimes stored as naive UTC
``YYYY-MM-DD HH:MM:SS.ffffff`` text.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

FT_SCHEMA = """
CREATE TABLE trades (
    id INTEGER NOT NULL,
    exchange VARCHAR(25) NOT NULL,
    pair VARCHAR(25) NOT NULL,
    base_currency VARCHAR(25),
    stake_currency VARCHAR(25),
    is_open BOOLEAN NOT NULL,
    fee_open FLOAT NOT NULL,
    fee_open_cost FLOAT,
    fee_open_currency VARCHAR(25),
    fee_close FLOAT NOT NULL,
    fee_close_cost FLOAT,
    fee_close_currency VARCHAR(25),
    open_rate FLOAT NOT NULL,
    open_rate_requested FLOAT,
    open_trade_value FLOAT,
    close_rate FLOAT,
    close_rate_requested FLOAT,
    realized_profit FLOAT,
    close_profit FLOAT,
    close_profit_abs FLOAT,
    stake_amount FLOAT NOT NULL,
    max_stake_amount FLOAT,
    amount FLOAT NOT NULL,
    amount_requested FLOAT,
    open_date DATETIME NOT NULL,
    close_date DATETIME,
    stop_loss FLOAT,
    stop_loss_pct FLOAT,
    initial_stop_loss FLOAT,
    initial_stop_loss_pct FLOAT,
    is_stop_loss_trailing BOOLEAN NOT NULL,
    max_rate FLOAT,
    min_rate FLOAT,
    exit_reason VARCHAR(255),
    exit_order_status VARCHAR(100),
    strategy VARCHAR(100),
    enter_tag VARCHAR(255),
    timeframe INTEGER,
    trading_mode VARCHAR(7),
    amount_precision FLOAT,
    price_precision FLOAT,
    precision_mode INTEGER,
    precision_mode_price INTEGER,
    contract_size FLOAT,
    leverage FLOAT,
    is_short BOOLEAN NOT NULL,
    liquidation_price FLOAT,
    interest_rate FLOAT NOT NULL,
    funding_fees FLOAT,
    funding_fee_running FLOAT,
    record_version INTEGER NOT NULL,
    PRIMARY KEY (id)
);
CREATE TABLE orders (
    id INTEGER NOT NULL,
    ft_trade_id INTEGER NOT NULL,
    ft_order_side VARCHAR(25) NOT NULL,
    ft_pair VARCHAR(25) NOT NULL,
    ft_is_open BOOLEAN NOT NULL,
    ft_amount FLOAT NOT NULL,
    ft_price FLOAT NOT NULL,
    ft_cancel_reason VARCHAR(255),
    order_id VARCHAR(255) NOT NULL,
    status VARCHAR(255),
    symbol VARCHAR(25),
    order_type VARCHAR(50),
    side VARCHAR(25),
    price FLOAT,
    average FLOAT,
    amount FLOAT,
    filled FLOAT,
    remaining FLOAT,
    cost FLOAT,
    stop_price FLOAT,
    order_date DATETIME,
    order_filled_date DATETIME,
    order_update_date DATETIME,
    funding_fee FLOAT,
    ft_fee_base FLOAT,
    ft_order_tag VARCHAR(255),
    PRIMARY KEY (id),
    CONSTRAINT _order_pair_order_id UNIQUE (ft_pair, order_id),
    FOREIGN KEY(ft_trade_id) REFERENCES trades (id)
);
"""


def create_ft_db(path: Path) -> Path:
    """Two trades: one closed (buy + partial exit + final exit) and one open with a pending order."""
    conn = sqlite3.connect(path)
    conn.executescript(FT_SCHEMA)
    trades = [
        # id, is_open, fee_open, fee_close, open_rate, stake, amount, open_date, close_date, exit_reason,
        # strategy, enter_tag
        (
            1,
            0,
            0.0025,
            0.0025,
            40000.0,
            400.0,
            0.01,
            "2026-03-01 00:05:00.000000",
            "2026-04-01 00:05:00.000000",
            "exit_signal",
            "BtcTrend",
            "trend_up",
        ),
        (
            2,
            1,
            0.0015,
            0.0015,
            45000.0,
            450.0,
            0.01,
            "2026-05-01 00:05:00.000000",
            None,
            None,
            "BtcTrend",
            "trend_up",
        ),
    ]
    conn.executemany(
        "INSERT INTO trades (id, exchange, pair, base_currency, stake_currency, is_open, fee_open, "
        "fee_close, open_rate, stake_amount, amount, open_date, close_date, exit_reason, strategy, "
        "enter_tag, is_stop_loss_trailing, is_short, interest_rate, record_version, leverage, trading_mode) "
        "VALUES (?, 'bitvavo', 'BTC/EUR', 'BTC', 'EUR', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0.0, 2, 1.0, "
        "'SPOT')",
        trades,
    )
    orders = [
        # id, trade, side, is_open, order_id, status, price, average, amount, filled, cost, filled_date, tag
        (
            1,
            1,
            "buy",
            0,
            "dry_run_buy_1",
            "closed",
            40000.0,
            40000.0,
            0.01,
            0.01,
            400.0,
            "2026-03-01 00:05:00.000000",
            None,
        ),
        (
            2,
            1,
            "sell",
            0,
            "dry_run_sell_1",
            "closed",
            42000.0,
            42000.0,
            0.004,
            0.004,
            168.0,
            "2026-03-15 00:05:00.000000",
            "partial_exit",
        ),
        (
            3,
            1,
            "sell",
            0,
            "dry_run_sell_2",
            "closed",
            44000.0,
            44000.0,
            0.006,
            0.006,
            264.0,
            "2026-04-01 00:05:00.000000",
            None,
        ),
        (
            4,
            2,
            "buy",
            0,
            "dry_run_buy_2",
            "closed",
            45000.0,
            45000.5,
            0.01,
            0.01,
            450.005,
            "2026-05-01 00:05:00.000000",
            None,
        ),
        # open (unfilled) order must be ignored
        (5, 2, "sell", 1, "dry_run_sell_3", "open", 50000.0, None, 0.01, 0.0, None, None, None),
        # cancelled order with zero fill must be ignored
        (6, 2, "buy", 0, "dry_run_buy_3", "canceled", 44000.0, None, 0.01, 0.0, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO orders (id, ft_trade_id, ft_order_side, ft_is_open, order_id, status, price, average, "
        "amount, filled, cost, order_filled_date, ft_order_tag, ft_pair, ft_amount, ft_price, symbol, "
        "order_type, side, order_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BTC/EUR', 0.01, 1.0, "
        "'BTC/EUR', 'limit', ?, '2026-01-01 00:00:00.000000')",
        [(*o, o[2]) for o in orders],
    )
    conn.commit()
    conn.close()
    return path
