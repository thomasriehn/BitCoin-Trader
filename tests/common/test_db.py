"""Tests for btctrader.common.db."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from btctrader.common.db import SCHEMA_SQL, TABLES, connect, iso_utc, parse_iso, utcnow


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_connect_creates_schema_wal_and_parent_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "ledger.sqlite"
    conn = connect(db_path)
    try:
        assert db_path.exists()
        assert set(TABLES) <= _tables(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.row_factory is sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(fills)")}
        assert {"exchange_trade_id", "row_hash", "prev_hash", "net_eur", "dry_run"} <= cols
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(fills)")}
        assert "idx_fills_account_ts" in idx
    finally:
        conn.close()


def test_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    conn = connect(db_path)
    conn.execute("INSERT INTO sync_state (key, value) VALUES ('last_fill_ts', 'x')")
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    try:
        conn2.executescript(SCHEMA_SQL)  # applying again must not fail or wipe data
        assert conn2.execute("SELECT value FROM sync_state WHERE key='last_fill_ts'").fetchone()[0] == "x"
    finally:
        conn2.close()


def test_schema_contains_only_if_not_exists() -> None:
    for line in SCHEMA_SQL.splitlines():
        if line.startswith("CREATE"):
            assert "IF NOT EXISTS" in line


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = connect(tmp_path / "l.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lots (account_id, buy_fill_id, acquired_at, qty_btc, cost_eur_incl_fees,"
                " remaining_qty, pre_2027) VALUES ('a', 999, '2026-01-01T00:00:00Z', '1', '1', '1', 1)"
            )
    finally:
        conn.close()


def test_unique_fill_constraint(tmp_path: Path) -> None:
    conn = connect(tmp_path / "l.sqlite")
    row = (
        "freqtrade-db", "bitvavo", "acc", "ft-1-1", "2026-01-01T00:00:00Z", "BTC/EUR", "buy",
        "0.01000000", "60000", "600", "0.9", "EUR", "0.9", "600.9", 1, "h",
    )
    sql = (
        "INSERT INTO fills (source, exchange, account_id, exchange_trade_id, ts_utc, pair, side,"
        " amount_btc, price_eur, gross_eur, fee_amount, fee_currency, fee_eur, net_eur, dry_run, row_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    try:
        conn.execute(sql, row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, row)
    finally:
        conn.close()


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_iso_utc_and_parse_iso_roundtrip() -> None:
    dt = datetime(2026, 9, 15, 13, 7, 2, tzinfo=UTC)
    assert iso_utc(dt) == "2026-09-15T13:07:02Z"
    assert parse_iso("2026-09-15T13:07:02Z") == dt
    assert parse_iso(iso_utc(dt)) == dt
    with_us = dt.replace(microsecond=123456)
    assert iso_utc(with_us) == "2026-09-15T13:07:02.123456Z"
    assert parse_iso(iso_utc(with_us)) == with_us


def test_iso_utc_converts_offsets_and_naive() -> None:
    berlin = datetime(2026, 9, 15, 15, 7, 2, tzinfo=timezone(timedelta(hours=2)))
    assert iso_utc(berlin) == "2026-09-15T13:07:02Z"
    assert iso_utc(datetime(2026, 1, 1, 0, 0, 0)) == "2026-01-01T00:00:00Z"


def test_parse_iso_variants() -> None:
    expected = datetime(2026, 9, 15, 13, 7, 2, tzinfo=UTC)
    assert parse_iso("2026-09-15T15:07:02+02:00") == expected
    assert parse_iso("2026-09-15 13:07:02") == expected
    assert parse_iso("2026-09-15") == datetime(2026, 9, 15, tzinfo=UTC)
    assert parse_iso("2026-09-15T13:07:02.500Z").microsecond == 500000
