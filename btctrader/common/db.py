"""SQLite access for the ledger database and UTC time helpers.

The schema mirrors docs/KOMPONENTEN.md section 5 exactly (plus indexes).
Only ``btctrader.ledger`` writes to this database; other components open it
read-only through the same ``connect`` helper.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,                 -- 'exchange' | 'freqtrade-db'
  exchange TEXT NOT NULL,               -- 'bitvavo'
  account_id TEXT NOT NULL,             -- LEDGER_ACCOUNT_ID
  exchange_trade_id TEXT NOT NULL,      -- Bitvavo fill id; dry-run 'ft-<order_id>-<n>'
  exchange_order_id TEXT,
  client_order_id TEXT,
  ts_utc TEXT NOT NULL,                 -- ISO-8601 UTC, exchange fill timestamp
  pair TEXT NOT NULL,                   -- 'BTC/EUR'
  side TEXT NOT NULL,                   -- 'buy' | 'sell'
  amount_btc TEXT NOT NULL,             -- Decimal as string, 8 decimals
  price_eur TEXT NOT NULL,
  gross_eur TEXT NOT NULL,              -- amount x price
  fee_amount TEXT NOT NULL,
  fee_currency TEXT NOT NULL,           -- 'EUR' | 'BTC'
  fee_eur TEXT NOT NULL,                -- fee in EUR (BTC fee: fee_amount x price)
  net_eur TEXT NOT NULL,                -- buy: gross + fee_eur ; sell: gross - fee_eur
  maker_taker TEXT,                     -- 'maker' | 'taker' | NULL
  strategy TEXT,
  signal_reason TEXT,                   -- Freqtrade enter_tag / exit_reason
  advisor_decision_id TEXT,
  dry_run INTEGER NOT NULL,             -- 1 in dry-run
  reconciled_with_bot_db INTEGER NOT NULL DEFAULT 0,
  prev_hash TEXT, row_hash TEXT NOT NULL,   -- SHA-256 chain over the business fields
  UNIQUE (exchange, account_id, exchange_trade_id)
);
CREATE TABLE IF NOT EXISTS lots (
  lot_id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  buy_fill_id INTEGER NOT NULL REFERENCES fills(id),
  acquired_at TEXT NOT NULL,
  qty_btc TEXT NOT NULL,
  cost_eur_incl_fees TEXT NOT NULL,     -- acquisition cost incl. buy fee (BMF Rn. 59)
  remaining_qty TEXT NOT NULL,
  pre_2027 INTEGER NOT NULL             -- acquired_at < 2027-01-01
);
CREATE TABLE IF NOT EXISTS disposals (
  id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  sell_fill_id INTEGER NOT NULL REFERENCES fills(id),   -- sell fill or fill with BTC fee
  kind TEXT NOT NULL,                   -- 'sell' | 'fee'
  lot_id INTEGER NOT NULL REFERENCES lots(lot_id),
  qty_btc TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  disposed_at TEXT NOT NULL,
  holding_days INTEGER NOT NULL,        -- informational
  proceeds_eur TEXT NOT NULL,           -- pro-rata proceeds (without sell fee)
  cost_eur TEXT NOT NULL,               -- pro-rata acquisition cost incl. buy fee
  sell_fee_eur TEXT NOT NULL,           -- pro-rata sell fee (Werbungskosten)
  gain_eur TEXT NOT NULL,               -- proceeds - cost - sell_fee
  taxable INTEGER NOT NULL,             -- disposed_date <= acquired_date + 1 year
  boundary_case INTEGER NOT NULL        -- 1 if disposed_date == acquired_date + 1 year
);
CREATE TABLE IF NOT EXISTS transfers (
  id INTEGER PRIMARY KEY, account_id TEXT, ts_utc TEXT, direction TEXT, asset TEXT,
  amount TEXT, tx_hash TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS equity_daily (
  date_utc TEXT PRIMARY KEY,            -- 'YYYY-MM-DD'
  eur_balance TEXT NOT NULL, btc_balance TEXT NOT NULL, btc_price_eur TEXT NOT NULL,
  equity_eur TEXT NOT NULL,             -- eur + btc x price
  bh_equity_eur TEXT NOT NULL,          -- buy-and-hold benchmark
  dca_equity_eur TEXT NOT NULL,         -- virtual weekly DCA
  fees_cum_eur TEXT NOT NULL,
  realized_gain_ytd_eur TEXT NOT NULL,  -- taxable section 23 gain in the current year
  dry_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, date_utc TEXT, amount_eur TEXT, description TEXT, receipt_ref TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_fills_account_ts ON fills (account_id, ts_utc, exchange_trade_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills (ts_utc);
CREATE INDEX IF NOT EXISTS idx_lots_account ON lots (account_id, acquired_at);
CREATE INDEX IF NOT EXISTS idx_lots_buy_fill ON lots (buy_fill_id);
CREATE INDEX IF NOT EXISTS idx_disposals_account_disposed ON disposals (account_id, disposed_at);
CREATE INDEX IF NOT EXISTS idx_disposals_sell_fill ON disposals (sell_fill_id);
CREATE INDEX IF NOT EXISTS idx_disposals_lot ON disposals (lot_id);
CREATE INDEX IF NOT EXISTS idx_transfers_account_ts ON transfers (account_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (date_utc);
"""

TABLES: tuple[str, ...] = (
    "fills",
    "lots",
    "disposals",
    "transfers",
    "equity_daily",
    "expenses",
    "sync_state",
)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and create) the ledger database.

    Creates the parent directory, enables WAL and foreign keys, sets
    ``sqlite3.Row`` as row factory and applies ``SCHEMA_SQL`` (idempotent).
    """
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def iso_utc(dt: datetime) -> str:
    """Format an aware datetime as ISO-8601 UTC with ``Z`` suffix.

    Naive datetimes are treated as UTC. Microseconds are kept only when non-zero.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    timespec = "seconds" if dt.microsecond == 0 else "microseconds"
    return dt.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime.

    Accepts ``Z`` or numeric offsets; a naive value is treated as UTC.
    A date-only string means midnight UTC.
    """
    text = s.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
