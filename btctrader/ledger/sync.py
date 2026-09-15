"""Fetch fills from the exchange (ccxt, read-only key) or from the Freqtrade SQLite DB.

Exchange mode uses ``fetch_my_trades`` of ``ccxt.bitvavo``. Verified against
ccxt 4.5 ``bitvavo.parse_trade``: unified fields ``id``, ``order``, ``timestamp``
(ms), ``side``, ``amount``, ``price``, ``cost``, ``fee`` (``{"cost", "currency"}``)
and ``takerOrMaker``; ``info`` keeps the raw Bitvavo strings (``amount``,
``price``, ``fee``, ``feeCurrency``), which are preferred to avoid float noise.

Freqtrade-db mode (dry run) reads the ``orders`` and ``trades`` tables of
Freqtrade 2026.8 (``freqtrade/persistence/trade_model.py``): one fill per
filled order, ``exchange_trade_id = "ft-<order_id>"``, fee =
``filled x average x fee_open|fee_close`` in EUR unless ``ft_fee_base`` (fee in
BTC) is set.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from btctrader.common.config import Settings
from btctrader.common.db import iso_utc, parse_iso
from btctrader.ledger.errors import LedgerError
from btctrader.ledger.fifo import FifoResult, rebuild
from btctrader.ledger.fills import (
    EXCHANGE,
    PAIR,
    Fill,
    UpsertResult,
    assert_chain,
    dec,
    get_state,
    set_state,
    upsert_fills,
)

log = logging.getLogger(__name__)

STATE_LAST_FILL_TS = "last_fill_ts"
STATE_LAST_SYNC = "last_sync_utc"
CCXT_SYMBOL = "BTC/EUR"
EXCHANGE_PAGE_LIMIT = 1000


class ExchangeLike(Protocol):
    """The subset of a ccxt exchange used here (lets tests inject a fake)."""

    def fetch_my_trades(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: Any = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class SyncResult:
    source: str
    fetched: int
    upsert: UpsertResult
    fifo: FifoResult | None
    last_fill_ts: str | None
    warnings: list[str] = field(default_factory=list)


# -- exchange ------------------------------------------------------------------------


def make_exchange(settings: Settings) -> Any:
    """Create a ``ccxt.bitvavo`` instance with the read-only key and ``operatorId`` option."""
    import ccxt  # imported lazily: heavy and not needed for dry-run

    if not (settings.bitvavo_api_key_ro and settings.bitvavo_api_secret_ro):
        raise LedgerError("LEDGER_SOURCE=exchange needs BITVAVO_API_KEY_RO and BITVAVO_API_SECRET_RO")
    return ccxt.bitvavo(
        {
            "apiKey": settings.bitvavo_api_key_ro,
            "secret": settings.bitvavo_api_secret_ro,
            "enableRateLimit": True,
            "options": {"operatorId": int(settings.bitvavo_operator_id)},
        }
    )


def _raw_or_unified(trade: dict[str, Any], raw_key: str, unified_key: str) -> Any:
    info = trade.get("info") or {}
    value = info.get(raw_key) if isinstance(info, dict) else None
    return value if value is not None else trade.get(unified_key)


def fill_from_ccxt_trade(trade: dict[str, Any], account_id: str, *, dry_run: bool = False) -> Fill:
    """Convert one ccxt unified trade (Bitvavo) into a ``Fill``."""
    trade_id = trade.get("id")
    if not trade_id:
        raise LedgerError(f"exchange trade without id: {trade!r}")
    ts = trade.get("timestamp")
    if ts is None:
        raise LedgerError(f"exchange trade {trade_id} without timestamp")
    fee = trade.get("fee") or {}
    info = trade.get("info") or {}
    fee_cost = info.get("fee") if isinstance(info, dict) and info.get("fee") is not None else fee.get("cost")
    fee_currency = (
        info.get("feeCurrency") if isinstance(info, dict) and info.get("feeCurrency") else fee.get("currency")
    )
    symbol = trade.get("symbol") or CCXT_SYMBOL
    return Fill.build(
        source="exchange",
        account_id=account_id,
        exchange_trade_id=str(trade_id),
        exchange_order_id=str(trade["order"]) if trade.get("order") else None,
        ts_utc=iso_utc(datetime.fromtimestamp(int(ts) / 1000, tz=UTC)),
        side=str(trade.get("side") or ""),
        amount_btc=dec(_raw_or_unified(trade, "amount", "amount"), "amount"),
        price_eur=dec(_raw_or_unified(trade, "price", "price"), "price"),
        fee_amount=dec(fee_cost if fee_cost is not None else 0, "fee"),
        fee_currency=str(fee_currency or "EUR"),
        maker_taker=trade.get("takerOrMaker"),
        dry_run=dry_run,
        pair=str(symbol).replace("-", "/"),
    )


def fetch_exchange_fills(
    exchange: ExchangeLike,
    account_id: str,
    *,
    since_ms: int | None = None,
    symbol: str = CCXT_SYMBOL,
    page_limit: int = EXCHANGE_PAGE_LIMIT,
    max_pages: int = 100,
) -> list[Fill]:
    """Page through ``fetch_my_trades`` and convert to fills.

    Bitvavo returns the newest trades of a window first, so pages walk backwards by
    passing ``until`` (ccxt maps it to the ``end`` query parameter) below the oldest
    trade received, until a page is short or reaches ``since_ms``.
    """
    fills: dict[str, Fill] = {}
    cursor_end: int | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {} if cursor_end is None else {"until": cursor_end}
        trades = exchange.fetch_my_trades(symbol, since_ms, page_limit, params)
        if not trades:
            break
        new = 0
        oldest: int | None = None
        for trade in trades:
            fill = fill_from_ccxt_trade(trade, account_id)
            if fill.exchange_trade_id not in fills:
                fills[fill.exchange_trade_id] = fill
                new += 1
            ts = int(trade.get("timestamp") or 0)
            oldest = ts if oldest is None else min(oldest, ts)
        if new == 0 or len(trades) < page_limit or oldest is None:
            break
        cursor_end = oldest - 1
        if since_ms is not None and cursor_end < since_ms:
            break
    return sorted(fills.values(), key=lambda f: (f.ts_utc, f.exchange_trade_id))


# -- freqtrade db ---------------------------------------------------------------------

_FT_QUERY = """
SELECT o.id AS o_id, o.order_id, o.ft_order_side, o.ft_pair, o.side, o.status, o.filled, o.amount,
       o.average, o.price, o.cost, o.order_filled_date, o.order_date, o.ft_fee_base, o.ft_order_tag,
       t.id AS trade_id, t.fee_open, t.fee_close, t.enter_tag, t.exit_reason, t.strategy
FROM orders o JOIN trades t ON t.id = o.ft_trade_id
WHERE o.ft_is_open = 0 AND o.status = 'closed' AND COALESCE(o.filled, 0) > 0 AND o.ft_pair = ?
ORDER BY COALESCE(o.order_filled_date, o.order_date), o.id
"""


def _ft_datetime(value: object) -> str:
    """Freqtrade stores naive UTC datetimes as ``YYYY-MM-DD HH:MM:SS[.ffffff]``."""
    if isinstance(value, datetime):
        return iso_utc(value)
    text = str(value).strip()
    return iso_utc(parse_iso(text))


def fills_from_freqtrade_db(
    ft_db_path: str | Path, account_id: str, *, pair: str = PAIR, dry_run: bool = True
) -> list[Fill]:
    """Read filled orders from the Freqtrade DB and convert each into one fill."""
    path = Path(ft_db_path)
    if not path.is_file():
        raise LedgerError(f"Freqtrade DB not found: {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_FT_QUERY, (pair,)).fetchall()
    finally:
        conn.close()
    fills: list[Fill] = []
    for row in rows:
        ft_side = str(row["ft_order_side"])
        side = "sell" if ft_side in ("sell", "stoploss") else "buy"
        filled = dec(row["filled"], "filled")
        price_raw = row["average"] if row["average"] is not None else row["price"]
        if price_raw is None and row["cost"]:
            price_raw = dec(row["cost"]) / filled
        if price_raw is None:
            log.warning("order %s has no price, skipped", row["order_id"])
            continue
        price = dec(price_raw, "average")
        fee_base = row["ft_fee_base"]
        if fee_base is not None and dec(fee_base) > 0:
            fee_amount: Decimal = dec(fee_base)
            fee_currency = "BTC"
        else:
            ratio = dec(row["fee_open"] if side == "buy" else row["fee_close"] or 0)
            fee_amount = filled * price * ratio
            fee_currency = "EUR"
        reason = row["ft_order_tag"] or (row["enter_tag"] if side == "buy" else row["exit_reason"])
        fills.append(
            Fill.build(
                source="freqtrade-db",
                account_id=account_id,
                exchange_trade_id=f"ft-{row['order_id']}",
                exchange_order_id=str(row["order_id"]),
                ts_utc=_ft_datetime(row["order_filled_date"] or row["order_date"]),
                side=side,
                amount_btc=filled,
                price_eur=price,
                fee_amount=fee_amount,
                fee_currency=fee_currency,
                maker_taker=None,
                dry_run=dry_run,
                pair=str(row["ft_pair"]),
                client_order_id=f"trade-{row['trade_id']}",
                strategy=row["strategy"],
                signal_reason=reason,
                reconciled_with_bot_db=True,
            )
        )
    return fills


# -- orchestration --------------------------------------------------------------------


def _since_ms_from_state(conn: sqlite3.Connection, overlap_days: int = 3) -> int | None:
    last = get_state(conn, STATE_LAST_FILL_TS)
    if not last:
        return None
    ts = parse_iso(last).timestamp() - overlap_days * 86_400
    return max(0, int(ts * 1000))


def run_sync(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    source: str | None = None,
    exchange: ExchangeLike | None = None,
    ft_db_path: str | Path | None = None,
    since_ms: int | None = None,
    rebuild_fifo: bool = True,
) -> SyncResult:
    """Fetch fills from the configured source, store them, rebuild FIFO and update ``sync_state``."""
    src = source or settings.ledger_source
    account_id = settings.ledger_account_id
    assert_chain(conn, account_id, EXCHANGE)
    if src == "exchange":
        client = exchange if exchange is not None else make_exchange(settings)
        since = since_ms if since_ms is not None else _since_ms_from_state(conn)
        fills: Sequence[Fill] = fetch_exchange_fills(client, account_id, since_ms=since)
    elif src == "freqtrade-db":
        fills = fills_from_freqtrade_db(ft_db_path or settings.ft_db_path, account_id)
    else:
        raise LedgerError(f"unknown ledger source {src!r}")
    upsert = upsert_fills(conn, fills)
    for conflict in upsert.conflicts or []:
        log.warning("fill conflict: %s", conflict)
    fifo = rebuild(conn, account_id) if rebuild_fifo else None
    last_row = conn.execute(
        "SELECT MAX(ts_utc) AS ts FROM fills WHERE exchange = ? AND account_id = ?", (EXCHANGE, account_id)
    ).fetchone()
    last_ts = last_row["ts"] if last_row and last_row["ts"] else None
    if last_ts:
        set_state(conn, STATE_LAST_FILL_TS, str(last_ts))
    set_state(conn, STATE_LAST_SYNC, iso_utc(datetime.now(tz=UTC)))
    log.info(
        "sync %s: fetched=%d inserted=%d unchanged=%d meta=%d conflicts=%d lots=%s disposals=%s",
        src,
        len(fills),
        upsert.inserted,
        upsert.unchanged,
        upsert.updated_meta,
        upsert.conflict_count,
        len(fifo.lots) if fifo else "-",
        len(fifo.disposals) if fifo else "-",
    )
    return SyncResult(
        source=src,
        fetched=len(fills),
        upsert=upsert,
        fifo=fifo,
        last_fill_ts=last_ts,
        warnings=list(upsert.conflicts or []),
    )
