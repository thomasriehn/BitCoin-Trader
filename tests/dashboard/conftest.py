"""Fixtures for the dashboard tests: a small ledger built with the ledger's own functions,
advisor and guard files written with the common helpers, and a FastAPI TestClient."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btctrader.common.config import Settings
from btctrader.common.db import connect
from btctrader.common.jsonl import append_jsonl, atomic_write_json
from btctrader.dashboard.app import create_app
from btctrader.ledger.fifo import rebuild
from btctrader.ledger.fills import Fill, upsert_fills

ACCOUNT = "test-account"
FT_URL = "http://127.0.0.1:8080"
NOW = "2026-09-15T12:00:00Z"


def make_fill(
    trade_id: str,
    ts: str,
    side: str,
    amount: str,
    price: str,
    fee: str = "0",
    fee_currency: str = "EUR",
    *,
    maker_taker: str | None = None,
    signal_reason: str | None = None,
) -> Fill:
    if len(ts) == 10:
        ts = ts + "T12:00:00Z"
    return Fill.build(
        source="freqtrade-db",
        account_id=ACCOUNT,
        exchange_trade_id=trade_id,
        ts_utc=ts,
        side=side,
        amount_btc=Decimal(amount),
        price_eur=Decimal(price),
        fee_amount=Decimal(fee),
        fee_currency=fee_currency,
        maker_taker=maker_taker,
        dry_run=True,
        signal_reason=signal_reason,
    )


# Fixture ledger (all times 12:00 UTC, "now" is 2026-09-15):
#   b1 2025-01-15 buy 0.02 @ 50000, fee 2.50  -> lot 1, cost 1002.50, tax-free from 2026-01-16
#   b2 2025-09-01 buy 0.01 @ 60000, fee 0.90  -> lot 2, cost 600.90,  tax-free from 2026-09-02 (already free)
#   s1 2026-01-10 sell 0.01 @ 80000, fee 2.00 -> lot 1, 360 days, taxable: 800 - 501.25 - 2 = 296.75
#   s2 2026-02-01 sell 0.01 @ 90000, fee 2.25 -> lot 1, > 1 year, not taxable: 900 - 501.25 - 2.25 = 396.50
#   b3 2026-06-01 buy 0.01 @ 70000, fee 1.05  -> lot 3, cost 701.05, tax-free from 2027-06-02
#   b4 2026-09-10 buy 0.001 @ 60000, fee 0.15 -> lot 4, cost 60.15,  tax-free from 2027-09-11
#   expense 2026-02-15: 9.99 (VPS) -> section 23 gain 2026 = 296.75 - 9.99 = 286.76
FIXTURE_FILLS = [
    make_fill("b1", "2025-01-15", "buy", "0.02", "50000", "2.5"),
    make_fill("b2", "2025-09-01", "buy", "0.01", "60000", "0.9"),
    make_fill(
        "s1", "2026-01-10", "sell", "0.01", "80000", "2", maker_taker="maker", signal_reason="exit_signal"
    ),
    make_fill("s2", "2026-02-01", "sell", "0.01", "90000", "2.25", maker_taker="taker"),
    make_fill("b3", "2026-06-01", "buy", "0.01", "70000", "1.05", signal_reason="trend_up"),
    make_fill("b4", "2026-09-10", "buy", "0.001", "60000", "0.15", maker_taker="maker"),
]

EQUITY_ROWS = [
    # date, equity, bh, dca, fees_cum, gain_ytd, price
    ("2026-09-10", "1000", "1000", "1000", "8.85", "286.76", "60000"),
    ("2026-09-11", "1100", "1200", "1010", "8.85", "286.76", "62000"),
    ("2026-09-12", "990", "900", "1005", "8.85", "286.76", "55000"),
    ("2026-09-13", "1050", "1000", "1020", "8.85", "286.76", "58000"),
    ("2026-09-14", "1200", "1300", "1050", "8.85", "286.76", "65000"),
]


def seed_ledger(path: Path, fills: list[Fill] | None = None, *, equity: bool = True) -> None:
    conn = connect(path)
    try:
        upsert_fills(conn, FIXTURE_FILLS if fills is None else fills)
        rebuild(conn, ACCOUNT)
        with conn:
            if fills is None:
                conn.execute(
                    "INSERT INTO expenses (date_utc, amount_eur, description) "
                    "VALUES ('2026-02-15', '9.99', 'VPS')"
                )
            if equity:
                conn.executemany(
                    "INSERT INTO equity_daily (date_utc, eur_balance, btc_balance, btc_price_eur, "
                    "equity_eur, bh_equity_eur, dca_equity_eur, fees_cum_eur, realized_gain_ytd_eur, "
                    "dry_run) "
                    "VALUES (?, '0', '0', ?, ?, ?, ?, ?, ?, 1)",
                    [
                        (d, price, eq, bh, dca, fees, gain)
                        for d, eq, bh, dca, fees, gain, price in EQUITY_ROWS
                    ],
                )
    finally:
        conn.close()


def seed_guard(guard_dir: Path, *, killswitch: bool = False) -> None:
    atomic_write_json(
        guard_dir / "state.json",
        {
            "schema_version": 1,
            "day_key": "2026-09-15",
            "day_start_equity": "1200.00",
            "peak_equity": "1250.00",
            "last_equity": "1190.00",
            "last_check": "2026-09-15T11:59:00Z",
            "ft_failures": 0,
        },
    )
    for i, name in enumerate(["day_start", "ft_unreachable", "daily_loss"]):
        append_jsonl(
            guard_dir / "events.jsonl",
            {"ts": f"2026-09-15T0{i}:00:00Z", "event": name, "details": {"n": i}},
        )
    if killswitch:
        atomic_write_json(
            guard_dir / "killswitch.lock",
            {
                "ts": "2026-09-15T10:00:00Z",
                "equity_eur": "950.00",
                "peak_equity_eur": "1250.00",
                "reason": "test",
            },
        )


def seed_advisor(advisor_dir: Path, *, valid_until: str = "2026-09-15T13:07:02Z") -> None:
    decision = {
        "schema_version": 1,
        "decision_id": "2026-09-15T11:07:02Z-a1b2c3",
        "created_at": "2026-09-15T11:07:02Z",
        "valid_until": valid_until,
        "mode": "shadow",
        "model": "Qwen/Qwen3-14B",
        "regime": "neutral",
        "confidence": 0.55,
        "horizon_days": 7,
        "rationale": "Seitwärts.",
        "key_factors": ["SMA200 flach"],
        "context_hash": "sha256:abc",
        "prompt_hash": "sha256:def",
    }
    atomic_write_json(advisor_dir / "decision.json", decision)
    append_jsonl(
        advisor_dir / "decisions.jsonl",
        {
            **decision,
            "latency_ms": 1234,
            "usage": {"prompt_tokens": 10},
            "raw_response": "{...}",
            "error": None,
        },
    )
    append_jsonl(
        advisor_dir / "decisions.jsonl",
        {
            "schema_version": 1,
            "decision_id": None,
            "created_at": "2026-09-15T12:07:02Z",
            "mode": "shadow",
            "model": "Qwen/Qwen3-14B",
            "error": "timeout after 120s",
        },
    )


@pytest.fixture
def env(tmp_path: Path) -> Settings:
    """Settings pointing at a fully seeded temp environment."""
    seed_ledger(tmp_path / "ledger.sqlite")
    seed_guard(tmp_path / "guard")
    seed_advisor(tmp_path / "advisor")
    return Settings(
        ledger_db_path=tmp_path / "ledger.sqlite",
        ledger_account_id=ACCOUNT,
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
        ft_api_url=FT_URL,
        ft_api_user="bot",
        ft_api_pass="pw",
        tz_display="Europe/Berlin",
    )


@pytest.fixture
def client(env: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(env)) as c:
        yield c


@pytest.fixture
def ledger_conn(env: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(env.ledger_db_path)
    yield conn
    conn.close()
