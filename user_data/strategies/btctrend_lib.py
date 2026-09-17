"""Pure helper functions for the BtcTrend strategies.

This module has no Freqtrade dependency (only numpy and pandas) so that the
trading rules can be unit-tested without a Freqtrade installation. Freqtrade
puts the strategy directory on ``sys.path`` while it imports a strategy file
(``freqtrade.resolvers.iresolver.PathModifier``), which is why the strategies
can simply ``import btctrend_lib``.

All functions are deterministic and side-effect free. They operate on floats
because Freqtrade itself works with floats; the tax ledger (a separate
component) is the place for Decimal arithmetic.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 365
DECISION_SCHEMA_VERSION = 1
VALID_REGIMES = frozenset({"risk_on", "neutral", "risk_off"})


def hysteresis_signals(close: pd.Series, sma: pd.Series, band_pct: float) -> tuple[pd.Series, pd.Series]:
    """Return (enter, exit) boolean series for a hysteresis band around a moving average.

    ``enter`` is True when ``close > sma * (1 + band_pct)``, ``exit`` is True when
    ``close < sma * (1 - band_pct)``. Rows where the SMA is not yet defined (NaN)
    are False in both series. Both conditions only look at the same row, so the
    result is free of lookahead by construction.
    """
    if band_pct < 0:
        raise ValueError("band_pct must be >= 0")
    upper = sma * (1.0 + band_pct)
    lower = sma * (1.0 - band_pct)
    enter = (close > upper) & sma.notna()
    exit_ = (close < lower) & sma.notna()
    return enter.fillna(False).astype(bool), exit_.fillna(False).astype(bool)


def realized_volatility(
    close: pd.Series, lookback: int, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> pd.Series:
    """Annualised realized volatility of simple returns over a trailing window.

    Uses only past and current rows (``rolling`` with a right-aligned window),
    the sample standard deviation (``ddof=1``) and ``sqrt(periods_per_year)``.
    The first ``lookback`` rows are NaN.
    """
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    returns = close.pct_change()
    # rolling(...).apply with raw=True recomputes each window from scratch. Pandas' default
    # rolling std uses an online algorithm whose rounding depends on where the series starts,
    # which shows up as -0.000% noise in Freqtrade's recursive-analysis.
    window_std = returns.rolling(window=lookback, min_periods=lookback).apply(
        lambda values: float(np.std(values, ddof=1)), raw=True
    )
    return window_std * math.sqrt(periods_per_year)


def exposure_from_vol(target_vol: float, realized_vol: float | None, fallback: float = 0.0) -> float:
    """Volatility-targeted exposure ``min(1, target_vol / realized_vol)``.

    Returns ``fallback`` (default 0.0: no exposure without a volatility estimate;
    the rule never fails open to 100 %) when the realized volatility is unknown
    (None or NaN) and 1.0 when it is zero (a flat market carries no measured
    risk). The result is always within ``[0, 1]``.
    """
    if target_vol <= 0:
        raise ValueError("target_vol must be > 0")
    if realized_vol is None or not math.isfinite(realized_vol):
        return _clip01(fallback)
    if realized_vol <= 0:
        return 1.0
    return _clip01(target_vol / realized_vol)


def min_order_value(capital: float, min_order_eur: float, min_order_pct: float) -> float:
    """Smallest order value (in stake currency) that is worth sending.

    ``max(min_order_eur, min_order_pct * capital)``: the exchange minimum or a
    percentage of the account, whichever is larger. This keeps fee-heavy micro
    rebalances away.
    """
    if capital < 0 or min_order_eur < 0 or min_order_pct < 0:
        raise ValueError("capital, min_order_eur and min_order_pct must be >= 0")
    return max(min_order_eur, min_order_pct * capital)


def rebalance_amount(
    capital: float,
    position_value: float,
    target_exposure: float,
    rebalance_band: float,
    min_order: float,
) -> float | None:
    """Decide whether and how much to rebalance an open position.

    Returns the order value in stake currency: positive means buy more,
    negative means sell part of the position, None means no order. An order is
    only proposed when the exposure drift ``|actual - target|`` exceeds
    ``rebalance_band`` (in exposure units, e.g. 0.15 = 15 percentage points)
    and the resulting order value is at least ``min_order``.
    """
    if capital <= 0:
        return None
    actual = position_value / capital
    drift = target_exposure - actual
    if abs(drift) <= rebalance_band:
        return None
    order_value = drift * capital
    if abs(order_value) < min_order:
        return None
    # Never sell more than the position is worth.
    if order_value < 0 and -order_value > position_value:
        order_value = -position_value
    return order_value


def partial_exit_stake(sell_fraction: float, trade_stake_amount: float) -> float:
    """Translate "sell this fraction of the position" into Freqtrade's partial-exit value.

    Freqtrade (2026.8, ``freqtradebot.check_and_call_adjust_trade_position`` and
    ``optimize.backtesting._check_adjust_trade_for_candle``) converts a negative
    ``adjust_trade_position`` result to a base amount of
    ``|stake| * trade.amount / trade.stake_amount``, i.e. relative to the invested
    cost basis, not to the current position value. Returning
    ``-sell_fraction * trade.stake_amount`` therefore sells exactly
    ``sell_fraction * trade.amount`` at any price. ``sell_fraction`` is clipped to
    ``[0, 1]``; 1.0 flattens the trade (remainder 0, Freqtrade closes it).
    """
    if trade_stake_amount <= 0:
        raise ValueError("trade_stake_amount must be > 0")
    return -_clip01(sell_fraction) * trade_stake_amount


def exit_min_stake(min_stake: float, stoploss: float) -> float:
    """Minimum remainder Freqtrade accepts after a partial exit.

    ``adjust_trade_position`` receives ``min_stake`` computed with stoploss 0
    (live) or -0.1 (backtest), while the remainder check in Freqtrade uses the
    strategy stoploss: ``exchange._get_stake_amount_limit`` multiplies the
    exchange minimum by ``(1 + reserve) / (1 - |stoploss|)``, capped at 1.5.
    Applying the same factor here (on top of the given value, so slightly
    conservative in backtesting) keeps a partial exit from being refused.
    """
    if min_stake < 0:
        raise ValueError("min_stake must be >= 0")
    sl = abs(stoploss)
    factor = 1.5 if sl >= 1 else min(1.5, 1.0 / (1.0 - sl))
    return min_stake * factor


def candles_held(open_date: datetime, now: datetime, timeframe_minutes: int) -> int:
    """Number of full candles elapsed since ``open_date``.

    Both datetimes must be timezone aware. Negative differences clamp to 0.
    """
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be > 0")
    seconds = (now - open_date).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // (timeframe_minutes * 60))


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def evaluate_decision(decision: Any, now: datetime) -> tuple[bool, str]:
    """Interpret an advisor ``decision.json`` document as an entry gate.

    Returns ``(block_entries, reason)``. Entries are blocked only when the
    document is structurally valid (``schema_version`` matches, ``valid_until``
    parses and is in the future, ``regime`` is known), ``mode == "gate"`` and
    ``regime == "risk_off"``. Any other case, including a missing or malformed
    document, fails open: the strategy behaves like plain ``BtcTrend`` and the
    reason string tells why.
    """
    if not isinstance(decision, dict):
        return False, "missing"
    if decision.get("schema_version") != DECISION_SCHEMA_VERSION:
        return False, "schema_version"
    mode = decision.get("mode")
    if mode != "gate":
        return False, f"mode={mode}"
    try:
        valid_until = parse_iso_utc(str(decision.get("valid_until", "")))
    except ValueError:
        return False, "valid_until unparsable"
    if valid_until < now:
        return False, "expired"
    regime = decision.get("regime")
    if regime not in VALID_REGIMES:
        return False, f"regime={regime}"
    if regime == "risk_off":
        return True, "risk_off"
    return False, str(regime)


def last_finite(series: pd.Series) -> float | None:
    """Last value of a series as float, or None when it is missing or not finite."""
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value_f):
        return None
    return value_f


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))
