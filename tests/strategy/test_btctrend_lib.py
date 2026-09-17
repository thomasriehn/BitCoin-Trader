"""Unit tests for the pure strategy helpers (no Freqtrade needed)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

# ------------------------------------------------------------------ hysteresis


def test_hysteresis_enter_only_above_upper_band(lib) -> None:
    sma = pd.Series([100.0] * 5)
    close = pd.Series([102.0, 103.0, 103.5, 100.0, 96.0])
    enter, exit_ = lib.hysteresis_signals(close, sma, 0.03)
    assert enter.tolist() == [False, False, True, False, False]
    assert exit_.tolist() == [False, False, False, False, True]


def test_hysteresis_inside_band_is_neutral(lib) -> None:
    sma = pd.Series([100.0, 100.0, 100.0])
    close = pd.Series([97.5, 100.0, 102.5])
    enter, exit_ = lib.hysteresis_signals(close, sma, 0.03)
    assert not enter.any()
    assert not exit_.any()


def test_hysteresis_nan_sma_gives_no_signal(lib) -> None:
    sma = pd.Series([np.nan, np.nan, 100.0])
    close = pd.Series([200.0, 1.0, 110.0])
    enter, exit_ = lib.hysteresis_signals(close, sma, 0.03)
    assert enter.tolist() == [False, False, True]
    assert exit_.tolist() == [False, False, False]


def test_hysteresis_negative_band_rejected(lib) -> None:
    with pytest.raises(ValueError):
        lib.hysteresis_signals(pd.Series([1.0]), pd.Series([1.0]), -0.01)


# ------------------------------------------------------------------ volatility


def test_realized_volatility_matches_manual_computation(lib) -> None:
    rng = np.random.default_rng(42)
    close = pd.Series(100.0 * np.cumprod(1 + rng.normal(0, 0.02, 80)))
    rvol = lib.realized_volatility(close, 30)
    assert rvol.iloc[:30].isna().all()
    returns = close.pct_change()
    expected = returns.iloc[-30:].std(ddof=1) * math.sqrt(365)
    assert rvol.iloc[-1] == pytest.approx(expected, rel=1e-9)


def test_realized_volatility_uses_only_past_rows(lib) -> None:
    rng = np.random.default_rng(1)
    close = pd.Series(100.0 * np.cumprod(1 + rng.normal(0, 0.02, 120)))
    full = lib.realized_volatility(close, 30)
    truncated = lib.realized_volatility(close.iloc[:90].reset_index(drop=True), 30)
    # Values on shared rows must be identical regardless of what comes later (no lookahead)
    # and independent of the series start (no recursion).
    pd.testing.assert_series_equal(full.iloc[:90], truncated, check_names=False)
    shifted = lib.realized_volatility(close.iloc[40:].reset_index(drop=True), 30)
    assert full.iloc[-1] == pytest.approx(shifted.iloc[-1], rel=0, abs=0)


def test_realized_volatility_rejects_short_lookback(lib) -> None:
    with pytest.raises(ValueError):
        lib.realized_volatility(pd.Series([1.0, 2.0]), 1)


# ------------------------------------------------------------------ exposure


@pytest.mark.parametrize(
    ("target", "rvol", "expected"),
    [
        (0.35, 0.70, 0.5),
        (0.35, 0.35, 1.0),
        (0.35, 0.10, 1.0),  # capped at 100 %
        (0.35, 1.40, 0.25),
        (0.35, 0.0, 1.0),  # flat market
    ],
)
def test_exposure_from_vol(lib, target: float, rvol: float, expected: float) -> None:
    assert lib.exposure_from_vol(target, rvol) == pytest.approx(expected)


def test_exposure_from_vol_fallback_when_unknown(lib) -> None:
    # Unknown volatility never fails open to 100 %: the default fallback is no exposure.
    assert lib.exposure_from_vol(0.35, None) == 0.0
    assert lib.exposure_from_vol(0.35, float("nan")) == 0.0
    assert lib.exposure_from_vol(0.35, float("nan"), fallback=0.5) == 0.5
    assert lib.exposure_from_vol(0.35, None, fallback=7.0) == 1.0
    with pytest.raises(ValueError):
        lib.exposure_from_vol(0.0, 0.5)


# ------------------------------------------------------------------ minimum order value


def test_min_order_value_uses_larger_of_exchange_min_and_pct(lib) -> None:
    assert lib.min_order_value(100.0, 5.0, 0.02) == 5.0
    assert lib.min_order_value(500.0, 5.0, 0.02) == 10.0
    assert lib.min_order_value(5000.0, 5.0, 0.02) == 100.0
    with pytest.raises(ValueError):
        lib.min_order_value(-1.0, 5.0, 0.02)


# ------------------------------------------------------------------ rebalance


def test_rebalance_no_order_inside_band(lib) -> None:
    # 1000 EUR capital, 600 EUR position (60 %), target 70 %: drift 10 pp < 15 pp band
    assert lib.rebalance_amount(1000.0, 600.0, 0.70, 0.15, 20.0) is None


def test_rebalance_buy_when_under_exposed(lib) -> None:
    # 30 % actual vs 60 % target -> buy 30 % of capital
    assert lib.rebalance_amount(1000.0, 300.0, 0.60, 0.15, 20.0) == pytest.approx(300.0)


def test_rebalance_sell_when_over_exposed(lib) -> None:
    # 90 % actual vs 50 % target -> sell 40 % of capital
    assert lib.rebalance_amount(1000.0, 900.0, 0.50, 0.15, 20.0) == pytest.approx(-400.0)


def test_rebalance_respects_min_order_in_eur(lib) -> None:
    # Drift 20 pp on 50 EUR capital = 10 EUR order, below the 20 EUR floor
    assert lib.rebalance_amount(50.0, 10.0, 0.40, 0.15, 20.0) is None
    # Same drift on 500 EUR capital = 100 EUR order, above the floor
    assert lib.rebalance_amount(500.0, 100.0, 0.40, 0.15, 20.0) == pytest.approx(100.0)


def test_rebalance_never_sells_more_than_position(lib) -> None:
    assert lib.rebalance_amount(1000.0, 100.0, -0.5, 0.15, 5.0) == pytest.approx(-100.0)


def test_rebalance_zero_capital(lib) -> None:
    assert lib.rebalance_amount(0.0, 0.0, 0.5, 0.15, 5.0) is None


# ------------------------------------------------------------------ candles held


def test_partial_exit_stake_matches_freqtrade_conversion(lib) -> None:
    # Freqtrade sells |stake| * trade.amount / trade.stake_amount base units.
    trade_amount, open_rate = 0.02, 40_000.0
    stake_amount = trade_amount * open_rate
    for fraction in (0.25, 0.5, 1.0):
        stake = lib.partial_exit_stake(fraction, stake_amount)
        assert stake < 0
        assert abs(stake) * trade_amount / stake_amount == pytest.approx(fraction * trade_amount)
    assert lib.partial_exit_stake(1.7, stake_amount) == pytest.approx(-stake_amount)
    assert lib.partial_exit_stake(-0.2, stake_amount) == 0.0
    with pytest.raises(ValueError):
        lib.partial_exit_stake(0.5, 0.0)


def test_exit_min_stake_adds_stoploss_reserve(lib) -> None:
    # exchange._get_stake_amount_limit: minimum / (1 - |stoploss|), capped at factor 1.5
    assert lib.exit_min_stake(5.25, -0.10) == pytest.approx(5.25 / 0.9)
    assert lib.exit_min_stake(5.25, 0.0) == pytest.approx(5.25)
    assert lib.exit_min_stake(5.25, -0.9) == pytest.approx(5.25 * 1.5)
    assert lib.exit_min_stake(5.25, -1.0) == pytest.approx(5.25 * 1.5)
    with pytest.raises(ValueError):
        lib.exit_min_stake(-1.0, -0.1)


def test_candles_held(lib) -> None:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    assert lib.candles_held(opened, opened, 1440) == 0
    assert lib.candles_held(opened, opened + timedelta(hours=23), 1440) == 0
    assert lib.candles_held(opened, opened + timedelta(days=5), 1440) == 5
    assert lib.candles_held(opened, opened - timedelta(days=1), 1440) == 0
    with pytest.raises(ValueError):
        lib.candles_held(opened, opened, 0)


# ------------------------------------------------------------------ advisor gate


def _decision(**overrides):
    base = {
        "schema_version": 1,
        "decision_id": "2026-09-15T13:07:02Z-a1b2c3",
        "created_at": "2026-09-15T13:07:02Z",
        "valid_until": "2026-09-15T15:07:02Z",
        "mode": "gate",
        "regime": "risk_off",
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


NOW = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def test_gate_blocks_on_valid_risk_off(lib) -> None:
    assert lib.evaluate_decision(_decision(), NOW) == (True, "risk_off")


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (None, "missing"),
        ("not a dict", "missing"),
        (_decision(schema_version=2), "schema_version"),
        (_decision(mode="shadow"), "mode=shadow"),
        (_decision(valid_until="2026-09-15T13:59:00Z"), "expired"),
        (_decision(valid_until="garbage"), "valid_until unparsable"),
        (_decision(regime="risk_on"), "risk_on"),
        (_decision(regime="neutral"), "neutral"),
        (_decision(regime="panic"), "regime=panic"),
    ],
)
def test_gate_fails_open(lib, decision, reason: str) -> None:
    assert lib.evaluate_decision(decision, NOW) == (False, reason)


def test_parse_iso_utc(lib) -> None:
    assert lib.parse_iso_utc("2026-09-15T13:07:02Z") == datetime(2026, 9, 15, 13, 7, 2, tzinfo=UTC)
    assert lib.parse_iso_utc("2026-09-15T15:07:02+02:00") == datetime(2026, 9, 15, 13, 7, 2, tzinfo=UTC)
    assert lib.parse_iso_utc("2026-09-15T13:07:02") == datetime(2026, 9, 15, 13, 7, 2, tzinfo=UTC)


def test_last_finite(lib) -> None:
    assert lib.last_finite(pd.Series([1.0, 2.0, float("nan")])) is None
    assert lib.last_finite(pd.Series([1.0, 2.5])) == 2.5
    assert lib.last_finite(pd.Series([], dtype=float)) is None
