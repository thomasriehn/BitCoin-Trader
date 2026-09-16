"""Fill records, the SHA-256 hash chain and idempotent storage in ``fills``.

A fill is one execution on the exchange (or one filled dry-run order). The
business fields of every row are hashed together with the ``row_hash`` of the
previously inserted row of the same ``(exchange, account_id)`` scope, so any
later edit of a stored amount, price, fee or timestamp is detectable with
``verify_chain``. Metadata that may be enriched later (strategy, signal reason,
advisor decision id, reconciliation flag, client order id) is not part of the
hash and can be updated by a later sync.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from btctrader.common.db import parse_iso
from btctrader.ledger.errors import ChainError, LedgerError

Q8 = Decimal("0.00000001")
EXCHANGE = "bitvavo"
PAIR = "BTC/EUR"

# Fixed-width timestamp format for ``ts_utc`` so that string order equals time order.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# sync_state key prefix holding "<row count>:<row_hash of the last inserted row>" per chain scope.
STATE_CHAIN_TAIL = "chain_tail"


def normalize_ts(ts: str) -> str:
    """Canonical ``ts_utc``: UTC, always six fractional digits, ``Z`` suffix.

    Applied to every fill regardless of how the caller formatted the timestamp
    (an earlier ``iso_utc`` dropped a zero fraction, mixing ``HH:MM:SSZ`` and
    ``HH:MM:SS.ffffffZ`` rows; because ``Z`` sorts after ``.`` a whole-second
    fill then sorted *after* a later fill of the same second).
    """
    return parse_iso(ts).strftime(TS_FORMAT)


def chain_tail_key(exchange: str, account_id: str) -> str:
    return f"{STATE_CHAIN_TAIL}:{exchange}:{account_id}"


# Fields protected by the hash chain (in hashing order).
BUSINESS_FIELDS: tuple[str, ...] = (
    "source",
    "exchange",
    "account_id",
    "exchange_trade_id",
    "exchange_order_id",
    "ts_utc",
    "pair",
    "side",
    "amount_btc",
    "price_eur",
    "gross_eur",
    "fee_amount",
    "fee_currency",
    "fee_eur",
    "net_eur",
    "maker_taker",
    "dry_run",
)

# Fields that a later sync may update without touching the chain.
META_FIELDS: tuple[str, ...] = (
    "client_order_id",
    "strategy",
    "signal_reason",
    "advisor_decision_id",
    "reconciled_with_bot_db",
)

ALL_FIELDS: tuple[str, ...] = BUSINESS_FIELDS + META_FIELDS


def dec(value: object, what: str = "value") -> Decimal:
    """Convert a string, int, float or Decimal to ``Decimal`` without float artefacts.

    Floats are converted through ``repr`` (shortest round-trip string), which is
    what Freqtrade wrote into its SQLite database anyway.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise LedgerError(f"{what}: boolean is not a number")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise LedgerError(f"{what}: cannot parse {value!r} as Decimal") from exc
    else:
        raise LedgerError(f"{what}: unsupported type {type(value).__name__}")
    if not result.is_finite():
        raise LedgerError(f"{what}: non-finite number {value!r}")
    return result


def q8(value: Decimal) -> Decimal:
    """Quantize to 8 decimal places (BTC precision, also used for EUR sub-cent values)."""
    return value.quantize(Q8, rounding=ROUND_HALF_EVEN)


def fmt(value: Decimal) -> str:
    """Render a Decimal as a plain string (no exponent notation)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True, slots=True)
class Fill:
    """One fill. Decimal fields are stored as strings in SQLite (see ``to_row``)."""

    source: str
    exchange: str
    account_id: str
    exchange_trade_id: str
    exchange_order_id: str | None
    ts_utc: str
    pair: str
    side: str
    amount_btc: Decimal
    price_eur: Decimal
    gross_eur: Decimal
    fee_amount: Decimal
    fee_currency: str
    fee_eur: Decimal
    net_eur: Decimal
    maker_taker: str | None
    dry_run: int
    client_order_id: str | None = None
    strategy: str | None = None
    signal_reason: str | None = None
    advisor_decision_id: str | None = None
    reconciled_with_bot_db: int = 0
    # Set once stored.
    id: int | None = None
    prev_hash: str | None = None
    row_hash: str | None = None

    @classmethod
    def build(
        cls,
        *,
        source: str,
        account_id: str,
        exchange_trade_id: str,
        ts_utc: str,
        side: str,
        amount_btc: Decimal | str | float,
        price_eur: Decimal | str | float,
        fee_amount: Decimal | str | float = Decimal(0),
        fee_currency: str = "EUR",
        exchange_order_id: str | None = None,
        maker_taker: str | None = None,
        dry_run: bool | int = False,
        exchange: str = EXCHANGE,
        pair: str = PAIR,
        client_order_id: str | None = None,
        strategy: str | None = None,
        signal_reason: str | None = None,
        advisor_decision_id: str | None = None,
        reconciled_with_bot_db: bool | int = False,
    ) -> Fill:
        """Create a fill and derive ``gross_eur``, ``fee_eur`` and ``net_eur``.

        ``fee_eur`` is the fee in EUR: for a BTC fee it is ``fee_amount x price``.
        ``net_eur`` is ``gross + fee`` for buys and ``gross - fee`` for sells.
        """
        side = side.lower().strip()
        if side not in ("buy", "sell"):
            raise LedgerError(f"fill {exchange_trade_id}: side must be buy or sell, got {side!r}")
        fee_currency = fee_currency.upper().strip() or "EUR"
        if fee_currency not in ("EUR", "BTC"):
            raise LedgerError(f"fill {exchange_trade_id}: unsupported fee currency {fee_currency!r}")
        if maker_taker is not None:
            maker_taker = maker_taker.lower()
            if maker_taker not in ("maker", "taker"):
                raise LedgerError(f"fill {exchange_trade_id}: maker_taker must be maker/taker")
        amount = q8(dec(amount_btc, "amount_btc"))
        price = dec(price_eur, "price_eur")
        fee_amt = q8(dec(fee_amount, "fee_amount"))
        if amount <= 0:
            raise LedgerError(f"fill {exchange_trade_id}: amount must be > 0")
        if price <= 0:
            raise LedgerError(f"fill {exchange_trade_id}: price must be > 0")
        if fee_amt < 0:
            raise LedgerError(f"fill {exchange_trade_id}: fee must be >= 0")
        gross = q8(amount * price)
        fee_eur = fee_amt if fee_currency == "EUR" else q8(fee_amt * price)
        net = gross + fee_eur if side == "buy" else gross - fee_eur
        try:
            ts_norm = normalize_ts(ts_utc)
        except ValueError as exc:
            raise LedgerError(f"fill {exchange_trade_id}: invalid timestamp {ts_utc!r}") from exc
        return cls(
            source=source,
            exchange=exchange,
            account_id=account_id,
            exchange_trade_id=str(exchange_trade_id),
            exchange_order_id=str(exchange_order_id) if exchange_order_id is not None else None,
            ts_utc=ts_norm,
            pair=pair,
            side=side,
            amount_btc=amount,
            price_eur=price,
            gross_eur=gross,
            fee_amount=fee_amt,
            fee_currency=fee_currency,
            fee_eur=fee_eur,
            net_eur=net,
            maker_taker=maker_taker,
            dry_run=1 if dry_run else 0,
            client_order_id=client_order_id,
            strategy=strategy,
            signal_reason=signal_reason,
            advisor_decision_id=advisor_decision_id,
            reconciled_with_bot_db=1 if reconciled_with_bot_db else 0,
        )

    # -- serialisation -------------------------------------------------------------

    def business_values(self) -> dict[str, str | int | None]:
        """Canonical (string) values of the hashed fields."""
        data = asdict(self)
        out: dict[str, str | int | None] = {}
        for name in BUSINESS_FIELDS:
            value = data[name]
            out[name] = fmt(value) if isinstance(value, Decimal) else value
        return out

    def to_row(self) -> dict[str, str | int | None]:
        """All columns as SQLite values (Decimals as strings)."""
        data = asdict(self)
        row: dict[str, str | int | None] = {}
        for name in ALL_FIELDS:
            value = data[name]
            row[name] = fmt(value) if isinstance(value, Decimal) else value
        row["prev_hash"] = self.prev_hash
        row["row_hash"] = self.row_hash
        return row

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Fill:
        keys = row.keys()

        def get(name: str) -> object:
            return row[name] if name in keys else None

        return cls(
            source=str(row["source"]),
            exchange=str(row["exchange"]),
            account_id=str(row["account_id"]),
            exchange_trade_id=str(row["exchange_trade_id"]),
            exchange_order_id=row["exchange_order_id"],
            ts_utc=str(row["ts_utc"]),
            pair=str(row["pair"]),
            side=str(row["side"]),
            amount_btc=Decimal(row["amount_btc"]),
            price_eur=Decimal(row["price_eur"]),
            gross_eur=Decimal(row["gross_eur"]),
            fee_amount=Decimal(row["fee_amount"]),
            fee_currency=str(row["fee_currency"]),
            fee_eur=Decimal(row["fee_eur"]),
            net_eur=Decimal(row["net_eur"]),
            maker_taker=row["maker_taker"],
            dry_run=int(row["dry_run"]),
            client_order_id=row["client_order_id"],
            strategy=row["strategy"],
            signal_reason=row["signal_reason"],
            advisor_decision_id=row["advisor_decision_id"],
            reconciled_with_bot_db=int(row["reconciled_with_bot_db"] or 0),
            id=int(row["id"]) if get("id") is not None else None,
            prev_hash=get("prev_hash"),  # type: ignore[arg-type]
            row_hash=get("row_hash"),  # type: ignore[arg-type]
        )


def sort_key(fill: Fill) -> tuple[datetime, str]:
    """Canonical processing order: exchange timestamp (parsed, so rows stored before
    ``normalize_ts`` existed still sort by time), then exchange trade id."""
    return (parse_iso(fill.ts_utc), fill.exchange_trade_id)


def _comparable_business_values(fill: Fill) -> dict[str, str | int | None]:
    """Business values with ``ts_utc`` normalised, so legacy rows written with a
    whole-second timestamp compare equal to the same fill fetched again."""
    values = fill.business_values()
    values["ts_utc"] = normalize_ts(fill.ts_utc)
    return values


# -- hashing -------------------------------------------------------------------------


def compute_row_hash(fill: Fill, prev_hash: str | None) -> str:
    """SHA-256 over the canonical JSON of the business fields plus the previous hash."""
    payload = {"prev_hash": prev_hash, **fill.business_values()}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_fills(conn: sqlite3.Connection, account_id: str, exchange: str = EXCHANGE) -> list[Fill]:
    """All stored fills of an account in canonical order (ts_utc, exchange_trade_id)."""
    rows = conn.execute(
        "SELECT * FROM fills WHERE exchange = ? AND account_id = ? ORDER BY ts_utc, exchange_trade_id",
        (exchange, account_id),
    ).fetchall()
    # Python sort by parsed time: SQL string order is wrong for mixed-precision legacy rows.
    return sorted((Fill.from_row(r) for r in rows), key=sort_key)


def verify_chain(conn: sqlite3.Connection, account_id: str, exchange: str = EXCHANGE) -> list[str]:
    """Recompute the hash chain in insertion order and return a list of problems (empty = intact).

    Besides the row-to-row links this compares the recomputed tail (row count and
    last ``row_hash``) with the anchor ``upsert_fills`` stored in ``sync_state``,
    so deleting the newest row (which leaves a valid chain) is detected too.
    """
    rows = conn.execute(
        "SELECT * FROM fills WHERE exchange = ? AND account_id = ? ORDER BY id",
        (exchange, account_id),
    ).fetchall()
    problems: list[str] = []
    prev: str | None = None
    for row in rows:
        fill = Fill.from_row(row)
        if fill.prev_hash != prev:
            problems.append(
                f"fill id={fill.id} ({fill.exchange_trade_id}): prev_hash {fill.prev_hash!r} "
                f"does not match previous row_hash {prev!r}"
            )
        expected = compute_row_hash(fill, fill.prev_hash)
        if fill.row_hash != expected:
            problems.append(
                f"fill id={fill.id} ({fill.exchange_trade_id}): row_hash mismatch "
                "(business fields were modified after insertion)"
            )
        prev = fill.row_hash
    tail_problem = _tail_problem(conn, exchange, account_id, len(rows), prev)
    if tail_problem:
        problems.append(tail_problem)
    return problems


def _tail_problem(
    conn: sqlite3.Connection, exchange: str, account_id: str, count: int, last_hash: str | None
) -> str | None:
    """Compare the actual tail of a scope with the anchor in ``sync_state`` (None = consistent)."""
    anchor = get_state(conn, chain_tail_key(exchange, account_id))
    if anchor is None or anchor == f"{count}:{last_hash or ''}":
        return None
    stored_count, _, stored_hash = anchor.partition(":")
    return (
        f"chain tail mismatch: sync_state records {stored_count} rows ending in {stored_hash!r}, "
        f"table has {count} rows ending in {last_hash!r} (rows deleted or reinserted?)"
    )


def assert_chain(conn: sqlite3.Connection, account_id: str, exchange: str = EXCHANGE) -> None:
    problems = verify_chain(conn, account_id, exchange)
    if problems:
        raise ChainError("hash chain broken: " + "; ".join(problems))


# -- storage -------------------------------------------------------------------------


@dataclass(slots=True)
class UpsertResult:
    inserted: int = 0
    unchanged: int = 0
    updated_meta: int = 0
    conflicts: list[str] | None = None

    def __post_init__(self) -> None:
        if self.conflicts is None:
            self.conflicts = []

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts or [])


def _last_hash(conn: sqlite3.Connection, exchange: str, account_id: str) -> str | None:
    row = conn.execute(
        "SELECT row_hash FROM fills WHERE exchange = ? AND account_id = ? ORDER BY id DESC LIMIT 1",
        (exchange, account_id),
    ).fetchone()
    return None if row is None else str(row["row_hash"])


def upsert_fills(conn: sqlite3.Connection, fills: Iterable[Fill]) -> UpsertResult:
    """Insert new fills (chained) and refresh metadata of known ones. Idempotent.

    Known fills (same exchange, account and ``exchange_trade_id``) are never
    rewritten: if their business fields differ from the incoming data the
    incoming fill is recorded as a conflict and skipped, so the chain stays
    intact and the discrepancy is visible in the sync result.
    """
    result = UpsertResult()
    incoming: Sequence[Fill] = sorted(fills, key=sort_key)
    last_hashes: dict[tuple[str, str], str | None] = {}
    with conn:
        # Take the write lock before reading the chain state: a second sync running at
        # the same time then waits (busy timeout) instead of chaining onto a stale prev_hash.
        begin_immediate(conn)
        for fill in incoming:
            scope = (fill.exchange, fill.account_id)
            existing = conn.execute(
                "SELECT * FROM fills WHERE exchange = ? AND account_id = ? AND exchange_trade_id = ?",
                (fill.exchange, fill.account_id, fill.exchange_trade_id),
            ).fetchone()
            if existing is not None:
                stored = Fill.from_row(existing)
                if _comparable_business_values(stored) != _comparable_business_values(fill):
                    result.conflicts.append(  # type: ignore[union-attr]
                        f"{fill.exchange_trade_id}: stored business fields differ from incoming data"
                    )
                    continue
                meta_new = {name: getattr(fill, name) for name in META_FIELDS}
                meta_old = {name: getattr(stored, name) for name in META_FIELDS}
                # Only fill in metadata that is new or changed and non-empty.
                updates = {k: v for k, v in meta_new.items() if v not in (None, "", 0) and v != meta_old[k]}
                if updates:
                    assignments = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(
                        f"UPDATE fills SET {assignments} WHERE id = ?",
                        (*updates.values(), stored.id),
                    )
                    result.updated_meta += 1
                else:
                    result.unchanged += 1
                continue
            if scope not in last_hashes:
                last_hashes[scope] = _last_hash(conn, *scope)
                # Never extend a chain whose tail no longer matches the anchor (deleted rows).
                problem = _tail_problem(conn, *scope, _row_count(conn, *scope), last_hashes[scope])
                if problem:
                    raise ChainError("hash chain broken: " + problem)
            prev = last_hashes[scope]
            chained = replace(fill, prev_hash=prev, row_hash=compute_row_hash(fill, prev))
            row = chained.to_row()
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO fills ({columns}) VALUES ({placeholders})", tuple(row.values()))
            last_hashes[scope] = chained.row_hash
            result.inserted += 1
        for scope in last_hashes:
            _record_chain_tail(conn, *scope)
    return result


def _row_count(conn: sqlite3.Connection, exchange: str, account_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE exchange = ? AND account_id = ?", (exchange, account_id)
    ).fetchone()
    return int(row["n"])


def _record_chain_tail(conn: sqlite3.Connection, exchange: str, account_id: str) -> None:
    """Store row count and last row_hash of a scope as the anchor ``verify_chain`` checks."""
    tail = _last_hash(conn, exchange, account_id) or ""
    count = _row_count(conn, exchange, account_id)
    _set_state_nocommit(conn, chain_tail_key(exchange, account_id), f"{count}:{tail}")


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Start a write transaction now (no-op when one is already open)."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _set_state_nocommit(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        _set_state_nocommit(conn, key, value)


def delete_state(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]
