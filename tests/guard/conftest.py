"""Shared fixtures for the guard tests: fake Freqtrade client, settings, helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from btctrader.common.alerts import Alerter
from btctrader.common.config import Settings, load_settings
from btctrader.common.ftapi import FreqtradeError
from btctrader.guard.guard import GuardConfig, GuardState, Observations

T0 = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


class FakeFT:
    """Duck-typed stand-in for ``FreqtradeClient``; records control calls."""

    def __init__(
        self,
        equity: str = "1000",
        *,
        dry_run: bool = True,
        eur: str | None = None,
        btc: str = "0",
        price: str = "50000",
        unreachable: bool = False,
        last_process_age_s: float = 5.0,
        bot_state: str | None = "running",
        est_stake_missing: bool = False,
        open_trades: int | None = None,
    ) -> None:
        self.equity = Decimal(equity)
        self.dry_run = dry_run
        self.btc = Decimal(btc)
        self.price = Decimal(price)
        self.eur = Decimal(eur) if eur is not None else self.equity - self.btc * self.price
        self.unreachable = unreachable
        self.last_process_age_s = last_process_age_s
        self.bot_state = bot_state
        self.est_stake_missing = est_stake_missing
        """Simulate Freqtrade valuing the BTC wallet at 0 (ticker unavailable): est_stake 0, total = EUR."""
        self.open_trades = open_trades
        """Number of open trades reported by /status; None = 1 when BTC is held, else 0."""
        self.fail: set[str] = set()
        """Control calls (``stopentry``, ``forceexit``) that raise ``FreqtradeError``."""
        self.calls: list[str] = []
        self.now = T0

    def _guard(self) -> None:
        if self.unreachable:
            raise FreqtradeError("connection refused")

    def health(self) -> dict[str, Any]:
        self._guard()
        ts = int(self.now.timestamp() - self.last_process_age_s)
        return {"last_process_ts": ts, "last_process": None, "bot_start_ts": ts - 3600}

    def balance(self) -> dict[str, Any]:
        self._guard()
        currencies = [
            {"currency": "EUR", "free": float(self.eur), "balance": float(self.eur), "used": 0.0,
             "est_stake": float(self.eur), "stake": "EUR", "side": "long", "is_position": False},
        ]
        est_btc = 0.0 if self.est_stake_missing else float(self.btc * self.price)
        if self.btc:
            currencies.append(
                {"currency": "BTC", "free": float(self.btc), "balance": float(self.btc), "used": 0.0,
                 "est_stake": est_btc, "stake": "EUR", "side": "long", "is_position": False}
            )
        total = float(self.eur) + est_btc if self.est_stake_missing else float(self.equity)
        return {"currencies": currencies, "total": total, "stake": "EUR", "value": 0.0}

    def status(self) -> list[dict[str, Any]]:
        self._guard()
        n = self.open_trades if self.open_trades is not None else (1 if self.btc else 0)
        return [{"trade_id": i + 1} for i in range(n)]

    def show_config(self) -> dict[str, Any]:
        self._guard()
        config: dict[str, Any] = {"dry_run": self.dry_run, "version": "2026.8"}
        if self.bot_state is not None:
            config["state"] = self.bot_state
        return config

    def stopentry(self) -> dict[str, Any]:
        self._guard()
        if "stopentry" in self.fail:
            raise FreqtradeError("POST /stopentry: 500 Internal Server Error", status_code=500)
        self.calls.append("stopentry")
        return {"status": "No more entries will occur from now. Run /start to enable entries."}

    def forceexit(self, tradeid: str = "all", ordertype: str | None = None) -> dict[str, Any]:
        self._guard()
        if "forceexit" in self.fail:
            raise FreqtradeError("POST /forceexit: 502 Bad Gateway", status_code=502)
        self.calls.append(f"forceexit:{tradeid}:{ordertype}")
        return {"result": "Created exit orders for all open trades."}


class RecordingAlerter(Alerter):
    """Alerter that only records what would have been sent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, priority: str = "default") -> list[str]:
        self.sent.append((title, message, priority))
        return ["fake"]


class FakeExchange:
    """ccxt stand-in for ``fetch_balance`` and ``cancel_all_orders_after``."""

    def __init__(self, eur: str = "0", btc: str = "0") -> None:
        self.eur = Decimal(eur)
        self.btc = Decimal(btc)
        self.cod_calls: list[tuple[int, Any]] = []

    def fetch_balance(self, params: Any = None) -> dict[str, Any]:
        return {"total": {"EUR": float(self.eur), "BTC": float(self.btc)}, "free": {}, "used": {}}

    def cancel_all_orders_after(self, timeout: int, params: Any = None) -> dict[str, Any]:
        self.cod_calls.append((timeout, params))
        return {"codGroupId": 1, "timeOfExpirySeconds": 1}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def make_settings(tmp_path: Path, **extra: str) -> Settings:
    env = {
        "FT_API_USER": "bot",
        "FT_API_PASS": "pw",
        "GUARD_DIR": str(tmp_path / "guard"),
        "ADVISOR_DIR": str(tmp_path / "advisor"),
    }
    env.update(extra)
    return load_settings(env)


@pytest.fixture
def cfg() -> GuardConfig:
    return GuardConfig()


@pytest.fixture
def alerter() -> RecordingAlerter:
    return RecordingAlerter()


def obs(
    equity: str | None = "1000",
    *,
    now: datetime = T0,
    ft_ok: bool = True,
    dry_run: bool | None = True,
    lock_exists: bool = False,
    **kwargs: Any,
) -> Observations:
    """Observation with sensible defaults; Decimal strings for money."""
    for key in ("ft_eur", "ft_btc", "exchange_eur", "exchange_btc", "btc_price"):
        if key in kwargs and kwargs[key] is not None:
            kwargs[key] = Decimal(kwargs[key])
    return Observations(
        now=now,
        ft_ok=ft_ok,
        equity=Decimal(equity) if equity is not None else None,
        dry_run=dry_run,
        lock_exists=lock_exists,
        **kwargs,
    )


def kinds(actions: list[Any]) -> list[str]:
    return [a.kind for a in actions]


def events(actions: list[Any]) -> list[str]:
    return [a.details["event"] for a in actions if a.kind == "event"]


def fresh_state(**kwargs: Any) -> GuardState:
    return GuardState(**kwargs)
