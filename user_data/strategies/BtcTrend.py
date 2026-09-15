"""BtcTrend: slow BTC/EUR trend following on daily candles for Freqtrade (INTERFACE_VERSION 3).

Rules (see docs/KOMPONENTEN.md section 7):

* Long only. Enter when the daily close is above SMA200 * (1 + band_pct), leave
  when it is below SMA200 * (1 - band_pct). The hysteresis band avoids whipsaw
  around the average; each round trip costs 0.30 % (maker) to 0.50 % (taker)
  at Bitvavo, so the band is several times the round-trip cost.
* Minimum holding period: exit signals are vetoed in ``confirm_trade_exit``
  for the first ``min_hold_candles`` candles. Stoploss, force exits and
  emergency exits are never vetoed. (Freqtrade only calls ``custom_exit`` when
  no exit signal is set, so ``custom_exit`` cannot block a signal; the veto
  hook Freqtrade provides for that is ``confirm_trade_exit``.)
* Volatility targeting: the stake is capital * min(1, target_vol / realized_vol)
  with realized_vol = std of daily returns over ``vol_lookback`` days * sqrt(365).
* Rebalancing: ``adjust_trade_position`` buys or sells part of the position
  when the exposure drifts more than ``rebalance_band`` from the target and
  the order is at least max(min_order_eur, min_order_pct * capital).
* Catastrophe stop at -10 % (bot side; Bitvavo has no exchange-side stop in
  Freqtrade). ROI is effectively disabled.
* ``confirm_trade_entry`` refuses new entries while ``$GUARD_DIR/killswitch.lock``
  exists (written by btctrader-guard). No network calls anywhere in this file.

Order settings for maker execution at Bitvavo (why they are what they are):

* Freqtrade 2026.8 lists Bitvavo with ``_ft_has = {"order_time_in_force": ["GTC"]}``
  (inherited default, ``freqtrade/exchange/bitvavo.py`` adds nothing). A config
  with ``order_time_in_force: {"entry": "PO"}`` is therefore rejected by
  ``Exchange.validate_order_time_in_force`` even though ccxt's ``bitvavo.createOrder``
  would translate ``timeInForce="PO"`` into ``postOnly: true``. Post-only can
  only be enabled through the undocumented ``exchange._ft_has_params`` override,
  which we do not rely on for the first dry-run phase.
* Consequence: ``order_time_in_force`` is GTC for entry and exit, order types are
  limit for entry and exit (market only for the stoploss). With
  ``entry_pricing.price_side = "same"`` and ``use_order_book = true`` an entry is
  priced at the best bid and an exit at the best ask, so the order rests in the
  book and is normally filled as maker (0.15 %). There is no guarantee: if the
  book moves through the price before the order arrives, Bitvavo fills it as
  taker (0.25 %). The ledger records the real maker/taker flag per fill; the
  backtest is therefore run with both fee levels.
* ``unfilledtimeout`` is 120 minutes for both sides; on daily candles a resting
  order may wait. After the timeout Freqtrade cancels and re-places at the new
  best price.
* Protections (CooldownPeriod, StoplossGuard, MaxDrawdown) live in this class
  because Freqtrade 2026.8 refuses ``protections`` in the config file
  (``ConfigurationError: Setting 'protections' in the configuration is deprecated``).
  Backtests need ``--enable-protections`` to apply them.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import btctrend_lib as lib
from freqtrade.enums import RunMode
from freqtrade.exchange import timeframe_to_minutes
from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)

PAIR = "BTC/EUR"
DEFAULT_GUARD_DIR = "/srv/trading/guard"
KILLSWITCH_FILENAME = "killswitch.lock"
# Exit reasons that must never be vetoed by the minimum holding period.
NON_VETOABLE_EXITS = frozenset(
    {
        "stop_loss",
        "stoploss_on_exchange",
        "trailing_stop_loss",
        "force_exit",
        "emergency_exit",
        "liquidation",
        "partial_exit",
    }
)


class BtcTrend(IStrategy):
    """Daily SMA200 trend filter with hysteresis, vol targeting and banded rebalancing."""

    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 210
    stoploss = -0.10
    minimal_roi = {"0": 100}
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    position_adjustment_enable = True
    max_entry_position_adjustment = 10
    use_custom_stoploss = False
    trailing_stop = False

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    # Strategy parameters. optimize=False keeps them fixed unless a hyperopt run
    # explicitly enables the space; the defaults are the contract values.
    sma_period = IntParameter(150, 250, default=200, space="buy", optimize=False)
    band_pct = DecimalParameter(0.0, 0.06, default=0.03, decimals=3, space="buy", optimize=False)
    min_hold_candles = IntParameter(0, 15, default=5, space="sell", optimize=False)
    target_vol = DecimalParameter(0.20, 0.60, default=0.35, decimals=2, space="buy", optimize=False)
    vol_lookback = IntParameter(10, 60, default=30, space="buy", optimize=False)
    rebalance_band = DecimalParameter(0.05, 0.30, default=0.15, decimals=2, space="buy", optimize=False)
    min_order_pct = DecimalParameter(0.0, 0.05, default=0.02, decimals=3, space="buy", optimize=False)
    # Exchange minimum order value in EUR. Read from market limits at runtime, fallback 5.0.
    min_order_eur = 5.0
    # Do not rebalance in the candle of the last fill (avoids order churn on one candle).
    rebalance_min_candles_since_fill = 1

    plot_config = {
        "main_plot": {
            "sma": {"color": "orange"},
            "band_upper": {"color": "green"},
            "band_lower": {"color": "red"},
        },
        "subplots": {"rvol": {"rvol": {"color": "blue"}}},
    }

    @property
    def protections(self) -> list[dict[str, Any]]:
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 30,
                "trade_limit": 2,
                "stop_duration_candles": 10,
                "only_per_pair": False,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 30,
                "trade_limit": 1,
                "stop_duration_candles": 10,
                "max_allowed_drawdown": 0.10,
            },
        ]

    # ------------------------------------------------------------------ lifecycle

    def bot_start(self, **kwargs: Any) -> None:
        self._exchange_min_order_eur: float | None = None
        self._tf_minutes = timeframe_to_minutes(self.timeframe)

    # ------------------------------------------------------------------ indicators

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Compute one column per candidate value so hyperopt on the buy space works;
        # with optimize=False the range contains only the default value.
        for period in self.sma_period.range:
            dataframe[f"sma_{period}"] = dataframe["close"].rolling(window=period, min_periods=period).mean()
        for lookback in self.vol_lookback.range:
            dataframe[f"rvol_{lookback}"] = lib.realized_volatility(dataframe["close"], lookback)
        dataframe["sma"] = dataframe[self._sma_col()]
        dataframe["rvol"] = dataframe[self._rvol_col()]
        dataframe["band_upper"] = dataframe["sma"] * (1.0 + float(self.band_pct.value))
        dataframe["band_lower"] = dataframe["sma"] * (1.0 - float(self.band_pct.value))
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        enter, _ = lib.hysteresis_signals(
            dataframe["close"], dataframe[self._sma_col()], float(self.band_pct.value)
        )
        dataframe.loc[enter, ["enter_long", "enter_tag"]] = (1, "trend_up")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        _, exit_ = lib.hysteresis_signals(
            dataframe["close"], dataframe[self._sma_col()], float(self.band_pct.value)
        )
        dataframe.loc[exit_, "exit_long"] = 1
        return dataframe

    # ------------------------------------------------------------------ sizing

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        # With stake_amount "unlimited" and max_open_trades 1 the proposed stake is the
        # tradable capital (free EUR * tradable_balance_ratio); no position is open here.
        capital = proposed_stake
        exposure = self.target_exposure(pair)
        stake = capital * exposure
        if stake < lib.min_order_value(
            capital, self.exchange_min_order_eur(), float(self.min_order_pct.value)
        ):
            logger.log(
                self._log_level(), "%s: stake %.2f EUR below minimum order value, skipping entry", pair, stake
            )
            return 0.0
        stake = min(stake, max_stake)
        if min_stake is not None and stake < min_stake:
            return 0.0
        logger.log(
            self._log_level(),
            "%s: exposure %.2f -> stake %.2f EUR of %.2f EUR capital",
            pair,
            exposure,
            stake,
            capital,
        )
        return stake

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs: Any,
    ) -> float | tuple[float | None, str | None] | None:
        if trade.has_open_orders:
            return None
        last_fill = trade.date_last_filled_utc or trade.open_date_utc
        if (
            lib.candles_held(last_fill, current_time, self._timeframe_minutes())
            < self.rebalance_min_candles_since_fill
        ):
            return None
        position_value = trade.amount * current_rate
        capital = self.free_stake() + position_value
        target = self.target_exposure(trade.pair)
        min_order = lib.min_order_value(
            capital, self.exchange_min_order_eur(), float(self.min_order_pct.value)
        )
        if min_stake is not None:
            min_order = max(min_order, min_stake)
        order_value = lib.rebalance_amount(
            capital, position_value, target, float(self.rebalance_band.value), min_order
        )
        if order_value is None:
            return None
        if order_value > 0:
            order_value = min(order_value, max_stake)
            if order_value < min_order:
                return None
            logger.log(
                self._log_level(),
                "%s: rebalance buy %.2f EUR (target exposure %.2f)",
                trade.pair,
                order_value,
                target,
            )
            return order_value, "rebalance_up"
        # Partial exit: never leave a dust position behind that is below the minimum.
        remaining = position_value + order_value
        if min_stake is not None and 0 < remaining < min_stake:
            order_value = -position_value
        logger.log(
            self._log_level(),
            "%s: rebalance sell %.2f EUR (target exposure %.2f)",
            trade.pair,
            -order_value,
            target,
        )
        return order_value, "rebalance_down"

    # ------------------------------------------------------------------ exits

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | bool | None:
        # Signal exits are produced by populate_exit_trend and gated by the minimum
        # holding period in confirm_trade_exit. No additional exit rule here.
        return None

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs: Any,
    ) -> bool:
        if exit_reason in NON_VETOABLE_EXITS:
            return True
        held = lib.candles_held(trade.open_date_utc, current_time, self._timeframe_minutes())
        if held < int(self.min_hold_candles.value):
            logger.info(
                "%s: exit '%s' vetoed, held %d < %d candles",
                pair,
                exit_reason,
                held,
                int(self.min_hold_candles.value),
            )
            return False
        return True

    # ------------------------------------------------------------------ entries

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        order_value = amount * rate
        if order_value < self.exchange_min_order_eur():
            logger.warning("%s: entry of %.2f EUR below exchange minimum, refused", pair, order_value)
            return False
        if self.killswitch_active():
            logger.warning("%s: entry refused, %s exists", pair, self.killswitch_path())
            return False
        return True

    # ------------------------------------------------------------------ helpers

    def target_exposure(self, pair: str) -> float:
        """Exposure from the last analysed candle's realized volatility (fallback 1.0)."""
        rvol: float | None = None
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and len(dataframe) > 0 and "rvol" in dataframe:
                rvol = lib.last_finite(dataframe["rvol"])
        except Exception as exc:  # noqa: BLE001 - never break order flow on a data hiccup
            logger.warning("%s: could not read analysed dataframe: %s", pair, exc)
        return lib.exposure_from_vol(float(self.target_vol.value), rvol)

    def free_stake(self) -> float:
        wallets = getattr(self, "wallets", None)
        if wallets is None:
            return 0.0
        return float(wallets.get_free(self.config.get("stake_currency", "EUR")))

    def exchange_min_order_eur(self) -> float:
        """Minimum order value from market limits (``limits.cost.min``), cached; fallback 5.0."""
        cached = getattr(self, "_exchange_min_order_eur", None)
        if cached is not None:
            return cached
        value = float(self.min_order_eur)
        try:
            market = self.dp.market(PAIR)
            cost_min = ((market or {}).get("limits") or {}).get("cost", {}).get("min")
            if cost_min:
                value = max(value, float(cost_min))
        except Exception as exc:  # noqa: BLE001 - market info is optional
            logger.debug("market limits unavailable, using fallback %.2f EUR: %s", value, exc)
        self._exchange_min_order_eur = value
        return value

    def killswitch_path(self) -> Path:
        return Path(os.environ.get("GUARD_DIR", DEFAULT_GUARD_DIR)) / KILLSWITCH_FILENAME

    def killswitch_active(self) -> bool:
        """True when the guard's lock file exists. Only consulted in live and dry-run mode."""
        if not self._is_live_or_dry():
            return False
        try:
            return self.killswitch_path().exists()
        except OSError:
            return False

    def _log_level(self) -> int:
        """INFO in live/dry-run (one line per decision), DEBUG in backtesting to keep output readable."""
        return logging.INFO if self._is_live_or_dry() else logging.DEBUG

    def _is_live_or_dry(self) -> bool:
        runmode = self.config.get("runmode")
        return runmode in (RunMode.LIVE, RunMode.DRY_RUN)

    def _timeframe_minutes(self) -> int:
        minutes = getattr(self, "_tf_minutes", None)
        if minutes is None:
            minutes = timeframe_to_minutes(self.timeframe)
            self._tf_minutes = minutes
        return minutes

    def _sma_col(self) -> str:
        return f"sma_{int(self.sma_period.value)}"

    def _rvol_col(self) -> str:
        return f"rvol_{int(self.vol_lookback.value)}"
