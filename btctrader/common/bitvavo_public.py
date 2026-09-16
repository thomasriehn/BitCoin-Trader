"""Public Bitvavo REST endpoints (no API key needed).

Base URL ``https://api.bitvavo.com/v2``. Response shapes (checked live on 15.09.2026):

* ``GET /{market}/candles?interval=1d&limit=..&start=..&end=..`` returns
  ``[[timestamp_ms, open, high, low, close, volume], ...]`` with numbers as
  strings, **newest first**. ``fetch_candles`` re-sorts to ascending order.
  The newest row is the **still open** candle of the current interval (checked
  live: at 22:46 UTC the 1d response ends with today's 00:00 UTC candle), so its
  close changes until the interval ends. Pass ``drop_incomplete=True`` or use
  ``closed_only`` / ``is_closed`` when a consumer needs closed candles only
  (daily closes, SMA/volatility windows, benchmark prices).
* ``GET /ticker/price?market=BTC-EUR`` returns ``{"market": ..., "price": "65827"}``.
* ``GET /ticker/book?market=BTC-EUR`` returns ``{"market", "bid", "bidSize", "ask", "askSize"}``.
* ``GET /markets?market=BTC-EUR`` returns one market object (``minOrderInQuoteAsset`` ...).

Errors come back as ``{"errorCode": int, "error": str}``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

import httpx

BASE_URL = "https://api.bitvavo.com/v2"
DEFAULT_TIMEOUT = 15.0
MAX_CANDLE_LIMIT = 1440
INTERVALS = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1W", "1M")
_MINUTE_MS = 60_000
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS
# Fixed-length intervals in milliseconds; "1M" is a calendar month and handled in interval_end_ms.
INTERVAL_MS: dict[str, int] = {
    "1m": _MINUTE_MS,
    "5m": 5 * _MINUTE_MS,
    "15m": 15 * _MINUTE_MS,
    "30m": 30 * _MINUTE_MS,
    "1h": _HOUR_MS,
    "2h": 2 * _HOUR_MS,
    "4h": 4 * _HOUR_MS,
    "6h": 6 * _HOUR_MS,
    "8h": 8 * _HOUR_MS,
    "12h": 12 * _HOUR_MS,
    "1d": _DAY_MS,
    "1W": 7 * _DAY_MS,
}


class BitvavoError(Exception):
    """Raised for transport errors, HTTP errors or unexpected payloads."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Candle(NamedTuple):
    ts_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _get(path: str, params: dict[str, Any] | None, client: httpx.Client | None) -> Any:
    own = client is None
    http = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        try:
            resp = http.get(f"{BASE_URL}{path}", params=params, timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as exc:
            raise BitvavoError(f"GET {path} failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise BitvavoError(
                f"GET {path}: response is not JSON (HTTP {resp.status_code})", resp.status_code
            ) from exc
        if resp.status_code >= 400 or (isinstance(data, dict) and "errorCode" in data):
            detail = data.get("error", data) if isinstance(data, dict) else data
            raise BitvavoError(f"GET {path}: HTTP {resp.status_code}: {detail}", resp.status_code)
        return data
    finally:
        if own:
            http.close()


def _dec(value: Any, what: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BitvavoError(f"{what}: cannot parse {value!r} as Decimal") from exc
    if not result.is_finite():
        raise BitvavoError(f"{what}: non-finite value {value!r}")
    return result


def interval_end_ms(ts_ms: int, interval: str) -> int:
    """Timestamp (ms) at which the candle starting at ``ts_ms`` closes."""
    if interval == "1M":
        start = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
        return int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)
    try:
        return ts_ms + INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"unsupported interval {interval!r}; use one of {', '.join(INTERVALS)}") from None


def is_closed(candle: Candle, interval: str, now_ms: int | None = None) -> bool:
    """True if the candle's interval has ended, i.e. its close is final."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    return interval_end_ms(candle.ts_ms, interval) <= now


def closed_only(candles: Sequence[Candle], interval: str, now_ms: int | None = None) -> list[Candle]:
    """Return only the candles whose interval has already ended (drops the open one)."""
    return [c for c in candles if is_closed(c, interval, now_ms)]


def parse_candles(rows: Sequence[Sequence[Any]]) -> list[Candle]:
    """Convert raw ``[[ts, o, h, l, c, v], ...]`` rows to ``Candle`` objects, ascending by time."""
    candles: list[Candle] = []
    for row in rows:
        if len(row) < 6:
            raise BitvavoError(f"candle row too short: {row!r}")
        candles.append(
            Candle(
                ts_ms=int(row[0]),
                open=_dec(row[1], "open"),
                high=_dec(row[2], "high"),
                low=_dec(row[3], "low"),
                close=_dec(row[4], "close"),
                volume=_dec(row[5], "volume"),
            )
        )
    candles.sort(key=lambda c: c.ts_ms)
    return candles


def fetch_candles(
    market: str = "BTC-EUR",
    interval: str = "1d",
    limit: int = 1440,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    drop_incomplete: bool = False,
    client: httpx.Client | None = None,
) -> list[Candle]:
    """Fetch OHLCV candles, ascending by time. ``limit`` is capped at 1440 (API maximum).

    The API includes the still open candle of the current interval as the
    newest row; ``drop_incomplete=True`` removes it (and anything else not yet
    closed at call time) so the result contains final closes only.
    """
    if interval not in INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}; use one of {', '.join(INTERVALS)}")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    params: dict[str, Any] = {"interval": interval, "limit": min(limit, MAX_CANDLE_LIMIT)}
    if start_ms is not None:
        params["start"] = int(start_ms)
    if end_ms is not None:
        params["end"] = int(end_ms)
    data = _get(f"/{market}/candles", params, client)
    if not isinstance(data, list):
        raise BitvavoError(f"candles: unexpected payload {type(data).__name__}")
    candles = parse_candles(data)
    if drop_incomplete:
        candles = closed_only(candles, interval)
    return candles


def fetch_ticker_price(market: str = "BTC-EUR", *, client: httpx.Client | None = None) -> Decimal:
    """Last traded price via ``GET /ticker/price?market=``."""
    data = _get("/ticker/price", {"market": market}, client)
    if not isinstance(data, dict) or "price" not in data:
        raise BitvavoError(f"ticker/price: unexpected payload {data!r}")
    return _dec(data["price"], "price")


def fetch_book_top(market: str = "BTC-EUR", *, client: httpx.Client | None = None) -> tuple[Decimal, Decimal]:
    """Best bid and ask via ``GET /ticker/book?market=`` as ``(bid, ask)``."""
    data = _get("/ticker/book", {"market": market}, client)
    if not isinstance(data, dict) or "bid" not in data or "ask" not in data:
        raise BitvavoError(f"ticker/book: unexpected payload {data!r}")
    return _dec(data["bid"], "bid"), _dec(data["ask"], "ask")


def fetch_market_info(market: str = "BTC-EUR", *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Market metadata via ``GET /markets?market=`` (min order size, tick size, status ...)."""
    data = _get("/markets", {"market": market}, client)
    if isinstance(data, list):
        # Some deployments answer with a one-element list.
        matches = [m for m in data if isinstance(m, dict) and m.get("market") == market]
        if not matches:
            raise BitvavoError(f"markets: {market} not found")
        data = matches[0]
    if not isinstance(data, dict):
        raise BitvavoError(f"markets: unexpected payload {data!r}")
    return data
