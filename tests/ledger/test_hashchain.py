"""SHA-256 hash chain over the fills table."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from btctrader.ledger.errors import ChainError
from btctrader.ledger.fills import (
    Fill,
    assert_chain,
    compute_row_hash,
    load_fills,
    upsert_fills,
    verify_chain,
)
from tests.ledger.conftest import ACCOUNT, make_fill


def _three_fills() -> list[Fill]:
    return [
        make_fill("t1", "2026-01-01", "buy", "0.1", "50000", "1"),
        make_fill("t2", "2026-01-02", "sell", "0.05", "51000", "1"),
        make_fill("t3", "2026-01-03", "buy", "0.02", "49000", "0.5"),
    ]


def test_chain_links_rows_and_verifies(ledger_conn: sqlite3.Connection) -> None:
    result = upsert_fills(ledger_conn, _three_fills())
    assert result.inserted == 3
    rows = load_fills(ledger_conn, ACCOUNT)
    assert rows[0].prev_hash is None
    assert rows[1].prev_hash == rows[0].row_hash
    assert rows[2].prev_hash == rows[1].row_hash
    assert rows[0].row_hash == compute_row_hash(rows[0], None)
    assert verify_chain(ledger_conn, ACCOUNT) == []
    assert_chain(ledger_conn, ACCOUNT)


def test_tampering_with_business_field_is_detected(ledger_conn: sqlite3.Connection) -> None:
    upsert_fills(ledger_conn, _three_fills())
    with ledger_conn:
        ledger_conn.execute("UPDATE fills SET amount_btc = '0.2' WHERE exchange_trade_id = 't2'")
    problems = verify_chain(ledger_conn, ACCOUNT)
    assert len(problems) == 1 and "t2" in problems[0] and "row_hash mismatch" in problems[0]
    with pytest.raises(ChainError):
        assert_chain(ledger_conn, ACCOUNT)


def test_deleting_a_row_breaks_the_link(ledger_conn: sqlite3.Connection) -> None:
    upsert_fills(ledger_conn, _three_fills())
    with ledger_conn:
        ledger_conn.execute("DELETE FROM fills WHERE exchange_trade_id = 't2'")
    problems = verify_chain(ledger_conn, ACCOUNT)
    assert any("t3" in p and "prev_hash" in p for p in problems)


def test_deleting_the_newest_row_is_detected(ledger_conn: sqlite3.Connection) -> None:
    """Dropping the tail leaves a valid link chain; the sync_state anchor still catches it."""
    fills = _three_fills()
    upsert_fills(ledger_conn, fills)
    with ledger_conn:
        ledger_conn.execute("DELETE FROM fills WHERE exchange_trade_id = 't3'")
    problems = verify_chain(ledger_conn, ACCOUNT)
    assert len(problems) == 1 and "chain tail mismatch" in problems[0] and "3 rows" in problems[0]
    # A later sync must not quietly re-append the deleted fill with a fresh link.
    with pytest.raises(ChainError, match="chain tail mismatch"):
        upsert_fills(ledger_conn, fills)
    assert ledger_conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_concurrent_upserts_serialise_on_the_write_lock(
    ledger_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A second writer must wait for the first instead of chaining onto a stale prev_hash."""
    path = tmp_path / "ledger.sqlite"
    other = sqlite3.connect(path, timeout=30.0, isolation_level="DEFERRED", check_same_thread=False)
    other.row_factory = sqlite3.Row
    errors: list[BaseException] = []

    def late_writer() -> None:
        try:
            upsert_fills(other, [make_fill("late", "2026-01-02", "buy", "0.1", "50000")])
        except BaseException as exc:  # noqa: BLE001 - surfaced via the assertion below
            errors.append(exc)

    ledger_conn.execute("BEGIN IMMEDIATE")  # hold the write lock while the other sync starts
    thread = threading.Thread(target=late_writer)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive()  # blocked on BEGIN IMMEDIATE, has not read the chain yet
    upsert_fills(ledger_conn, [make_fill("early", "2026-01-01", "buy", "0.1", "50000")])  # commits, unlocks
    thread.join(timeout=30)
    other.close()
    assert not thread.is_alive() and errors == []
    rows = load_fills(ledger_conn, ACCOUNT)
    assert [r.exchange_trade_id for r in rows] == ["early", "late"]
    assert rows[1].prev_hash == rows[0].row_hash
    assert verify_chain(ledger_conn, ACCOUNT) == []


def test_rewriting_hash_without_prev_link_is_detected(ledger_conn: sqlite3.Connection) -> None:
    upsert_fills(ledger_conn, _three_fills())
    rows = load_fills(ledger_conn, ACCOUNT)
    tampered = Fill.build(
        source=rows[1].source,
        account_id=rows[1].account_id,
        exchange_trade_id=rows[1].exchange_trade_id,
        ts_utc=rows[1].ts_utc,
        side=rows[1].side,
        amount_btc="0.2",
        price_eur=rows[1].price_eur,
        fee_amount=rows[1].fee_amount,
        dry_run=rows[1].dry_run,
    )
    # An attacker recomputes the row hash but the following row still points to the old hash.
    new_hash = compute_row_hash(tampered, rows[1].prev_hash)
    with ledger_conn:
        ledger_conn.execute(
            "UPDATE fills SET amount_btc = '0.2', gross_eur = ?, net_eur = ?, row_hash = ? "
            "WHERE exchange_trade_id = 't2'",
            (str(tampered.gross_eur), str(tampered.net_eur), new_hash),
        )
    problems = verify_chain(ledger_conn, ACCOUNT)
    assert len(problems) == 1 and "t3" in problems[0]


def test_upsert_is_idempotent_and_flags_conflicts(ledger_conn: sqlite3.Connection) -> None:
    fills = _three_fills()
    upsert_fills(ledger_conn, fills)
    again = upsert_fills(ledger_conn, fills)
    assert (again.inserted, again.unchanged, again.updated_meta, again.conflict_count) == (0, 3, 0, 0)
    # Metadata may be enriched later without breaking the chain.
    enriched = make_fill(
        "t1", "2026-01-01", "buy", "0.1", "50000", "1", strategy="BtcTrend", signal_reason="x"
    )
    res = upsert_fills(ledger_conn, [enriched])
    assert res.updated_meta == 1
    assert verify_chain(ledger_conn, ACCOUNT) == []
    row = load_fills(ledger_conn, ACCOUNT)[0]
    assert (row.strategy, row.signal_reason) == ("BtcTrend", "x")
    # A changed business field of a known fill is a conflict, not an overwrite.
    changed = make_fill("t1", "2026-01-01", "buy", "0.1", "50001", "1")
    res = upsert_fills(ledger_conn, [changed])
    assert res.conflict_count == 1 and res.inserted == 0
    assert load_fills(ledger_conn, ACCOUNT)[0].price_eur == 50000
    assert verify_chain(ledger_conn, ACCOUNT) == []


def test_chains_are_per_account(ledger_conn: sqlite3.Connection) -> None:
    upsert_fills(ledger_conn, [make_fill("a1", "2026-01-01", "buy", "0.1", "50000")])
    upsert_fills(ledger_conn, [make_fill("b1", "2026-01-01", "buy", "0.1", "50000", account="other")])
    upsert_fills(ledger_conn, [make_fill("a2", "2026-01-02", "buy", "0.1", "50000")])
    mine = load_fills(ledger_conn, ACCOUNT)
    other = load_fills(ledger_conn, "other")
    assert other[0].prev_hash is None
    assert mine[1].prev_hash == mine[0].row_hash
    assert verify_chain(ledger_conn, ACCOUNT) == [] and verify_chain(ledger_conn, "other") == []
