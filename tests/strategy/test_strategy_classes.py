"""Freqtrade-level tests for BtcTrend and BtcAdvisorGated.

These tests need the Freqtrade venv (/home/user/ft-venv) and skip cleanly elsewhere.
The strategies are loaded through Freqtrade's StrategyResolver, which also verifies
that ``import btctrend_lib`` works via the strategy-path mechanism.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("freqtrade")

from freqtrade.data.dataprovider import DataProvider  # noqa: E402
from freqtrade.enums import CandleType, RunMode  # noqa: E402
from freqtrade.resolvers import StrategyResolver  # noqa: E402

PAIR = "BTC/EUR"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def base_config(strategy_dir: Path, name: str, runmode: RunMode) -> dict[str, Any]:
    return {
        "strategy": name,
        "strategy_path": str(strategy_dir),
        "user_data_dir": strategy_dir.parent,
        "runmode": runmode,
        "stake_currency": "EUR",
        "stake_amount": "unlimited",
        "max_open_trades": 1,
        "dry_run": True,
        "exchange": {"name": "bitvavo", "pair_whitelist": [PAIR]},
        "trading_mode": "spot",
        "candle_type_def": CandleType.SPOT,
    }


def make_dataframe(n: int = 260, trend: float = 0.004, seed: int = 7, noise: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10_000.0 * np.cumprod(1 + trend + rng.normal(0, noise, n))
    dates = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "date": dates,
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 100.0,
        }
    )


def load(strategy_dir: Path, name: str = "BtcTrend", runmode: RunMode = RunMode.BACKTEST):
    config = base_config(strategy_dir, name, runmode)
    strategy = StrategyResolver.load_strategy(config)
    strategy.dp = DataProvider(config, None)
    strategy.bot_start()
    return strategy


def analyse(strategy, df: pd.DataFrame) -> pd.DataFrame:
    out = strategy.populate_indicators(df.copy(), {"pair": PAIR})
    out = strategy.populate_entry_trend(out, {"pair": PAIR})
    out = strategy.populate_exit_trend(out, {"pair": PAIR})
    strategy.dp._set_cached_df(PAIR, strategy.timeframe, out, CandleType.SPOT)
    # Backtesting exposes the dataframe only up to the candle being evaluated.
    strategy.dp._set_dataframe_max_index(PAIR, len(out))
    return out


def fake_trade(amount: float, opened: datetime, last_fill: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        pair=PAIR,
        amount=amount,
        open_date_utc=opened,
        date_last_filled_utc=last_fill or opened,
        has_open_orders=False,
    )


class FakeWallets:
    def __init__(self, free_eur: float) -> None:
        self.free_eur = free_eur

    def get_free(self, currency: str) -> float:
        assert currency == "EUR"
        return self.free_eur


# ------------------------------------------------------------------ class contract


def test_btctrend_class_attributes(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    assert s.INTERFACE_VERSION == 3
    assert s.timeframe == "1d"
    assert s.can_short is False
    assert s.process_only_new_candles is True
    assert s.startup_candle_count == 210
    assert s.stoploss == -0.10
    # Freqtrade normalises the ROI keys to int; "practically off" means 10000 % target.
    assert s.minimal_roi == {0: 100}
    assert s.use_exit_signal is True
    assert s.exit_profit_only is False
    assert s.position_adjustment_enable is True
    assert s.max_entry_position_adjustment == 10
    assert s.order_types["entry"] == "limit"
    assert s.order_types["exit"] == "limit"
    assert s.order_types["stoploss"] == "market"
    assert s.order_types["stoploss_on_exchange"] is False
    assert s.order_time_in_force == {"entry": "GTC", "exit": "GTC"}
    assert s.sma_period.value == 200
    assert s.band_pct.value == pytest.approx(0.03)
    assert s.min_hold_candles.value == 5
    assert s.target_vol.value == pytest.approx(0.35)
    assert s.vol_lookback.value == 30
    assert s.rebalance_band.value == pytest.approx(0.15)
    assert s.min_order_pct.value == pytest.approx(0.02)
    assert s.min_order_eur == 5.0
    methods = {p["method"] for p in s.protections}
    assert methods == {"CooldownPeriod", "StoplossGuard", "MaxDrawdown"}


def test_gated_class_inherits(strategy_dir: Path) -> None:
    s = load(strategy_dir, "BtcAdvisorGated")
    assert s.INTERFACE_VERSION == 3
    assert s.timeframe == "1d"
    assert type(s).__mro__[1].__name__ == "BtcTrend"


# ------------------------------------------------------------------ signals


def test_signals_in_uptrend_and_downtrend(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    up = analyse(s, make_dataframe(trend=0.004))
    assert "sma" in up and "rvol" in up and "sma_200" in up and "rvol_30" in up
    assert up["sma"].iloc[:199].isna().all() and up["sma"].iloc[199:].notna().all()
    last = up.iloc[-1]
    assert last["close"] > last["sma"] * 1.03
    assert last["enter_long"] == 1 and last["enter_tag"] == "trend_up"
    assert pd.isna(last.get("exit_long", np.nan)) or last["exit_long"] != 1

    down = analyse(s, make_dataframe(trend=-0.004))
    last = down.iloc[-1]
    assert last["close"] < last["sma"] * 0.97
    assert last["exit_long"] == 1
    assert pd.isna(last.get("enter_long", np.nan)) or last["enter_long"] != 1


def test_indicators_do_not_depend_on_future_rows(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    df = make_dataframe(n=300)
    full = s.populate_indicators(df.copy(), {"pair": PAIR})
    part = s.populate_indicators(df.iloc[:250].copy(), {"pair": PAIR})
    for col in ("sma", "rvol", "band_upper", "band_lower"):
        pd.testing.assert_series_equal(full[col].iloc[:250], part[col], check_names=False)


# ------------------------------------------------------------------ sizing


def test_custom_stake_amount_scales_with_vol(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    df = analyse(s, make_dataframe(noise=0.04))
    rvol = float(df["rvol"].iloc[-1])
    expected_exposure = min(1.0, 0.35 / rvol)
    assert expected_exposure < 1.0
    stake = s.custom_stake_amount(PAIR, NOW, 50_000.0, 990.0, 5.8, 990.0, 1.0, "trend_up", "long")
    assert stake == pytest.approx(990.0 * expected_exposure)
    assert 0 < stake <= 990.0


def test_custom_stake_amount_refuses_dust(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    analyse(s, make_dataframe())
    # 4 EUR capital: the stake is below the 5 EUR exchange minimum whatever the exposure
    assert s.custom_stake_amount(PAIR, NOW, 50_000.0, 4.0, 5.8, 4.0, 1.0, "trend_up", "long") == 0.0


def test_exchange_min_order_falls_back_without_exchange(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    assert s.exchange_min_order_eur() == 5.0
    s._exchange_min_order_eur = None
    s.dp.market = lambda pair: {"limits": {"cost": {"min": 7.5}}}  # type: ignore[method-assign]
    assert s.exchange_min_order_eur() == 7.5


def test_adjust_trade_position_rebalances(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    # 4 % daily noise -> realized vol around 75 % -> target exposure well below 1
    df = analyse(s, make_dataframe(noise=0.04))
    rate = float(df["close"].iloc[-1])
    target = s.target_exposure(PAIR)
    assert 0.3 < target < 0.7
    opened = NOW - timedelta(days=10)
    # Position of 200 EUR plus 800 EUR free: 20 % actual exposure.
    s.wallets = FakeWallets(800.0)
    trade = fake_trade(amount=200.0 / rate, opened=opened)
    result = s.adjust_trade_position(trade, NOW, rate, 0.0, 5.8, 800.0, rate, rate, 0.0, 0.0)
    drift = target - 0.2
    assert drift > 0.15
    assert result is not None
    amount, tag = result
    assert amount == pytest.approx(drift * 1000.0)
    assert tag == "rebalance_up"
    # Heavily over-exposed: 950 EUR position, 50 EUR free -> partial exit.
    s.wallets = FakeWallets(50.0)
    trade = fake_trade(amount=950.0 / rate, opened=opened)
    result = s.adjust_trade_position(trade, NOW, rate, 0.0, 5.8, 50.0, rate, rate, 0.0, 0.0)
    assert result is not None
    amount, tag = result
    assert amount < 0 and tag == "rebalance_down"
    assert amount == pytest.approx((target - 0.95) * 1000.0)


def test_adjust_trade_position_waits_one_candle_after_fill(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    df = analyse(s, make_dataframe(noise=0.04))
    rate = float(df["close"].iloc[-1])
    s.wallets = FakeWallets(50.0)
    trade = fake_trade(
        amount=950.0 / rate, opened=NOW - timedelta(days=10), last_fill=NOW - timedelta(hours=3)
    )
    assert s.adjust_trade_position(trade, NOW, rate, 0.0, 5.8, 50.0, rate, rate, 0.0, 0.0) is None
    trade.has_open_orders = True
    trade.date_last_filled_utc = NOW - timedelta(days=2)
    assert s.adjust_trade_position(trade, NOW, rate, 0.0, 5.8, 50.0, rate, rate, 0.0, 0.0) is None


# ------------------------------------------------------------------ exits


def test_confirm_trade_exit_min_hold(strategy_dir: Path) -> None:
    s = load(strategy_dir)
    young = fake_trade(0.01, NOW - timedelta(days=2))
    old = fake_trade(0.01, NOW - timedelta(days=5))
    assert s.confirm_trade_exit(PAIR, young, "limit", 0.01, 50_000.0, "GTC", "exit_signal", NOW) is False
    assert s.confirm_trade_exit(PAIR, old, "limit", 0.01, 50_000.0, "GTC", "exit_signal", NOW) is True
    for reason in ("stop_loss", "trailing_stop_loss", "force_exit", "emergency_exit"):
        assert s.confirm_trade_exit(PAIR, young, "market", 0.01, 50_000.0, "GTC", reason, NOW) is True
    assert s.custom_exit(PAIR, young, NOW, 50_000.0, 0.0) is None


# ------------------------------------------------------------------ entries and killswitch


def test_confirm_trade_entry_killswitch(strategy_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GUARD_DIR", str(tmp_path))
    s = load(strategy_dir, runmode=RunMode.DRY_RUN)
    args = (PAIR, "limit", 0.01, 50_000.0, "GTC", NOW, "trend_up", "long")
    assert s.confirm_trade_entry(*args) is True
    (tmp_path / "killswitch.lock").write_text("2026-09-15T10:00:00Z equity=800 peak=1000\n")
    assert s.confirm_trade_entry(*args) is False
    # Backtests ignore a lock file on the machine that runs them.
    bt = load(strategy_dir, runmode=RunMode.BACKTEST)
    assert bt.confirm_trade_entry(*args) is True


def test_confirm_trade_entry_refuses_below_exchange_minimum(strategy_dir: Path) -> None:
    s = load(strategy_dir, runmode=RunMode.DRY_RUN)
    assert s.confirm_trade_entry(PAIR, "limit", 0.00001, 50_000.0, "GTC", NOW, "trend_up", "long") is False


# ------------------------------------------------------------------ advisor gate


def write_decision(path: Path, **overrides: Any) -> None:
    doc = {
        "schema_version": 1,
        "decision_id": "2026-09-15T11:07:02Z-a1b2c3",
        "created_at": "2026-09-15T11:07:02Z",
        "valid_until": (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "gate",
        "model": "test",
        "regime": "risk_off",
        "confidence": 0.7,
        "horizon_days": 7,
        "rationale": "test",
        "key_factors": [],
    }
    doc.update(overrides)
    (path / "decision.json").write_text(json.dumps(doc))


def test_gate_blocks_entries_only_when_valid_gate_risk_off(
    strategy_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADVISOR_DIR", str(tmp_path))
    monkeypatch.setenv("GUARD_DIR", str(tmp_path))
    s = load(strategy_dir, "BtcAdvisorGated", runmode=RunMode.DRY_RUN)
    args = (PAIR, "limit", 0.01, 50_000.0, "GTC", NOW, "trend_up", "long")

    write_decision(tmp_path)
    s.bot_loop_start(NOW)
    assert s._gate_block is True
    assert s.confirm_trade_entry(*args) is False

    write_decision(tmp_path, regime="risk_on")
    s.bot_loop_start(NOW)
    assert s.confirm_trade_entry(*args) is True

    write_decision(tmp_path, mode="shadow")
    s.bot_loop_start(NOW)
    assert s.confirm_trade_entry(*args) is True

    write_decision(tmp_path, valid_until="2026-09-15T11:00:00Z")
    s.bot_loop_start(NOW)
    assert s._gate_reason == "expired"
    assert s.confirm_trade_entry(*args) is True

    # Exits are never touched by the gate.
    write_decision(tmp_path)
    s.bot_loop_start(NOW)
    old = fake_trade(0.01, NOW - timedelta(days=9))
    assert s.confirm_trade_exit(PAIR, old, "limit", 0.01, 50_000.0, "GTC", "exit_signal", NOW) is True


def test_gate_fails_open_and_warns_once_per_hour(
    strategy_dir: Path, tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("ADVISOR_DIR", str(tmp_path))
    s = load(strategy_dir, "BtcAdvisorGated", runmode=RunMode.DRY_RUN)
    with caplog.at_level(logging.WARNING, logger="BtcAdvisorGated"):
        s.bot_loop_start(NOW)
        s.bot_loop_start(NOW + timedelta(minutes=10))
        s.bot_loop_start(NOW + timedelta(minutes=61))
    warnings = [r for r in caplog.records if "advisor gate inactive" in r.getMessage()]
    assert len(warnings) == 2
    assert s._gate_block is False
    (tmp_path / "decision.json").write_text("{not json")
    s.bot_loop_start(NOW + timedelta(hours=3))
    assert s._gate_block is False


def test_gate_inactive_in_backtest(strategy_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADVISOR_DIR", str(tmp_path))
    write_decision(tmp_path)
    s = load(strategy_dir, "BtcAdvisorGated", runmode=RunMode.BACKTEST)
    s.bot_loop_start(NOW)
    assert s._gate_block is False
    assert s.confirm_trade_entry(PAIR, "limit", 0.01, 50_000.0, "GTC", NOW, "trend_up", "long") is True
