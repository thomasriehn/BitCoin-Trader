"""Market context for the advisor, built only from the public Bitvavo API.

No API keys, no news, no external feeds. Optionally a ``status`` callable
(the ``status()`` method of the Freqtrade REST client, nothing more) is asked
for the bot status (position yes/no, unrealised profit); if that fails the
context still gets built and ``bot.available`` is ``false``.

Bitvavo returns the still open candle of the current interval as the newest
row. All indicators (SMA, volatility, returns, drawdown, volume trend) and the
``candles_1d`` / ``candles_4h`` rows use closed candles only, exactly like the
strategy (``process_only_new_candles``). The open daily candle is reported
separately as ``open_candle_1d`` and its close is the live ``price_eur``.

The context is a plain ``dict`` of JSON-native values so it can be
serialised deterministically (sorted keys, compact separators) and hashed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from btctrader.common import bitvavo_public
from btctrader.common.bitvavo_public import BitvavoError, Candle
from btctrader.common.db import utcnow
from btctrader.common.ftapi import FreqtradeError

log = logging.getLogger(__name__)

MARKET = "BTC-EUR"
CONTEXT_SCHEMA_VERSION = 1
DAILY_CANDLES_IN_CONTEXT = 30
FOURHOUR_CANDLES_IN_CONTEXT = 24
DAILY_CANDLES_TO_FETCH = 400  # >= 365 for the yearly high, >= 200 for SMA200 plus a margin
SMA_SHORT = 50
SMA_LONG = 200
VOL_LOOKBACK_DAYS = 30
RETURN_WINDOWS_DAYS = (1, 7, 30, 90)
HIGH_LOOKBACK_DAYS = 365
VOLUME_SHORT_DAYS = 7
VOLUME_LONG_DAYS = 30
VOLUME_TREND_BAND = 0.15  # +-15 % ratio counts as flat
VOLUME_DECIMALS = Decimal("0.01")  # candle volumes are rounded to 2 decimals to save prompt tokens

StatusFn = Callable[[], Any]
"""Read-only callable returning the Freqtrade ``/status`` list (open trades)."""


def iso_seconds(dt: datetime) -> str:
    """ISO-8601 UTC with ``Z`` and whole seconds (``2026-09-15T13:07:02Z``), the format of section 6."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8 (no ASCII escaping)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return iso_seconds(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def sha256_prefixed(text: str) -> str:
    """``sha256:<hex>`` of the UTF-8 encoded text."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def context_hash(context: dict[str, Any]) -> str:
    """Hash of the stable serialisation of the context (order of insertion does not matter)."""
    return sha256_prefixed(stable_json(context))


# -- numeric helpers (float is fine here: analytics, not money) ------------------------


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _closes(candles: Sequence[Candle]) -> list[float]:
    return [float(c.close) for c in candles]


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values, None if not enough data."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def pct_distance(value: float, reference: float | None) -> float | None:
    """Percent distance of ``value`` from ``reference`` (positive = above)."""
    if reference is None or reference == 0:
        return None
    return (value / reference - 1.0) * 100.0


def realized_vol_annualised_pct(closes: Sequence[float], lookback: int) -> float | None:
    """Annualised standard deviation of daily log returns over ``lookback`` returns, in percent."""
    if len(closes) < lookback + 1:
        return None
    window = closes[-(lookback + 1) :]
    rets = [math.log(b / a) for a, b in zip(window[:-1], window[1:], strict=True) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.0) * 100.0


def trailing_return_pct(closes: Sequence[float], days: int) -> float | None:
    """Return over the last ``days`` daily candles in percent, None if not enough data."""
    if days <= 0 or len(closes) < days + 1:
        return None
    base = closes[-1 - days]
    if base <= 0:
        return None
    return (closes[-1] / base - 1.0) * 100.0


def drawdown_from_high(candles: Sequence[Candle], lookback: int) -> tuple[float | None, float | None]:
    """``(high, drawdown_pct)`` where high is the max candle high over the last ``lookback`` candles."""
    if not candles:
        return None, None
    window = candles[-lookback:]
    high = max(float(c.high) for c in window)
    last = float(candles[-1].close)
    if high <= 0:
        return high, None
    return high, (last / high - 1.0) * 100.0


def volume_trend(candles: Sequence[Candle], short: int, long: int) -> tuple[float | None, str]:
    """Ratio of mean volume over the last ``short`` candles vs the ``long`` candles before them."""
    if len(candles) < short + long:
        return None, "unknown"
    recent = candles[-short:]
    earlier = candles[-(short + long) : -short]
    mean_recent = sum(float(c.volume) for c in recent) / short
    mean_earlier = sum(float(c.volume) for c in earlier) / long
    if mean_earlier <= 0:
        return None, "unknown"
    ratio = mean_recent / mean_earlier
    if ratio > 1.0 + VOLUME_TREND_BAND:
        label = "rising"
    elif ratio < 1.0 - VOLUME_TREND_BAND:
        label = "falling"
    else:
        label = "flat"
    return ratio, label


def _candle_row(c: Candle, *, date_only: bool) -> dict[str, Any]:
    ts = datetime.fromtimestamp(c.ts_ms / 1000.0, tz=UTC)
    return {
        "t": ts.date().isoformat() if date_only else iso_seconds(ts),
        "o": str(c.open),
        "h": str(c.high),
        "l": str(c.low),
        "c": str(c.close),
        "v": str(c.volume.quantize(VOLUME_DECIMALS, rounding=ROUND_HALF_UP)),
    }


def _candle_rows(candles: Sequence[Candle], *, date_only: bool) -> list[dict[str, Any]]:
    return [_candle_row(c, date_only=date_only) for c in candles]


def split_open_candle(
    candles: Sequence[Candle], interval: str, now: datetime
) -> tuple[list[Candle], Candle | None]:
    """``(closed candles, open candle or None)``; the open candle is the one whose interval has not ended."""
    now_ms = int(now.timestamp() * 1000)
    closed = bitvavo_public.closed_only(candles, interval, now_ms)
    still_open = [c for c in candles if not bitvavo_public.is_closed(c, interval, now_ms)]
    return closed, (still_open[-1] if still_open else None)


# -- market part ---------------------------------------------------------------------


def market_features(
    daily: Sequence[Candle], fourhour: Sequence[Candle], *, now: datetime | None = None
) -> dict[str, Any]:
    """Compute all indicator fields from candle lists (ascending by time).

    Indicators and candle rows are computed on closed candles only (as of
    ``now``, default: current time); the open daily candle, if present, is
    reported as ``open_candle_1d`` and provides the live ``price_eur``. This
    keeps ``volume_trend`` and ``dist_sma200_pct`` stable within a day and
    identical to what the daily strategy sees.
    """
    ts_now = now or utcnow()
    closed_daily, open_daily = split_open_candle(daily, "1d", ts_now)
    closed_4h, _ = split_open_candle(fourhour, "4h", ts_now)
    if not closed_daily:
        raise ValueError("no closed daily candles")
    closes = _closes(closed_daily)
    last_close = closes[-1]
    live = open_daily.close if open_daily is not None else closed_daily[-1].close
    sma50 = sma(closes, SMA_SHORT)
    sma200 = sma(closes, SMA_LONG)
    high_365, dd = drawdown_from_high(closed_daily, HIGH_LOOKBACK_DAYS)
    vol_ratio, vol_label = volume_trend(closed_daily, VOLUME_SHORT_DAYS, VOLUME_LONG_DAYS)
    last_ts = datetime.fromtimestamp(closed_daily[-1].ts_ms / 1000.0, tz=UTC)
    return {
        "market": MARKET,
        "price_eur": str(live),
        "last_close_eur": str(closed_daily[-1].close),
        "last_daily_candle": last_ts.date().isoformat(),
        "daily_candles_available": len(closed_daily),
        "candles_1d": _candle_rows(closed_daily[-DAILY_CANDLES_IN_CONTEXT:], date_only=True),
        "open_candle_1d": _candle_row(open_daily, date_only=True) if open_daily is not None else None,
        "candles_4h": _candle_rows(closed_4h[-FOURHOUR_CANDLES_IN_CONTEXT:], date_only=False),
        "sma50_eur": _round(sma50, 2),
        "sma200_eur": _round(sma200, 2),
        "dist_sma50_pct": _round(pct_distance(last_close, sma50)),
        "dist_sma200_pct": _round(pct_distance(last_close, sma200)),
        "realized_vol_30d_annualised_pct": _round(realized_vol_annualised_pct(closes, VOL_LOOKBACK_DAYS), 2),
        "returns_pct": {f"{d}d": _round(trailing_return_pct(closes, d)) for d in RETURN_WINDOWS_DAYS},
        "high_365d_eur": _round(high_365, 2),
        "drawdown_from_365d_high_pct": _round(dd),
        "volume_ratio_7d_vs_30d": _round(vol_ratio),
        "volume_trend": vol_label,
    }


def fetch_market(client: httpx.Client | None = None) -> tuple[list[Candle], list[Candle]]:
    """Fetch the daily and 4h candle series from the public Bitvavo API (newest row is still open)."""
    daily = bitvavo_public.fetch_candles(MARKET, "1d", DAILY_CANDLES_TO_FETCH, client=client)
    # one extra 4h candle so that 24 closed ones remain after the open one is set aside
    fourhour = bitvavo_public.fetch_candles(MARKET, "4h", FOURHOUR_CANDLES_IN_CONTEXT + 1, client=client)
    if len(daily) < 2:
        raise BitvavoError("too few daily candles returned")
    return daily, fourhour


# -- bot part ------------------------------------------------------------------------


def bot_status(status: StatusFn | None) -> dict[str, Any]:
    """Position summary via the read-only ``status`` callable; never raises, reports ``available: false``.

    Only ``FreqtradeClient.status`` is handed in, not the client, so this
    module cannot reach any control endpoint of the bot (section 6).
    """
    if status is None:
        return {"available": False, "reason": "not configured"}
    try:
        trades = status()
    except (FreqtradeError, httpx.HTTPError) as exc:
        log.warning("freqtrade status unavailable, continuing without bot context: %s", exc)
        return {"available": False, "reason": str(exc)[:200]}
    if not isinstance(trades, list):
        return {"available": False, "reason": "unexpected status payload"}
    open_trades = [t for t in trades if isinstance(t, dict) and t.get("is_open") is not False]
    profit_pct = [float(t["profit_pct"]) for t in open_trades if t.get("profit_pct") is not None]
    profit_abs = [float(t["profit_abs"]) for t in open_trades if t.get("profit_abs") is not None]
    stake = [float(t["stake_amount"]) for t in open_trades if t.get("stake_amount") is not None]
    return {
        "available": True,
        "in_position": bool(open_trades),
        "open_trades": len(open_trades),
        "unrealized_profit_pct": _round(sum(profit_pct) / len(profit_pct), 2) if profit_pct else None,
        "unrealized_profit_eur": _round(sum(profit_abs), 2) if profit_abs else None,
        "stake_eur": _round(sum(stake), 2) if stake else None,
    }


# -- assembly ------------------------------------------------------------------------


def build_context(
    *,
    client: httpx.Client | None = None,
    status: StatusFn | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch candles, compute features, optionally add bot status; returns a JSON-native dict."""
    daily, fourhour = fetch_market(client)
    return assemble_context(daily, fourhour, bot=bot_status(status), now=now)


def assemble_context(
    daily: Sequence[Candle],
    fourhour: Sequence[Candle],
    *,
    bot: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the context dict from already fetched candles (``now`` decides which candles are closed)."""
    ts = now or utcnow()
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "as_of": iso_seconds(ts),
        "market": market_features(daily, fourhour, now=ts),
        "bot": bot if bot is not None else {"available": False, "reason": "not configured"},
    }
