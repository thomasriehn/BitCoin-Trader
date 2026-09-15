"""Market context for the advisor, built only from the public Bitvavo API.

No API keys, no news, no external feeds. Optionally the Freqtrade REST API
is asked for the bot status (position yes/no, unrealised profit); if that
fails the context still gets built and ``bot.available`` is ``false``.

The context is a plain ``dict`` of JSON-native values so it can be
serialised deterministically (sorted keys, compact separators) and hashed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from btctrader.common import bitvavo_public
from btctrader.common.bitvavo_public import BitvavoError, Candle
from btctrader.common.db import iso_utc, utcnow
from btctrader.common.ftapi import FreqtradeClient, FreqtradeError

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


def stable_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8 (no ASCII escaping)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return iso_utc(value)
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


def _candle_rows(candles: Sequence[Candle], *, date_only: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in candles:
        ts = datetime.fromtimestamp(c.ts_ms / 1000.0, tz=UTC)
        rows.append(
            {
                "t": ts.date().isoformat() if date_only else iso_utc(ts),
                "o": str(c.open),
                "h": str(c.high),
                "l": str(c.low),
                "c": str(c.close),
                "v": str(c.volume),
            }
        )
    return rows


# -- market part ---------------------------------------------------------------------


def market_features(daily: Sequence[Candle], fourhour: Sequence[Candle]) -> dict[str, Any]:
    """Compute all indicator fields from candle lists (ascending by time). Pure function."""
    if not daily:
        raise ValueError("no daily candles")
    closes = _closes(daily)
    last_close = closes[-1]
    sma50 = sma(closes, SMA_SHORT)
    sma200 = sma(closes, SMA_LONG)
    high_365, dd = drawdown_from_high(daily, HIGH_LOOKBACK_DAYS)
    vol_ratio, vol_label = volume_trend(daily, VOLUME_SHORT_DAYS, VOLUME_LONG_DAYS)
    last_ts = datetime.fromtimestamp(daily[-1].ts_ms / 1000.0, tz=UTC)
    return {
        "market": MARKET,
        "price_eur": str(daily[-1].close),
        "last_daily_candle": last_ts.date().isoformat(),
        "daily_candles_available": len(daily),
        "candles_1d": _candle_rows(daily[-DAILY_CANDLES_IN_CONTEXT:], date_only=True),
        "candles_4h": _candle_rows(fourhour[-FOURHOUR_CANDLES_IN_CONTEXT:], date_only=False),
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
    """Fetch the daily and 4h candle series from the public Bitvavo API."""
    daily = bitvavo_public.fetch_candles(MARKET, "1d", DAILY_CANDLES_TO_FETCH, client=client)
    fourhour = bitvavo_public.fetch_candles(MARKET, "4h", FOURHOUR_CANDLES_IN_CONTEXT, client=client)
    if len(daily) < 2:
        raise BitvavoError("too few daily candles returned")
    return daily, fourhour


# -- bot part ------------------------------------------------------------------------


def bot_status(ft: FreqtradeClient | None) -> dict[str, Any]:
    """Position summary from the Freqtrade API; never raises, reports ``available: false`` instead."""
    if ft is None:
        return {"available": False, "reason": "not configured"}
    try:
        trades = ft.status()
    except (FreqtradeError, httpx.HTTPError) as exc:
        log.warning("freqtrade status unavailable, continuing without bot context: %s", exc)
        return {"available": False, "reason": str(exc)[:200]}
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
    ft: FreqtradeClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch candles, compute features, optionally add bot status; returns a JSON-native dict."""
    daily, fourhour = fetch_market(client)
    return assemble_context(daily, fourhour, bot=bot_status(ft), now=now)


def assemble_context(
    daily: Sequence[Candle],
    fourhour: Sequence[Candle],
    *,
    bot: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the context dict from already fetched candles (used by tests and evaluate)."""
    ts = now or utcnow()
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "as_of": iso_utc(ts),
        "market": market_features(daily, fourhour),
        "bot": bot if bot is not None else {"available": False, "reason": "not configured"},
    }
