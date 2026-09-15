"""Public Bitvavo REST endpoints (no API key needed).

Base URL ``https://api.bitvavo.com/v2``. Response shapes (checked live on 15.09.2026):

* ``GET /{market}/candles?interval=1d&limit=..&start=..&end=..`` returns
  ``[[timestamp_ms, open, high, low, close, volume], ...]`` with numbers as
  strings, **newest first**. ``fetch_candles`` re-sorts to ascending order.
* ``GET /ticker/price?market=BTC-EUR`` returns ``{"market": ..., "price": "65827"}``.
* ``GET /ticker/book?market=BTC-EUR`` returns ``{"market", "bid", "bidSize", "ask", "askSize"}``.
* ``GET /markets?market=BTC-EUR`` returns one market object (``minOrderInQuoteAsset`` ...).

Errors come back as ``{"errorCode": int, "error": str}``.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

import httpx

BASE_URL = "https://api.bitvavo.com/v2"
DEFAULT_TIMEOUT = 15.0
MAX_CANDLE_LIMIT = 1440
INTERVALS = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1W", "1M")


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
    client: httpx.Client | None = None,
) -> list[Candle]:
    """Fetch OHLCV candles, ascending by time. ``limit`` is capped at 1440 (API maximum)."""
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
    return parse_candles(data)


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
