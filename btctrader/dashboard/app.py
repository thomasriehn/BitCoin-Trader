"""FastAPI dashboard: read-only view on ledger, advisor, guard and the Freqtrade API.

Endpoints (docs/KOMPONENTEN.md section 10):

* ``GET /``                       HTML page (htmx polls the partials below every 30 s)
* ``GET /api/equity?days=365``    equity / B&H / DCA / drawdown series from ``equity_daily``
* ``GET /api/fees``               cumulative fees, last 30 days, maker share
* ``GET /api/tax?year=YYYY``      section 23 figures of the year, open lots with ``frei_ab``
* ``GET /api/status``             Freqtrade (degrades to ``reachable: false``), guard, advisor, events
* ``GET /api/decisions?limit=50`` tail of ``decisions.jsonl`` (newest first)
* ``GET /healthz``                liveness
* ``GET /partials/{tiles,fills,events}`` HTML fragments for htmx

No login (the page is only reachable through Tailscale), no exchange keys,
nothing is written. ``create_app`` takes explicit ``Settings`` for tests; the
module level ``app`` loads them lazily from the environment on first use. If
that fails (invalid ``btctrader.env``) the page still renders with defaults and
shows the error as a badge in the header instead of hiding it in the log.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from btctrader.common.config import ConfigError, Settings, load_settings
from btctrader.common.db import connect, iso_utc, parse_iso, utcnow
from btctrader.common.ftapi import FreqtradeClient
from btctrader.dashboard import data
from btctrader.dashboard.data import MAX_EQUITY_DAYS, MAX_LIST_LIMIT

log = logging.getLogger("btctrader.dashboard")

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

# Pinned CDN fallbacks (must match static/vendor/fetch-vendor.sh).
CHARTJS_VERSION = "4.5.1"
HTMX_VERSION = "2.0.10"
CHARTJS_CDN = f"https://cdn.jsdelivr.net/npm/chart.js@{CHARTJS_VERSION}/dist/chart.umd.js"
HTMX_CDN = f"https://cdn.jsdelivr.net/npm/htmx.org@{HTMX_VERSION}/dist/htmx.min.js"
CHARTJS_SRI = "sha384-hfkuqrKeWFmnTMWN31VWyoe8xgdTADD11kgxmdpx2uyE6j5Az5uZq6u6AKYYmAOw"
HTMX_SRI = "sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"

DEFAULT_EQUITY_DAYS = 365
FILLS_ON_PAGE = 50
EVENTS_ON_PAGE = 20
# Short timeout for the polled page: a hanging Freqtrade (accepts TCP, never answers) must
# not stall every 30 s tile refresh for 6 x 10 s. Guard and ledger keep the client default.
FT_TIMEOUT_S = 3.0


# -- formatting helpers (Jinja filters) ----------------------------------------------


def fmt_eur(value: object, places: int = 2) -> str:
    """German money format: ``1.234,56 €``. Accepts Decimal, str, int, float or None."""
    dec = _to_decimal(value)
    if dec is None:
        return "–"
    text = format(dec, f",.{places}f")
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " €"


def fmt_num(value: object, places: int = 2) -> str:
    dec = _to_decimal(value)
    if dec is None:
        return "–"
    text = format(dec, f",.{places}f")
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_pct(value: object, places: int = 2, signed: bool = True) -> str:
    dec = _to_decimal(value)
    if dec is None:
        return "–"
    sign = "+" if signed and dec > 0 else ""
    return sign + fmt_num(dec, places) + " %"


def fmt_btc(value: object) -> str:
    dec = _to_decimal(value)
    if dec is None:
        return "–"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") + " BTC"


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def resolve_tz(tz_name: str) -> ZoneInfo:
    """``TZ_DISPLAY`` as a ZoneInfo; an unknown name falls back to UTC (the header shows the result)."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def make_local_ts(tz_name: str) -> Any:
    """Return a filter that renders an ISO UTC timestamp in the display time zone."""
    tz = resolve_tz(tz_name)

    def local_ts(value: object, fmt: str = "%d.%m.%Y %H:%M") -> str:
        if not isinstance(value, str) or not value:
            return "–"
        try:
            dt = parse_iso(value)
        except ValueError:
            return value
        return dt.astimezone(tz).strftime(fmt)

    return local_ts


def de_date(value: object) -> str:
    """``YYYY-MM-DD`` -> ``DD.MM.YYYY``; other values are returned unchanged."""
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return f"{value[8:10]}.{value[5:7]}.{value[0:4]}"
    return "–" if value in (None, "") else str(value)


# -- app factory ----------------------------------------------------------------------


def create_app(settings: Settings | None = None, *, ft_client: FreqtradeClient | None = None) -> FastAPI:
    app = FastAPI(title="btctrader dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.settings_error = None
    app.state.ft_client = ft_client
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["eur"] = fmt_eur
    templates.env.filters["num"] = fmt_num
    templates.env.filters["pct"] = fmt_pct
    templates.env.filters["btc"] = fmt_btc
    templates.env.filters["de_date"] = de_date
    templates.env.filters["local_ts"] = make_local_ts((settings or Settings()).tz_display)
    app.state.templates = templates

    # -- helpers -----------------------------------------------------------------

    def get_settings() -> Settings:
        current: Settings | None = app.state.settings
        if current is None:
            try:
                current = load_settings()
            except ConfigError as exc:
                log.error("settings invalid, using defaults: %s", exc)
                app.state.settings_error = str(exc)
                current = Settings()
            app.state.settings = current
            templates.env.filters["local_ts"] = make_local_ts(current.tz_display)
        return current

    def get_ft_client(s: Settings) -> FreqtradeClient | None:
        client: FreqtradeClient | None = app.state.ft_client
        if client is None and s.ft_api_user and s.ft_api_pass:
            client = FreqtradeClient(s.ft_api_url, s.ft_api_user, s.ft_api_pass, timeout=FT_TIMEOUT_S)
            app.state.ft_client = client
        return client

    @contextmanager
    def ledger(s: Settings) -> Iterator[tuple[sqlite3.Connection, bool]]:
        """Yield ``(connection, available)``; a missing ledger yields an empty in-memory DB.

        The real ledger connection is read-only and inside one read transaction
        (see ``data.open_ledger``), so all queries of a request see one snapshot.
        """
        conn = data.open_ledger(s.ledger_db_path)
        available = conn is not None
        if conn is None:
            conn = connect(":memory:")
        try:
            yield conn, available
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def overview(s: Settings, now: datetime) -> dict[str, Any]:
        """Everything the tiles need, computed once per request."""
        with ledger(s) as (conn, available):
            equity = data.equity_series(conn, DEFAULT_EQUITY_DAYS)
            fees = data.fees_summary(conn, s.ledger_account_id, now)
            tax = data.tax_summary(conn, s.ledger_account_id, now.year, now.date())
        bot = data.bot_status(get_ft_client(s))
        guard = data.guard_status(s.guard_dir, EVENTS_ON_PAGE)
        advisor = data.advisor_status(s.advisor_dir, now)
        pending_lots = [lot for lot in tax["open_lots"] if not lot["tax_free_now"]]
        next_free = min(pending_lots, key=_free_key, default=None)
        return {
            "ledger_available": available,
            "equity": equity["summary"],
            "equity_days": DEFAULT_EQUITY_DAYS,
            "fees": fees,
            "tax": tax,
            "next_free_lot": next_free,
            "bot": bot,
            "guard": guard,
            "advisor": advisor,
            "now": iso_utc(now),
        }

    def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    # -- HTML --------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        s = get_settings()
        now = utcnow()
        ctx = overview(s, now)
        with ledger(s) as (conn, _):
            ctx["fills"] = data.recent_fills(conn, s.ledger_account_id, FILLS_ON_PAGE)
        ctx["events"] = ctx["guard"]["events"]
        ctx.update(
            {
                "chartjs_cdn": CHARTJS_CDN,
                "chartjs_sri": CHARTJS_SRI,
                "htmx_cdn": HTMX_CDN,
                "htmx_sri": HTMX_SRI,
                "tz_display": resolve_tz(s.tz_display).key,
                "settings_error": app.state.settings_error,
                "account_id": s.ledger_account_id,
            }
        )
        return render(request, "index.html", ctx)

    @app.get("/partials/tiles", response_class=HTMLResponse, include_in_schema=False)
    def partial_tiles(request: Request) -> HTMLResponse:
        s = get_settings()
        ctx = overview(s, utcnow())
        ctx["oob"] = True  # also refresh the header's "Stand" via an htmx out-of-band swap
        return render(request, "_tiles.html", ctx)

    @app.get("/partials/fills", response_class=HTMLResponse, include_in_schema=False)
    def partial_fills(request: Request) -> HTMLResponse:
        s = get_settings()
        with ledger(s) as (conn, available):
            fills = data.recent_fills(conn, s.ledger_account_id, FILLS_ON_PAGE)
        return render(request, "_fills.html", {"fills": fills, "ledger_available": available})

    @app.get("/partials/events", response_class=HTMLResponse, include_in_schema=False)
    def partial_events(request: Request) -> HTMLResponse:
        s = get_settings()
        guard = data.guard_status(s.guard_dir, EVENTS_ON_PAGE)
        return render(request, "_events.html", {"events": guard["events"]})

    # -- JSON API ----------------------------------------------------------------

    @app.get("/api/equity")
    def api_equity(days: int = Query(DEFAULT_EQUITY_DAYS, ge=1, le=MAX_EQUITY_DAYS)) -> JSONResponse:
        s = get_settings()
        with ledger(s) as (conn, available):
            payload = data.equity_series(conn, days)
        payload["ledger_available"] = available
        return JSONResponse(payload)

    @app.get("/api/fees")
    def api_fees() -> JSONResponse:
        s = get_settings()
        with ledger(s) as (conn, available):
            payload = data.fees_summary(conn, s.ledger_account_id, utcnow())
        payload["ledger_available"] = available
        return JSONResponse(payload)

    @app.get("/api/tax")
    def api_tax(year: int | None = Query(None, ge=2009, le=2100)) -> JSONResponse:
        s = get_settings()
        now = utcnow()
        with ledger(s) as (conn, available):
            payload = data.tax_summary(conn, s.ledger_account_id, year or now.year, now.date())
        payload["ledger_available"] = available
        return JSONResponse(payload)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        s = get_settings()
        now = utcnow()
        return JSONResponse(
            {
                "generated_at": iso_utc(now),
                "bot": data.bot_status(get_ft_client(s)),
                "guard": data.guard_status(s.guard_dir, EVENTS_ON_PAGE),
                "advisor": data.advisor_status(s.advisor_dir, now),
                "advisor_mode": s.advisor_mode,
            }
        )

    @app.get("/api/decisions")
    def api_decisions(limit: int = Query(50, ge=1, le=MAX_LIST_LIMIT)) -> JSONResponse:
        s = get_settings()
        return JSONResponse({"limit": limit, "decisions": data.decisions_tail(s.advisor_dir, limit)})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        s = get_settings()
        return JSONResponse(
            {"status": "ok", "time": iso_utc(utcnow()), "ledger": Path(s.ledger_db_path).is_file()}
        )

    return app


def _free_key(lot: dict[str, Any]) -> str:
    return str(lot["frei_ab"])


app = create_app()
