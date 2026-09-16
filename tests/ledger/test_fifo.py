"""FIFO lots and disposals, BTC fee mini disposals and the one-year holding rule."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from btctrader.ledger.errors import FifoError
from btctrader.ledger.fifo import compute_fifo, holding_status, load_disposals, load_lots, rebuild
from btctrader.ledger.fills import Fill, compute_row_hash, load_fills, upsert_fills
from tests.ledger.conftest import ACCOUNT, make_fill, store


def test_partial_lot_consumption_across_several_sells(ledger_conn: sqlite3.Connection) -> None:
    # Two lots: 0.5 BTC @ 20000 (fee 10 EUR), 0.5 BTC @ 30000 (fee 15 EUR).
    fills = [
        make_fill("b1", "2026-01-10", "buy", "0.5", "20000", "10"),
        make_fill("b2", "2026-02-10", "buy", "0.5", "30000", "15"),
        make_fill("s1", "2026-03-10", "sell", "0.3", "40000", "12"),  # eats 0.3 of lot 1
        make_fill("s2", "2026-04-10", "sell", "0.4", "50000", "20"),  # eats 0.2 of lot 1 + 0.2 of lot 2
        make_fill("s3", "2026-05-10", "sell", "0.3", "60000", "18"),  # eats the rest of lot 2
    ]
    store(ledger_conn, fills)
    result = rebuild(ledger_conn, ACCOUNT)

    assert [lot.remaining_qty for lot in result.lots] == [Decimal(0), Decimal(0)]
    assert result.lots[0].cost_eur_incl_fees == Decimal("10010")
    assert result.lots[1].cost_eur_incl_fees == Decimal("15015")
    assert result.btc_held == 0

    disposals = load_disposals(ledger_conn, ACCOUNT)
    assert [(d.kind, d.lot_id, d.qty_btc) for d in disposals] == [
        ("sell", 1, Decimal("0.3")),
        ("sell", 1, Decimal("0.2")),
        ("sell", 2, Decimal("0.2")),
        ("sell", 2, Decimal("0.3")),
    ]
    d1, d2a, d2b, d3 = disposals
    # s1: proceeds 12000, cost 0.3/0.5 x 10010 = 6006, fee 12
    assert (d1.proceeds_eur, d1.cost_eur, d1.sell_fee_eur, d1.gain_eur) == (
        Decimal("12000"),
        Decimal("6006"),
        Decimal("12"),
        Decimal("5982"),
    )
    # s2: 20000 gross split 0.2/0.4 each, fee 20 split 10/10, costs 4004 (rest of lot 1) and 6006
    assert d2a.proceeds_eur + d2b.proceeds_eur == Decimal("20000")
    assert d2a.sell_fee_eur + d2b.sell_fee_eur == Decimal("20")
    assert d2a.cost_eur == Decimal("4004")
    assert d2b.cost_eur == Decimal("6006")
    # s3: rest of lot 2 = 15015 - 6006 = 9009
    assert d3.cost_eur == Decimal("9009")
    assert d3.gain_eur == Decimal("18000") - Decimal("9009") - Decimal("18")
    # Totals are exact: all cost consumed, all proceeds and fees allocated.
    assert sum(d.cost_eur for d in disposals) == Decimal("25025")
    assert sum(d.proceeds_eur for d in disposals) == Decimal("50000")
    assert sum(d.sell_fee_eur for d in disposals) == Decimal("50")
    assert all(d.taxable == 1 and d.boundary_case == 0 for d in disposals)
    assert d1.holding_days == 59


def test_fee_in_btc_creates_fee_disposal(ledger_conn: sqlite3.Connection) -> None:
    fills = [
        make_fill("b1", "2026-01-10", "buy", "1", "10000", "0.001", "BTC"),
        make_fill("s1", "2026-02-10", "sell", "0.5", "20000", "0.0005", "BTC"),
    ]
    store(ledger_conn, fills)
    result = rebuild(ledger_conn, ACCOUNT)
    stored = load_fills(ledger_conn, ACCOUNT)
    buy, sell = stored
    assert buy.fee_eur == Decimal("10")  # 0.001 x 10000
    assert buy.net_eur == Decimal("10010")
    assert sell.fee_eur == Decimal("10")  # 0.0005 x 20000
    assert sell.net_eur == Decimal("9990")

    disposals = result.disposals
    assert [d.kind for d in disposals] == ["fee", "sell", "fee"]
    fee_buy, sale, fee_sell = disposals
    assert fee_buy.qty_btc == Decimal("0.001")
    assert fee_buy.proceeds_eur == Decimal("10")
    assert fee_buy.sell_fee_eur == 0
    assert fee_buy.cost_eur == Decimal("10.01")  # 0.001/1 x 10010
    assert fee_buy.sell_fill_id == buy.id
    assert sale.qty_btc == Decimal("0.5")
    assert sale.proceeds_eur == Decimal("10000")
    assert sale.sell_fee_eur == Decimal("10")
    assert fee_sell.qty_btc == Decimal("0.0005")
    assert fee_sell.proceeds_eur == Decimal("10")
    assert fee_sell.sell_fill_id == sell.id
    # 1 - 0.001 - 0.5 - 0.0005 remaining
    assert result.btc_held == Decimal("0.4985")
    lots = load_lots(ledger_conn, ACCOUNT, open_only=True)
    assert len(lots) == 1 and lots[0].remaining_qty == Decimal("0.4985")
    # The sum over all gains equals cash flow: -10000 (net of BTC fee) ... check conservation:
    # total proceeds (10 + 10000 + 10) - consumed cost - sell fee = gain
    total_gain = sum(d.gain_eur for d in disposals)
    consumed_cost = sum(d.cost_eur for d in disposals)
    assert total_gain == Decimal("10020") - consumed_cost - Decimal("10")


@pytest.mark.parametrize(
    ("acquired", "disposed", "taxable", "boundary"),
    [
        (date(2024, 2, 29), date(2025, 2, 28), True, True),  # leap day + 1 year clamps to 28 Feb
        (date(2024, 2, 29), date(2025, 3, 1), False, False),
        (date(2025, 6, 10), date(2026, 6, 10), True, True),  # anniversary is still taxable
        (date(2025, 6, 10), date(2026, 6, 11), False, False),
        (date(2023, 6, 10), date(2024, 6, 10), True, True),  # 366 days across a leap year
        (date(2023, 6, 10), date(2024, 6, 9), True, False),
        (date(2026, 1, 1), date(2026, 1, 1), True, False),
    ],
)
def test_holding_status(acquired: date, disposed: date, taxable: bool, boundary: bool) -> None:
    assert holding_status(acquired, disposed) == (taxable, boundary)


def test_taxable_flags_end_to_end(ledger_conn: sqlite3.Connection) -> None:
    fills = [
        make_fill("b1", "2024-02-29T10:00:00Z", "buy", "0.2", "50000"),
        make_fill("s1", "2025-02-28T23:59:59Z", "sell", "0.1", "60000"),
        make_fill("s2", "2025-03-01T00:00:01Z", "sell", "0.1", "60000"),
        make_fill("b2", "2025-06-10T08:00:00Z", "buy", "0.1", "50000"),
        make_fill("s3", "2026-06-10T20:00:00Z", "sell", "0.1", "70000"),
        make_fill("b3", "2023-06-10", "buy", "0.1", "20000"),
        make_fill("s4", "2024-06-10", "sell", "0.1", "30000"),
    ]
    store(ledger_conn, fills)
    result = rebuild(ledger_conn, ACCOUNT)
    by_fill = {d.sell_fill_id: d for d in result.disposals}
    ids = {f.exchange_trade_id: f.id for f in load_fills(ledger_conn, ACCOUNT)}
    assert (
        by_fill[ids["s4"]].taxable,
        by_fill[ids["s4"]].boundary_case,
        by_fill[ids["s4"]].holding_days,
    ) == (
        1,
        1,
        366,
    )
    assert (by_fill[ids["s1"]].taxable, by_fill[ids["s1"]].boundary_case) == (1, 1)
    assert (by_fill[ids["s2"]].taxable, by_fill[ids["s2"]].boundary_case) == (0, 0)
    assert (by_fill[ids["s3"]].taxable, by_fill[ids["s3"]].boundary_case) == (1, 1)
    lots = load_lots(ledger_conn, ACCOUNT)
    assert all(lot.pre_2027 == 1 for lot in lots)
    assert lots[0].free_from == date(2024, 6, 11)


def test_rebuild_is_deterministic_and_idempotent(ledger_conn: sqlite3.Connection) -> None:
    fills = [
        make_fill("b1", "2026-01-10", "buy", "0.5", "20000", "10"),
        make_fill("s1", "2026-03-10", "sell", "0.3", "40000", "12"),
    ]
    store(ledger_conn, fills)
    first = rebuild(ledger_conn, ACCOUNT)
    second = rebuild(ledger_conn, ACCOUNT)
    assert first == second
    assert ledger_conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 1
    assert ledger_conn.execute("SELECT COUNT(*) FROM disposals").fetchone()[0] == 1
    pure = compute_fifo(load_fills(ledger_conn, ACCOUNT), ACCOUNT)
    assert pure == second


def test_sell_without_lots_raises(ledger_conn: sqlite3.Connection) -> None:
    store(ledger_conn, [make_fill("s1", "2026-03-10", "sell", "0.3", "40000")])
    with pytest.raises(FifoError, match="exceeds lots held"):
        rebuild(ledger_conn, ACCOUNT)


def test_other_accounts_are_isolated(ledger_conn: sqlite3.Connection) -> None:
    store(
        ledger_conn,
        [
            make_fill("b1", "2026-01-10", "buy", "0.5", "20000"),
            make_fill("b1", "2026-01-10", "buy", "0.7", "20000", account="other"),
        ],
    )
    rebuild(ledger_conn, ACCOUNT)
    rebuild(ledger_conn, "other")
    lots = load_lots(ledger_conn, ACCOUNT)
    others = load_lots(ledger_conn, "other")
    assert len(lots) == 1 and lots[0].qty_btc == Decimal("0.5")
    assert len(others) == 1 and others[0].qty_btc == Decimal("0.7")
    assert lots[0].lot_id != others[0].lot_id


def test_legacy_whole_second_rows_sort_by_time_not_by_string(ledger_conn: sqlite3.Connection) -> None:
    """Rows stored before ts_utc had a fixed precision ('...:00Z') sort after '...:00.250000Z' as
    strings although they are earlier; ordering, FIFO and idempotent re-sync must still be right."""
    legacy = make_fill("b1", "2026-01-01T10:00:00.000000Z", "buy", "0.02", "50000")
    legacy = Fill(**{**{f: getattr(legacy, f) for f in legacy.__slots__}, "ts_utc": "2026-01-01T10:00:00Z"})
    assert legacy.ts_utc == "2026-01-01T10:00:00Z"
    upsert_fills(ledger_conn, [legacy])
    store(ledger_conn, [make_fill("s1", "2026-01-01T10:00:00.250000Z", "sell", "0.01", "50100")])
    stored = load_fills(ledger_conn, ACCOUNT)
    assert [f.exchange_trade_id for f in stored] == ["b1", "s1"]
    assert stored[0].ts_utc == "2026-01-01T10:00:00Z"  # stored text (and its hash) is untouched
    assert stored[0].row_hash == compute_row_hash(stored[0], None)
    result = rebuild(ledger_conn, ACCOUNT)
    assert result.btc_held == Decimal("0.01") and len(result.disposals) == 1
    # The same fill fetched again (now normalised) is recognised, not flagged as a conflict.
    again = upsert_fills(ledger_conn, [make_fill("b1", "2026-01-01T10:00:00Z", "buy", "0.02", "50000")])
    assert (again.inserted, again.unchanged, again.conflict_count) == (0, 1, 0)
