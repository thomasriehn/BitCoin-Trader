"""Deterministic FIFO rebuild of lots and disposals per account (section 23 EStG).

Rules (docs/KOMPONENTEN.md section 5):

* Fills are processed in order ``(ts_utc, exchange_trade_id)``. Every buy opens
  a lot with ``cost_eur_incl_fees = net_eur`` (gross plus buy fee, BMF Rn. 59).
* A sell consumes the oldest lots first. Proceeds (gross, without the sell
  fee), acquisition cost and the sell fee (Werbungskosten) are split pro rata
  over the consumed lots; ``gain = proceeds - cost - sell_fee``.
* A fee paid in BTC is a mini disposal (``kind='fee'``) of ``fee_amount`` BTC
  with ``proceeds = fee_btc x price`` against the oldest lots.
* ``taxable = disposed_date <= acquired_date + relativedelta(years=1)`` on UTC
  calendar dates; ``boundary_case = 1`` when the disposal falls exactly on the
  anniversary.

All arithmetic uses ``decimal.Decimal``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from btctrader.common.db import parse_iso
from btctrader.ledger.errors import FifoError
from btctrader.ledger.fills import Fill, begin_immediate, fmt, load_fills, q8, sort_key

PRE_2027_CUTOFF = date(2027, 1, 1)
ZERO = Decimal(0)


@dataclass(slots=True)
class Lot:
    lot_id: int
    account_id: str
    buy_fill_id: int
    acquired_at: str
    qty_btc: Decimal
    cost_eur_incl_fees: Decimal
    remaining_qty: Decimal
    remaining_cost: Decimal
    pre_2027: int

    @property
    def acquired_date(self) -> date:
        return parse_iso(self.acquired_at).date()

    @property
    def free_from(self) -> date:
        """First calendar day on which a disposal of this lot is no longer taxable."""
        return self.acquired_date + relativedelta(years=1, days=1)


@dataclass(slots=True)
class Disposal:
    id: int
    account_id: str
    sell_fill_id: int
    kind: str
    lot_id: int
    qty_btc: Decimal
    acquired_at: str
    disposed_at: str
    holding_days: int
    proceeds_eur: Decimal
    cost_eur: Decimal
    sell_fee_eur: Decimal
    gain_eur: Decimal
    taxable: int
    boundary_case: int


@dataclass(slots=True)
class FifoResult:
    lots: list[Lot]
    disposals: list[Disposal]

    @property
    def open_lots(self) -> list[Lot]:
        return [lot for lot in self.lots if lot.remaining_qty > 0]

    @property
    def btc_held(self) -> Decimal:
        return sum((lot.remaining_qty for lot in self.lots), ZERO)


def holding_status(acquired: date, disposed: date) -> tuple[bool, bool]:
    """Return ``(taxable, boundary_case)`` for calendar dates in UTC.

    ``taxable`` is true when the disposal happens within one year of the
    acquisition, the anniversary included (``relativedelta`` clamps 29 Feb to
    28 Feb in non-leap years). ``boundary_case`` marks the anniversary itself.
    """
    if disposed < acquired:
        raise FifoError(f"disposal date {disposed} before acquisition date {acquired}")
    limit = acquired + relativedelta(years=1)
    return disposed <= limit, disposed == limit


def _share(total: Decimal, part: Decimal, whole: Decimal) -> Decimal:
    """Pro-rata share of ``total`` for ``part`` of ``whole``, quantized to 8 places."""
    if whole == 0:
        return ZERO
    return q8(total * part / whole)


class _Fifo:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.lots: list[Lot] = []
        self.disposals: list[Disposal] = []
        self._cursor = 0  # index of the oldest lot that may still have quantity

    def buy(self, fill: Fill) -> None:
        acquired = parse_iso(fill.ts_utc).date()
        self.lots.append(
            Lot(
                lot_id=len(self.lots) + 1,
                account_id=self.account_id,
                buy_fill_id=fill.id or 0,
                acquired_at=fill.ts_utc,
                qty_btc=fill.amount_btc,
                cost_eur_incl_fees=fill.net_eur,
                remaining_qty=fill.amount_btc,
                remaining_cost=fill.net_eur,
                pre_2027=1 if acquired < PRE_2027_CUTOFF else 0,
            )
        )

    def dispose(
        self, fill: Fill, kind: str, qty: Decimal, proceeds_total: Decimal, fee_total: Decimal
    ) -> None:
        """Consume ``qty`` BTC from the oldest lots and record one disposal per consumed lot."""
        available = sum((lot.remaining_qty for lot in self.lots[self._cursor :]), ZERO)
        if qty > available:
            raise FifoError(
                f"fill {fill.exchange_trade_id} ({fill.ts_utc}): {kind} of {fmt(qty)} BTC exceeds "
                f"lots held ({fmt(available)} BTC); record inbound transfers as fills first"
            )
        disposed_date = parse_iso(fill.ts_utc).date()
        remaining = qty
        proceeds_left = proceeds_total
        fee_left = fee_total
        while remaining > 0:
            lot = self.lots[self._cursor]
            if lot.remaining_qty <= 0:
                self._cursor += 1
                continue
            take = min(lot.remaining_qty, remaining)
            last_piece = take == remaining
            if take == lot.remaining_qty:
                cost = lot.remaining_cost
            else:
                cost = _share(lot.remaining_cost, take, lot.remaining_qty)
            proceeds = proceeds_left if last_piece else _share(proceeds_total, take, qty)
            fee = fee_left if last_piece else _share(fee_total, take, qty)
            acquired_date = lot.acquired_date
            taxable, boundary = holding_status(acquired_date, disposed_date)
            self.disposals.append(
                Disposal(
                    id=len(self.disposals) + 1,
                    account_id=self.account_id,
                    sell_fill_id=fill.id or 0,
                    kind=kind,
                    lot_id=lot.lot_id,
                    qty_btc=take,
                    acquired_at=lot.acquired_at,
                    disposed_at=fill.ts_utc,
                    holding_days=(disposed_date - acquired_date).days,
                    proceeds_eur=proceeds,
                    cost_eur=cost,
                    sell_fee_eur=fee,
                    gain_eur=proceeds - cost - fee,
                    taxable=1 if taxable else 0,
                    boundary_case=1 if boundary else 0,
                )
            )
            lot.remaining_qty -= take
            lot.remaining_cost -= cost
            remaining -= take
            proceeds_left -= proceeds
            fee_left -= fee
            if lot.remaining_qty <= 0:
                lot.remaining_qty = ZERO
                lot.remaining_cost = ZERO
                self._cursor += 1


def compute_fifo(fills: Sequence[Fill], account_id: str) -> FifoResult:
    """Pure FIFO computation over fills of one account (fills of other accounts are ignored)."""
    engine = _Fifo(account_id)
    ordered = sorted((f for f in fills if f.account_id == account_id), key=sort_key)
    for fill in ordered:
        if fill.side == "buy":
            engine.buy(fill)
        elif fill.side == "sell":
            engine.dispose(fill, "sell", fill.amount_btc, fill.gross_eur, fill.fee_eur)
        else:
            raise FifoError(f"fill {fill.exchange_trade_id}: unknown side {fill.side!r}")
        if fill.fee_currency == "BTC" and fill.fee_amount > 0:
            engine.dispose(fill, "fee", fill.fee_amount, fill.fee_eur, ZERO)
    return FifoResult(lots=engine.lots, disposals=engine.disposals)


def rebuild(conn: sqlite3.Connection, account_id: str) -> FifoResult:
    """Clear ``lots`` and ``disposals`` of the account and rebuild them from all stored fills.

    Runs inside one immediate (write) transaction so a concurrent sync cannot insert
    fills between reading them and rewriting the tables. On ``FifoError`` nothing
    is changed: the previous lots and disposals stay in place.
    """
    with conn:
        begin_immediate(conn)
        fills = load_fills(conn, account_id)
        result = compute_fifo(fills, account_id)
        conn.execute("DELETE FROM disposals WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM lots WHERE account_id = ?", (account_id,))
        # Lot ids are assigned per rebuild; offset them so several accounts never collide.
        offset_row = conn.execute("SELECT COALESCE(MAX(lot_id), 0) AS m FROM lots").fetchone()
        lot_offset = int(offset_row["m"])
        disp_row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM disposals").fetchone()
        disp_offset = int(disp_row["m"])
        conn.executemany(
            "INSERT INTO lots (lot_id, account_id, buy_fill_id, acquired_at, qty_btc, cost_eur_incl_fees, "
            "remaining_qty, pre_2027) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lot.lot_id + lot_offset,
                    lot.account_id,
                    lot.buy_fill_id,
                    lot.acquired_at,
                    fmt(lot.qty_btc),
                    fmt(lot.cost_eur_incl_fees),
                    fmt(lot.remaining_qty),
                    lot.pre_2027,
                )
                for lot in result.lots
            ],
        )
        conn.executemany(
            "INSERT INTO disposals (id, account_id, sell_fill_id, kind, lot_id, qty_btc, acquired_at, "
            "disposed_at, holding_days, proceeds_eur, cost_eur, sell_fee_eur, gain_eur, taxable, "
            "boundary_case) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d.id + disp_offset,
                    d.account_id,
                    d.sell_fill_id,
                    d.kind,
                    d.lot_id + lot_offset,
                    fmt(d.qty_btc),
                    d.acquired_at,
                    d.disposed_at,
                    d.holding_days,
                    fmt(d.proceeds_eur),
                    fmt(d.cost_eur),
                    fmt(d.sell_fee_eur),
                    fmt(d.gain_eur),
                    d.taxable,
                    d.boundary_case,
                )
                for d in result.disposals
            ],
        )
    if lot_offset or disp_offset:
        for lot in result.lots:
            lot.lot_id += lot_offset
        for d in result.disposals:
            d.id += disp_offset
            d.lot_id += lot_offset
    return result


def load_disposals(
    conn: sqlite3.Connection, account_id: str, *, year: int | None = None, taxable_only: bool = False
) -> list[Disposal]:
    """Stored disposals of an account, optionally restricted to a calendar year (UTC)."""
    sql = "SELECT * FROM disposals WHERE account_id = ?"
    params: list[object] = [account_id]
    if year is not None:
        sql += " AND disposed_at >= ? AND disposed_at < ?"
        params += [f"{year:04d}-01-01", f"{year + 1:04d}-01-01"]
    if taxable_only:
        sql += " AND taxable = 1"
    sql += " ORDER BY disposed_at, id"
    rows = conn.execute(sql, params).fetchall()
    disposals = [
        Disposal(
            id=int(r["id"]),
            account_id=str(r["account_id"]),
            sell_fill_id=int(r["sell_fill_id"]),
            kind=str(r["kind"]),
            lot_id=int(r["lot_id"]),
            qty_btc=Decimal(r["qty_btc"]),
            acquired_at=str(r["acquired_at"]),
            disposed_at=str(r["disposed_at"]),
            holding_days=int(r["holding_days"]),
            proceeds_eur=Decimal(r["proceeds_eur"]),
            cost_eur=Decimal(r["cost_eur"]),
            sell_fee_eur=Decimal(r["sell_fee_eur"]),
            gain_eur=Decimal(r["gain_eur"]),
            taxable=int(r["taxable"]),
            boundary_case=int(r["boundary_case"]),
        )
        for r in rows
    ]
    # Sort by parsed time: legacy rows may mix whole-second and fractional timestamps.
    return sorted(disposals, key=lambda d: (parse_iso(d.disposed_at), d.id))


def load_lots(conn: sqlite3.Connection, account_id: str, *, open_only: bool = False) -> list[Lot]:
    sql = "SELECT * FROM lots WHERE account_id = ?"
    if open_only:
        sql += " AND CAST(remaining_qty AS REAL) > 0"
    sql += " ORDER BY acquired_at, lot_id"
    rows = conn.execute(sql, (account_id,)).fetchall()
    lots: list[Lot] = []
    for r in rows:
        qty = Decimal(r["qty_btc"])
        cost = Decimal(r["cost_eur_incl_fees"])
        remaining = Decimal(r["remaining_qty"])
        lots.append(
            Lot(
                lot_id=int(r["lot_id"]),
                account_id=str(r["account_id"]),
                buy_fill_id=int(r["buy_fill_id"]),
                acquired_at=str(r["acquired_at"]),
                qty_btc=qty,
                cost_eur_incl_fees=cost,
                remaining_qty=remaining,
                remaining_cost=_share(cost, remaining, qty) if remaining < qty else cost,
                pre_2027=int(r["pre_2027"]),
            )
        )
    lots.sort(key=lambda lot: (parse_iso(lot.acquired_at), lot.lot_id))
    return [lot for lot in lots if not open_only or lot.remaining_qty > 0]
