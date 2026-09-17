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
from btctrader.ledger.errors import FifoError, LedgerError, ccxt_errors
from btctrader.ledger.fifo import FifoResult, rebuild
from btctrader.ledger.fills import (
    EXCHANGE,
    PAIR,
    Fill,
    UpsertResult,
    assert_chain,
    dec,
    delete_state,
    set_state,
    upsert_fills,
)

log = logging.getLogger(__name__)

STATE_LAST_FILL_TS = "last_fill_ts"
STATE_LAST_SYNC = "last_sync_utc"
STATE_FIFO_ERROR = "fifo_error"
STATE_FIFO_ERROR_UTC = "fifo_error_utc"
CCXT_SYMBOL = "BTC/EUR"
EXCHANGE_PAGE_LIMIT = 1000

# (source, dry_run) the fills of each ledger source carry; an account holds exactly one kind.
SOURCE_MODES: dict[str, tuple[str, int]] = {"exchange": ("exchange", 0), "freqtrade-db": ("freqtrade-db", 1)}


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
    fifo_error: str | None = None  # set when the FIFO rebuild failed; lots/disposals are stale then


# -- exchange ------------------------------------------------------------------------


def make_exchange(settings: Settings) -> Any:
    """Create a ``ccxt.bitvavo`` instance with the read-only key and ``operatorId`` option."""
    import ccxt  # imported lazily: heavy and not needed for dry-run

    if not (settings.bitvavo_api_key_ro and settings.bitvavo_api_secret_ro):
        raise LedgerError("LEDGER_SOURCE=exchange needs BITVAVO_API_KEY_RO and BITVAVO_API_SECRET_RO")
    try:
        return ccxt.bitvavo(
            {
                "apiKey": settings.bitvavo_api_key_ro,
                "secret": settings.bitvavo_api_secret_ro,
                "enableRateLimit": True,
                "options": {"operatorId": int(settings.bitvavo_operator_id)},
            }
        )
    except ccxt.BaseError as exc:
        raise LedgerError(f"cannot create ccxt.bitvavo client: {type(exc).__name__}: {exc}") from exc


def _fetch_my_trades(
    exchange: ExchangeLike, symbol: str, since: int | None, limit: int, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """``fetch_my_trades`` with ccxt errors (network, rate limit, auth) turned into ``LedgerError``."""
    try:
        return exchange.fetch_my_trades(symbol, since, limit, params)
    except ccxt_errors() as exc:
        raise LedgerError(f"exchange trades failed: {type(exc).__name__}: {exc}") from exc


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

    Bitvavo documents that a window returns its newest trades first, so pages walk
    backwards by passing ``until`` (ccxt maps it to the ``end`` query parameter)
    below the oldest trade received. Because that ordering was not verified against
    the private endpoint, the walk is direction-agnostic: whenever a full page was
    seen, a forward pass with ``since = newest + 1`` follows, so trades are complete
    even if the server hands out the oldest ``limit`` trades of the window.
    """
    fills: dict[str, Fill] = {}
    newest: int | None = None
    saw_full_page = False

    def take(trades: list[dict[str, Any]]) -> tuple[int, int | None, int | None]:
        """Store new fills; return (new count, oldest ts, newest ts) of the page."""
        nonlocal newest
        new = 0
        lo: int | None = None
        hi: int | None = None
        for trade in trades:
            fill = fill_from_ccxt_trade(trade, account_id)
            if fill.exchange_trade_id not in fills:
                fills[fill.exchange_trade_id] = fill
                new += 1
            ts = int(trade.get("timestamp") or 0)
            lo = ts if lo is None else min(lo, ts)
            hi = ts if hi is None else max(hi, ts)
        if hi is not None:
            newest = hi if newest is None else max(newest, hi)
        return new, lo, hi

    cursor_end: int | None = None
    pages = 0
    while pages < max_pages:
        pages += 1
        params: dict[str, Any] = {} if cursor_end is None else {"until": cursor_end}
        trades = _fetch_my_trades(exchange, symbol, since_ms, page_limit, params)
        if not trades:
            break
        new, oldest, _ = take(trades)
        if len(trades) >= page_limit:
            saw_full_page = True
        if new == 0 or len(trades) < page_limit or oldest is None:
            break
        cursor_end = oldest - 1
        if since_ms is not None and cursor_end < since_ms:
            break
    # Forward pass: only needed when a page was full (a short page already held the whole window).
    while saw_full_page and newest is not None and pages < max_pages:
        pages += 1
        trades = _fetch_my_trades(exchange, symbol, newest + 1, page_limit, {})
        if not trades:
            break
        new, _, _ = take(trades)
        if new == 0 or len(trades) < page_limit:
            break
    if pages >= max_pages:
        log.warning("exchange trades: stopped after %d pages, history may be incomplete", pages)
    return sorted(fills.values(), key=lambda f: (f.ts_utc, f.exchange_trade_id))


# -- freqtrade db ---------------------------------------------------------------------

# Every non-open order with a filled amount is a fill. Freqtrade keeps a partially filled
# entry that timed out as status 'canceled' with filled > 0 (freqtradebot.handle_cancel_enter,
# reason PARTIALLY_FILLED) and counts that amount as bought, so no status filter here.
_FT_QUERY = """
SELECT o.id AS o_id, o.order_id, o.ft_order_side, o.ft_pair, o.side, o.status, o.filled, o.amount,
       o.average, o.price, o.cost, o.order_filled_date, o.order_date, o.ft_fee_base, o.ft_order_tag,
       t.id AS trade_id, t.fee_open, t.fee_close, t.fee_open_currency, t.fee_close_currency,
       t.enter_tag, t.exit_reason, t.strategy
FROM orders o JOIN trades t ON t.id = o.ft_trade_id
WHERE o.ft_is_open = 0 AND COALESCE(o.filled, 0) > 0 AND o.ft_pair = ?
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
        fee_cur_ft = str(row["fee_open_currency" if side == "buy" else "fee_close_currency"] or "").upper()
        if fee_base is not None and dec(fee_base) > 0:
            fee_amount: Decimal = dec(fee_base)
            fee_currency = "BTC"
        else:
            if fee_cur_ft == "BTC":
                # Freqtrade "ate the fee into dust" (apply_fee_conditional returned None), so the
                # BTC amount is unknown; the EUR estimate below records no fee disposal.
                log.warning(
                    "order %s: Freqtrade fee in BTC but ft_fee_base is NULL (fee eaten into dust); "
                    "fee booked as EUR estimate, no BTC fee disposal",
                    row["order_id"],
                )
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


def _since_ms_for_account(conn: sqlite3.Connection, account_id: str, overlap_days: int = 3) -> int | None:
    """Start of the exchange window: newest stored *exchange* fill of this account minus an overlap.

    Derived from the account's own fills, not from the global ``last_fill_ts`` key, so a
    new account id in an existing DB starts with the full history.
    """
    last = _last_fill_ts(conn, account_id, "exchange")
    if not last:
        return None
    ts = parse_iso(last).timestamp() - overlap_days * 86_400
    return max(0, int(ts * 1000))


def assert_same_mode(
    conn: sqlite3.Connection, account_id: str, source: str, exchange: str = EXCHANGE
) -> None:
    """Refuse to mix dry-run and real fills in one account.

    FIFO, tax report, fees and equity figures never filter on ``source``/``dry_run``,
    so a go-live that keeps ``LEDGER_ACCOUNT_ID`` would turn months of simulated
    buys into real lots. The live phase needs a new account id (or a new DB).
    """
    expected = SOURCE_MODES.get(source)
    if expected is None:
        raise LedgerError(f"unknown ledger source {source!r}")
    rows = conn.execute(
        "SELECT DISTINCT source, dry_run FROM fills WHERE exchange = ? AND account_id = ?",
        (exchange, account_id),
    ).fetchall()
    foreign = [(str(r["source"]), int(r["dry_run"])) for r in rows]
    foreign = [mode for mode in foreign if mode != expected]
    if foreign:
        found = ", ".join(f"source={s!r} dry_run={d}" for s, d in foreign)
        raise LedgerError(
            f"account {account_id!r} already holds fills of another kind ({found}); "
            f"LEDGER_SOURCE={source!r} would mix them with source={expected[0]!r} dry_run={expected[1]}. "
            "Use a new LEDGER_ACCOUNT_ID (for example 'bitvavo-live') or a new LEDGER_DB_PATH for this phase."
        )


def _last_fill_ts(
    conn: sqlite3.Connection, account_id: str, source: str, exchange: str = EXCHANGE
) -> str | None:
    """Newest ``ts_utc`` of the account's fills of this source, compared as time (not as string)."""
    rows = conn.execute(
        "SELECT ts_utc FROM fills WHERE exchange = ? AND account_id = ? AND source = ?",
        (exchange, account_id, source),
    ).fetchall()
    if not rows:
        return None
    return max((str(r["ts_utc"]) for r in rows), key=parse_iso)


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
    """Fetch fills from the configured source, store them, rebuild FIFO and update ``sync_state``.

    A failing FIFO rebuild (for example a sell that exceeds the lots held) does not
    raise: the fills stay stored, ``lots``/``disposals`` keep their previous content,
    the message lands in ``sync_state`` (``fifo_error``, ``fifo_error_utc``) and in
    ``SyncResult.fifo_error``. The keys are removed by the next successful rebuild.
    """
    src = source or settings.ledger_source
    account_id = settings.ledger_account_id
    assert_chain(conn, account_id, EXCHANGE)
    assert_same_mode(conn, account_id, src)
    if src == "exchange":
        client = exchange if exchange is not None else make_exchange(settings)
        since = since_ms if since_ms is not None else _since_ms_for_account(conn, account_id)
        fills: Sequence[Fill] = fetch_exchange_fills(client, account_id, since_ms=since)
    else:
        fills = fills_from_freqtrade_db(ft_db_path or settings.ft_db_path, account_id)
    upsert = upsert_fills(conn, fills)
    for conflict in upsert.conflicts or []:
        log.warning("fill conflict: %s", conflict)
    fifo: FifoResult | None = None
    fifo_error: str | None = None
    if rebuild_fifo:
        try:
            fifo = rebuild(conn, account_id)
        except FifoError as exc:
            fifo_error = str(exc)
            log.error("FIFO rebuild failed, lots/disposals are stale: %s", exc)
            set_state(conn, STATE_FIFO_ERROR, fifo_error)
            set_state(conn, STATE_FIFO_ERROR_UTC, iso_utc(datetime.now(tz=UTC)))
        else:
            delete_state(conn, STATE_FIFO_ERROR)
            delete_state(conn, STATE_FIFO_ERROR_UTC)
    last_ts = _last_fill_ts(conn, account_id, SOURCE_MODES[src][0])
    if last_ts:
        set_state(conn, STATE_LAST_FILL_TS, last_ts)
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
        fifo_error=fifo_error,
    )
