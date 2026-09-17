"""Read-only data access for the dashboard.

Everything here reads ``ledger.sqlite`` (through ``btctrader.common.db.connect_readonly``,
so the connection cannot write), the advisor files (``decision.json``,
``decisions.jsonl``), the guard files (``state.json``, ``killswitch.lock``,
``events.jsonl``) and the Freqtrade REST API. Nothing is ever written.

Money is handled as ``decimal.Decimal``. The JSON payloads serialise EUR
amounts as strings with two decimals (exact), except the chart series in
``equity_series`` which are floats because they only feed Chart.js.

Tax figures are not recomputed here: ``tax_summary`` calls
``btctrader.ledger.export.build_tax_report`` so the numbers match the yearly
``steuer-YYYY.csv`` exactly, and open lots use ``Lot.free_from`` for the
``frei_ab`` date.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from btctrader.common.db import connect_readonly, iso_utc, parse_iso, utcnow
from btctrader.common.ftapi import FreqtradeClient, FreqtradeError
from btctrader.common.jsonl import read_json, read_jsonl_tail
from btctrader.ledger.export import build_tax_report

ZERO = Decimal(0)
Q2 = Decimal("0.01")
HUNDRED = Decimal(100)

DECISION_FILE = "decision.json"
DECISIONS_LOG = "decisions.jsonl"
STATE_FILE = "state.json"
LOCK_FILE = "killswitch.lock"
EVENTS_FILE = "events.jsonl"

MAX_EQUITY_DAYS = 3660
MAX_LIST_LIMIT = 500


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_EVEN)


def money(value: Decimal | None) -> str | None:
    """Serialise an EUR amount as a plain string with two decimals."""
    return None if value is None else format(q2(value), "f")


def _pct(part: Decimal, base: Decimal) -> Decimal | None:
    """``part / base`` in percent, rounded to two decimals; None when the base is zero."""
    if base == 0:
        return None
    return q2(part / base * HUNDRED)


def _dec(value: object) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value))


# -- ledger access -------------------------------------------------------------------


def open_ledger(path: str | Path) -> sqlite3.Connection | None:
    """Open the ledger read-only (``mode=ro``). Returns None when the file does not exist yet.

    Only ``btctrader.ledger`` may write ``ledger.sqlite`` (KOMPONENTEN.md section 5),
    so the dashboard never uses the schema-applying ``connect``: no DDL, no PRAGMA,
    and an INSERT on the returned connection raises ``sqlite3.OperationalError``.
    A read transaction is started right away so that every SELECT of one request
    sees the same WAL snapshot even while the daily ledger sync rebuilds the FIFO
    tables; the caller closes (or rolls back) the connection when done.
    """
    p = Path(path)
    if not p.is_file():
        return None
    conn = connect_readonly(p)
    conn.execute("BEGIN")
    return conn


# -- equity --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquityRow:
    date_utc: str
    equity: Decimal
    bh: Decimal
    dca: Decimal
    fees_cum: Decimal
    realized_gain_ytd: Decimal
    btc_price: Decimal
    dry_run: int


def load_equity_rows(conn: sqlite3.Connection) -> list[EquityRow]:
    rows = conn.execute("SELECT * FROM equity_daily ORDER BY date_utc").fetchall()
    return [
        EquityRow(
            date_utc=str(r["date_utc"]),
            equity=Decimal(r["equity_eur"]),
            bh=Decimal(r["bh_equity_eur"]),
            dca=Decimal(r["dca_equity_eur"]),
            fees_cum=Decimal(r["fees_cum_eur"]),
            realized_gain_ytd=Decimal(r["realized_gain_ytd_eur"]),
            btc_price=Decimal(r["btc_price_eur"]),
            dry_run=int(r["dry_run"]),
        )
        for r in rows
    ]


def drawdown_series(values: list[Decimal]) -> list[Decimal]:
    """Drawdown in percent from the running peak (0 at a new high, negative below it)."""
    out: list[Decimal] = []
    peak: Decimal | None = None
    for v in values:
        peak = v if peak is None or v > peak else peak
        if peak <= 0:
            out.append(ZERO)
        else:
            out.append(q2((v - peak) / peak * HUNDRED))
    return out


def equity_series(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    """Equity, benchmark and drawdown series of the last ``days`` calendar days plus a summary.

    The window is ``days`` calendar days ending at the newest snapshot (not the last
    ``days`` rows, which would silently cover more time when the daily timer missed
    days). Drawdowns are computed over the whole history (so the running peak is
    the true all-time peak) and then cut to the window; the summary's max drawdown
    is the minimum of the returned window, i.e. measured from the all-time high.
    """
    rows = load_equity_rows(conn)
    dd_bot = drawdown_series([r.equity for r in rows])
    dd_bh = drawdown_series([r.bh for r in rows])
    dd_dca = drawdown_series([r.dca for r in rows])
    if days > 0 and rows:
        start = _window_start(rows[-1].date_utc, days)
        keep = sum(1 for r in rows if r.date_utc >= start)
        if keep < len(rows):
            rows, dd_bot, dd_bh, dd_dca = rows[-keep:], dd_bot[-keep:], dd_bh[-keep:], dd_dca[-keep:]

    summary: dict[str, Any] = {
        "rows": len(rows),
        "from": rows[0].date_utc if rows else None,
        "to": rows[-1].date_utc if rows else None,
        "equity_eur": None,
        "bh_equity_eur": None,
        "dca_equity_eur": None,
        "vs_bh_eur": None,
        "vs_bh_pct": None,
        "vs_dca_eur": None,
        "vs_dca_pct": None,
        "return_pct": None,
        "max_drawdown_bot_pct": None,
        "max_drawdown_bh_pct": None,
        "max_drawdown_dca_pct": None,
        "btc_price_eur": None,
        "dry_run": None,
    }
    if rows:
        last = rows[-1]
        first = rows[0]
        summary.update(
            {
                "equity_eur": money(last.equity),
                "bh_equity_eur": money(last.bh),
                "dca_equity_eur": money(last.dca),
                "vs_bh_eur": money(last.equity - last.bh),
                "vs_bh_pct": _num(_pct(last.equity - last.bh, last.bh)),
                "vs_dca_eur": money(last.equity - last.dca),
                "vs_dca_pct": _num(_pct(last.equity - last.dca, last.dca)),
                "return_pct": _num(_pct(last.equity - first.equity, first.equity)),
                "max_drawdown_bot_pct": _num(min(dd_bot)),
                "max_drawdown_bh_pct": _num(min(dd_bh)),
                "max_drawdown_dca_pct": _num(min(dd_dca)),
                "btc_price_eur": _num(last.btc_price),
                "dry_run": bool(last.dry_run),
            }
        )
    return {
        "days": days,
        "series": {
            "dates": [r.date_utc for r in rows],
            "equity": [float(r.equity) for r in rows],
            "bh": [float(r.bh) for r in rows],
            "dca": [float(r.dca) for r in rows],
            "drawdown": [float(v) for v in dd_bot],
            "drawdown_bh": [float(v) for v in dd_bh],
            "drawdown_dca": [float(v) for v in dd_dca],
        },
        "summary": summary,
    }


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _window_start(last_date: str, days: int) -> str:
    """First ``YYYY-MM-DD`` of a ``days``-day window that ends on ``last_date`` (inclusive)."""
    try:
        end = date.fromisoformat(last_date[:10])
    except ValueError:
        return ""
    return (end - timedelta(days=days - 1)).isoformat()


# -- fees ----------------------------------------------------------------------------


def fees_summary(conn: sqlite3.Connection, account_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Cumulative fees, last 30 days, current year and the maker share (by EUR and by count)."""
    now = now or utcnow()
    since_30d = iso_utc(now - timedelta(days=30))
    year_start = f"{now.year:04d}-01-01"
    rows = conn.execute(
        "SELECT ts_utc, fee_eur, maker_taker FROM fills WHERE account_id = ? ORDER BY ts_utc",
        (account_id,),
    ).fetchall()
    cum = ZERO
    last_30d = ZERO
    ytd = ZERO
    maker_eur = ZERO
    known_eur = ZERO
    maker_count = 0
    known_count = 0
    unknown_count = 0
    for r in rows:
        fee = Decimal(r["fee_eur"])
        ts = str(r["ts_utc"])
        cum += fee
        if ts >= since_30d:
            last_30d += fee
        if ts >= year_start:
            ytd += fee
        mt = r["maker_taker"]
        if mt in ("maker", "taker"):
            known_count += 1
            known_eur += fee
            if mt == "maker":
                maker_count += 1
                maker_eur += fee
        else:
            unknown_count += 1
    return {
        "cum_eur": money(cum),
        "last_30d_eur": money(last_30d),
        "ytd_eur": money(ytd),
        "year": now.year,
        "fills_total": len(rows),
        "maker_share": {
            "by_count_pct": _num(_pct(Decimal(maker_count), Decimal(known_count))),
            "by_fee_eur_pct": _num(_pct(maker_eur, known_eur)),
            "maker_fills": maker_count,
            "taker_fills": known_count - maker_count,
            "unknown_fills": unknown_count,
        },
    }


# -- tax -----------------------------------------------------------------------------


def fees_in_year(conn: sqlite3.Connection, account_id: str, year: int) -> Decimal:
    rows = conn.execute(
        "SELECT fee_eur FROM fills WHERE account_id = ? AND ts_utc >= ? AND ts_utc < ?",
        (account_id, f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
    ).fetchall()
    return sum((Decimal(r["fee_eur"]) for r in rows), ZERO)


def count_fills(conn: sqlite3.Connection, account_id: str, year: int | None = None) -> int:
    if year is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM fills WHERE account_id = ?", (account_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE account_id = ? AND ts_utc >= ? AND ts_utc < ?",
            (account_id, f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
        ).fetchone()
    return int(row["n"])


def tax_summary(
    conn: sqlite3.Connection, account_id: str, year: int, today: date | None = None
) -> dict[str, Any]:
    """Section 23 figures of ``year`` straight from the ledger's own tax report."""
    today = today or utcnow().date()
    report = build_tax_report(conn, account_id, year)
    s = report.summary
    open_lots = [
        {
            "lot_id": lot.lot_id,
            "acquired_at": lot.acquired_at,
            "remaining_qty_btc": format(lot.remaining_qty, "f"),
            "cost_eur_incl_fees": money(lot.cost_eur_incl_fees),
            "frei_ab": lot.free_from.isoformat(),
            "tax_free_now": today >= lot.free_from,
            "days_until_free": max((lot.free_from - today).days, 0),
            "pre_2027": bool(lot.pre_2027),
        }
        for lot in report.open_lots
    ]
    return {
        "year": year,
        "gain_section_23_eur": money(s.gain_section_23),
        "gain_before_expenses_eur": money(s.gain_taxable_before_expenses),
        "sell_fees_eur": money(s.sell_fees_taxable),
        "expenses_eur": money(s.expenses),
        "werbungskosten_eur": money(s.sell_fees_taxable + s.expenses),
        "gain_not_taxable_eur": money(s.gain_not_taxable),
        "proceeds_taxable_eur": money(s.proceeds_taxable),
        "cost_taxable_eur": money(s.cost_taxable),
        "fees_ytd_eur": money(fees_in_year(conn, account_id, year)),
        "freigrenze_eur": money(s.freigrenze),
        "distance_to_freigrenze_eur": money(s.distance_to_freigrenze),
        "freigrenze_exceeded": s.freigrenze_exceeded,
        "counts": {
            "disposals": s.count_total,
            "taxable": s.count_taxable,
            "boundary": s.count_boundary,
            "fills_year": count_fills(conn, account_id, year),
            "fills_total": count_fills(conn, account_id),
            "open_lots": len(open_lots),
        },
        "open_lots": open_lots,
    }


# -- fills ---------------------------------------------------------------------------


def recent_fills(conn: sqlite3.Connection, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, ts_utc, side, amount_btc, price_eur, gross_eur, fee_eur, net_eur, maker_taker, "
        "strategy, signal_reason, dry_run, source, exchange_trade_id FROM fills WHERE account_id = ? "
        "ORDER BY ts_utc DESC, exchange_trade_id DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "ts_utc": str(r["ts_utc"]),
            "side": str(r["side"]),
            "amount_btc": str(r["amount_btc"]),
            "price_eur": money(Decimal(r["price_eur"])),
            "gross_eur": money(Decimal(r["gross_eur"])),
            "fee_eur": money(Decimal(r["fee_eur"])),
            "net_eur": money(Decimal(r["net_eur"])),
            "maker_taker": r["maker_taker"],
            "strategy": r["strategy"],
            "signal_reason": r["signal_reason"],
            "dry_run": bool(r["dry_run"]),
            "source": str(r["source"]),
            "exchange_trade_id": str(r["exchange_trade_id"]),
        }
        for r in rows
    ]


# -- advisor -------------------------------------------------------------------------


def advisor_status(advisor_dir: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """Current ``decision.json`` plus the last ``decisions.jsonl`` line (which may be an error).

    ``decision.json`` is taken as is (``read_json`` accepts any JSON object), so the
    display values are derived here defensively: ``confidence_pct`` is None unless
    ``confidence`` is a number. The page must not 500 because of a hand-edited or
    truncated file.
    """
    now = now or utcnow()
    d = Path(advisor_dir)
    decision = read_json(d / DECISION_FILE)
    tail = read_jsonl_tail(d / DECISIONS_LOG, 1)
    last_log = tail[-1] if tail else None
    valid: bool | None = None
    age_s: int | None = None
    confidence_pct: float | None = None
    if decision is not None:
        vu = _safe_parse(decision.get("valid_until"))
        ca = _safe_parse(decision.get("created_at"))
        valid = vu is not None and vu >= now
        age_s = int((now - ca).total_seconds()) if ca is not None else None
        confidence_pct = _confidence_pct(decision.get("confidence"))
    return {
        "decision": decision,
        "valid": valid,
        "age_s": age_s,
        "confidence_pct": confidence_pct,
        "last_log": _slim_decision(last_log) if last_log else None,
    }


def _confidence_pct(value: object) -> float | None:
    """``confidence`` (0..1) as a percentage rounded to two decimals; None if not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        dec = Decimal(str(value))
    except InvalidOperation:
        return None
    if not dec.is_finite():
        return None
    return _num(q2(dec * HUNDRED))


def decisions_tail(advisor_dir: str | Path, limit: int) -> list[dict[str, Any]]:
    """Last ``limit`` lines of ``decisions.jsonl``, newest first."""
    lines = read_jsonl_tail(Path(advisor_dir) / DECISIONS_LOG, limit)
    return list(reversed(lines))


def _slim_decision(line: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in line.items() if k != "raw_response"}


def _safe_parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


# -- guard ---------------------------------------------------------------------------


def guard_status(guard_dir: str | Path, events: int = 20) -> dict[str, Any]:
    d = Path(guard_dir)
    lock = d / LOCK_FILE
    lock_content: dict[str, Any] | None = None
    if lock.is_file():
        lock_content = read_json(lock)
        if lock_content is None:
            try:
                lock_content = {"raw": lock.read_text(encoding="utf-8", errors="replace")[:500]}
            except OSError:
                lock_content = {}
    tail = read_jsonl_tail(d / EVENTS_FILE, events)
    return {
        "state": read_json(d / STATE_FILE),
        "killswitch": {"active": lock.is_file(), "content": lock_content},
        "events": [_normalise_event(e) for e in reversed(tail)],
    }


def _normalise_event(event: dict[str, Any]) -> dict[str, Any]:
    """Guarantee ``details`` is a dict so the template can iterate it.

    The guard always writes a dict, but a hand-written or older line may carry a
    string or list; that is wrapped as ``{"raw": <value>}`` instead of failing the page.
    """
    details = event.get("details")
    if details is None:
        return {**event, "details": {}}
    if isinstance(details, dict):
        return event
    return {**event, "details": {"raw": details if isinstance(details, str) else str(details)}}


# -- freqtrade -----------------------------------------------------------------------


def bot_status(client: FreqtradeClient | None) -> dict[str, Any]:
    """Health, config, profit, balance and open trades; ``reachable: false`` plus ``error`` when down."""
    if client is None:
        return {"reachable": False, "error": "FT_API_USER/FT_API_PASS nicht gesetzt"}
    try:
        health = client.health()
        config = client.show_config()
        profit = client.profit()
        balance = client.balance()
        open_trades = client.status()
    except FreqtradeError as exc:
        return {"reachable": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the dashboard must never 500 because the bot is odd
        return {"reachable": False, "error": f"{exc.__class__.__name__}: {exc}"}
    return {
        "reachable": True,
        "error": None,
        "health": _pick(health, ("last_process", "last_process_ts", "bot_start", "bot_start_ts")),
        "config": _pick(
            config,
            (
                "dry_run",
                "state",
                "strategy",
                "bot_name",
                "runmode",
                "stake_currency",
                "max_open_trades",
                "timeframe",
                "exchange",
            ),
        ),
        "profit": _pick(
            profit,
            (
                "profit_closed_coin",
                "profit_closed_ratio",
                "profit_all_coin",
                "profit_all_ratio",
                "trade_count",
                "closed_trade_count",
                "winning_trades",
                "losing_trades",
                "max_drawdown",
                "max_drawdown_abs",
                "latest_trade_date",
                "avg_duration",
            ),
        ),
        "balance": {
            "total": balance.get("total"),
            "stake": balance.get("stake"),
            "starting_capital": balance.get("starting_capital"),
            "currencies": [
                _pick(c, ("currency", "balance", "free", "used", "est_stake"))
                for c in balance.get("currencies", [])
                if isinstance(c, dict) and str(c.get("currency", "")).upper() in ("EUR", "BTC")
            ],
        },
        "open_trades": [
            _pick(
                t,
                (
                    "trade_id",
                    "pair",
                    "amount",
                    "stake_amount",
                    "open_date",
                    "open_rate",
                    "current_rate",
                    "profit_ratio",
                    "profit_abs",
                    "enter_tag",
                    "strategy",
                ),
            )
            for t in open_trades
            if isinstance(t, dict)
        ],
    }


def _pick(data: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {k: data.get(k) for k in keys if k in data}


def today_utc() -> date:
    return datetime.now(UTC).date()
