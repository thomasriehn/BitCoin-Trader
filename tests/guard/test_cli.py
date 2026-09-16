"""CLI and I/O layer: check end to end (respx), reset --confirm, status, reconciliation, COD."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from btctrader.common.config import Settings
from btctrader.common.db import parse_iso
from btctrader.common.jsonl import read_json, read_jsonl_tail
from btctrader.guard import cli, guard
from btctrader.guard.cli import main, run_check
from tests.guard.conftest import T0, FakeExchange, FakeFT, RecordingAlerter, make_settings

FT_URL = "http://127.0.0.1:8080"
API = f"{FT_URL}/api/v1"


def _events(settings: Settings) -> list[str]:
    return [e["event"] for e in read_jsonl_tail(settings.guard_dir / "events.jsonl", 100)]


def _no_exchange(*_: Any) -> Any:
    raise AssertionError("exchange must not be created in this test")


# -- balances ------------------------------------------------------------------------


def test_balances_from_ft_uses_est_stake_for_price() -> None:
    eur, btc, price = cli.balances_from_ft(FakeFT("1000", btc="0.01", price="50000").balance())
    assert (eur, btc, price) == (Decimal("500"), Decimal("0.01"), Decimal("50000"))
    eur, btc, price = cli.balances_from_ft({"currencies": [], "total": 0})
    assert (eur, btc, price) == (Decimal(0), Decimal(0), None)


def test_balances_from_ft_never_derives_a_zero_price() -> None:
    """Freqtrade reports est_stake 0 when its ticker lookup fails; 0 is not a known price."""
    balance = FakeFT("1000", btc="0.01", price="50000", est_stake_missing=True).balance()
    assert balance["total"] == 500.0  # what Freqtrade would report: EUR only
    eur, btc, price = cli.balances_from_ft(balance)
    assert (eur, btc, price) == (Decimal("500"), Decimal("0.01"), None)


def test_bot_state_from_config() -> None:
    assert cli.bot_state_from_config({"state": "Running"}) == "running"
    assert cli.bot_state_from_config({"state": ""}) is None
    assert cli.bot_state_from_config({}) is None


# -- check end to end (respx-mocked Freqtrade API) --------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FT_API_USER", "bot")
    monkeypatch.setenv("FT_API_PASS", "pw")
    monkeypatch.setenv("FT_API_URL", FT_URL)
    monkeypatch.setenv("GUARD_DIR", str(tmp_path / "guard"))
    monkeypatch.setenv("ADVISOR_DIR", str(tmp_path / "advisor"))
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    monkeypatch.setenv("GUARD_TRADE_ENV_FILE", str(tmp_path / "missing-secrets-guard.env"))
    monkeypatch.delenv("GUARD_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("GUARD_TRADE_API_SECRET", raising=False)
    return tmp_path


class MockFreqtrade:
    """respx routes for the guard's Freqtrade calls with an adjustable equity and open-trade list."""

    def __init__(self, router: respx.MockRouter, equity: float = 1000.0) -> None:
        self.equity = equity
        self.open_trades: list[dict[str, Any]] = [{"trade_id": 1}]
        router.post(f"{API}/token/login").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "refresh_token": "r"})
        )
        router.get(f"{API}/health").mock(
            return_value=httpx.Response(200, json={"last_process_ts": 1, "last_process": None})
        )
        router.get(f"{API}/balance").mock(side_effect=self._balance)
        router.get(f"{API}/status").mock(side_effect=self._status)
        router.get(f"{API}/show_config").mock(
            return_value=httpx.Response(200, json={"dry_run": True, "version": "2026.8", "state": "running"})
        )
        self.stopentry = router.post(f"{API}/stopentry").mock(
            return_value=httpx.Response(200, json={"status": "No more entries will occur from now."})
        )
        self.forceexit = router.post(f"{API}/forceexit").mock(
            return_value=httpx.Response(200, json={"result": "Created exit order for trade all."})
        )

    def _status(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.open_trades)

    def _balance(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "currencies": [
                    {"currency": "EUR", "free": self.equity, "balance": self.equity, "used": 0.0,
                     "est_stake": self.equity, "stake": "EUR", "side": "long", "is_position": False}
                ],
                "total": self.equity,
                "stake": "EUR",
            },
        )


def test_check_end_to_end_writes_state_events_and_lock(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    guard_dir = env / "guard"
    with respx.mock(assert_all_called=False) as router:
        ft = MockFreqtrade(router, 1000.0)
        assert main(["check"]) == 0
        state = read_json(guard_dir / "state.json")
        assert state is not None
        assert Decimal(state["day_start_equity"]) == 1000 and Decimal(state["peak_equity"]) == 1000
        assert state["last_check"].endswith("Z")
        assert _events(make_settings(env)) == ["day_start"]
        assert ft.stopentry.call_count == 0

        # 25 % below the peak within the same day: daily loss and kill switch; stopentry is sent once
        ft.equity = 750.0
        assert main(["check"]) == 0
        assert ft.forceexit.call_count == 1
        assert json.loads(ft.forceexit.calls[0].request.content) == {"tradeid": "all", "ordertype": "market"}
        assert ft.stopentry.call_count == 1
        lock = read_json(guard_dir / "killswitch.lock")
        assert lock is not None
        assert lock["equity_eur"] == "750.00" and lock["peak_equity_eur"] == "1000.00"
        assert lock["drawdown_pct"] == "25.00" and lock["ts"].endswith("Z")
        events = read_jsonl_tail(guard_dir / "events.jsonl", 10)
        assert [e["event"] for e in events] == ["day_start", "daily_loss", "killswitch"]
        assert events[-1]["details"]["open_trades"] == 1
        assert all(set(e) == {"ts", "event", "details"} for e in events)

        # lock present, exit filled (no open trades): stopentry again, no second forceexit, no new event
        ft.open_trades = []
        assert main(["check"]) == 0
        assert ft.stopentry.call_count == 2 and ft.forceexit.call_count == 1
        events = read_jsonl_tail(guard_dir / "events.jsonl", 10)
        assert [e["event"] for e in events] == ["day_start", "daily_loss", "killswitch"]

        # reset --confirm removes the lock; the next run does not re-apply stopentry
        assert main(["reset"]) == 2
        assert (guard_dir / "killswitch.lock").exists()
        assert main(["reset", "--confirm"]) == 0
        assert not (guard_dir / "killswitch.lock").exists()
        assert "killswitch.lock entfernt" in capsys.readouterr().out
        assert main(["check"]) == 0
        assert ft.stopentry.call_count == 2
        events = read_jsonl_tail(guard_dir / "events.jsonl", 10)
        assert [e["event"] for e in events] == ["day_start", "daily_loss", "killswitch", "killswitch_reset"]
        assert events[-1]["details"]["lock_existed"] is True
        state = read_json(guard_dir / "state.json")
        assert state is not None and Decimal(state["peak_equity"]) == 750

        assert main(["status", "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["killswitch_lock"] is None and Decimal(report["state"]["last_equity"]) == 750
        assert [e["event"] for e in report["events"]][-1] == "killswitch_reset"
        assert main(["status"]) == 0
        assert "Kill-Switch:       aus" in capsys.readouterr().out


def test_check_retries_forceexit_under_lock_while_trade_is_open(env: Path) -> None:
    guard_dir = env / "guard"
    with respx.mock(assert_all_called=False) as router:
        ft = MockFreqtrade(router, 1000.0)
        assert main(["check"]) == 0
        ft.equity = 750.0
        assert main(["check"]) == 0
        assert ft.forceexit.call_count == 1 and (guard_dir / "killswitch.lock").exists()
        # the exit did not fill (or Freqtrade silently skipped it): the trade is still open
        assert main(["check"]) == 0
        assert ft.forceexit.call_count == 2
        assert json.loads(ft.forceexit.calls[1].request.content) == {"tradeid": "all", "ordertype": "market"}
        assert _events(make_settings(env))[-1] == "killswitch_open_trades"
        # third run: forceexit again, the event/alert is rate-limited
        assert main(["check"]) == 0
        assert ft.forceexit.call_count == 3
        assert _events(make_settings(env)).count("killswitch_open_trades") == 1


def test_check_returns_zero_when_freqtrade_is_down(env: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{API}/token/login").mock(side_effect=httpx.ConnectError("refused"))
        assert main(["check"]) == 0
    state = read_json(env / "guard" / "state.json")
    assert state is not None and state["ft_failures"] == 1
    assert _events(make_settings(env)) == ["ft_unreachable"]


def test_check_without_credentials_fails_cleanly(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FT_API_PASS")
    assert main(["check"]) == 1


# -- run_check with fakes -------------------------------------------------------------


def test_btc_without_est_stake_is_valued_with_public_ticker(
    settings: Settings, alerter: RecordingAlerter
) -> None:
    """Freqtrade's ticker cache is empty: /balance.total shrinks to the EUR part. The guard
    must value the BTC itself instead of reading a phantom 50 % crash."""
    ft = FakeFT("1000", btc="0.01", price="50000")  # 500 EUR + 0.01 BTC
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    ft.est_stake_missing = True
    assert ft.balance()["total"] == 500.0
    t1 = T0 + timedelta(minutes=1)
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=t1, price_fetcher=lambda: Decimal("50000"))
    assert state.last_equity == Decimal("1000") and state.ft_failures == 0
    assert ft.calls == [] and alerter.sent == []
    assert not (settings.guard_dir / "killswitch.lock").exists()
    assert _events(settings) == ["day_start"]


def test_btc_without_est_stake_and_no_ticker_skips_the_run(
    settings: Settings, alerter: RecordingAlerter
) -> None:
    ft = FakeFT("1000", btc="0.01", price="50000")
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    ft.est_stake_missing = True

    def no_ticker() -> Decimal:
        raise RuntimeError("bitvavo 503")

    t1 = T0 + timedelta(minutes=1)
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=t1, price_fetcher=no_ticker)
    assert state.ft_failures == 1 and "est_stake" in (state.last_error or "")
    assert state.peak_equity == Decimal("1000") and state.last_equity == Decimal("1000")
    assert ft.calls == [] and not (settings.guard_dir / "killswitch.lock").exists()
    assert _events(settings) == ["day_start", "ft_unreachable"]


def test_failed_forceexit_is_alerted_and_retried(settings: Settings, alerter: RecordingAlerter) -> None:
    ft = FakeFT("1000", btc="0.02", price="50000")
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    ft.fail = {"forceexit"}
    ft.price = Decimal("37500")  # 25 % drawdown
    ft.equity = ft.eur + ft.btc * ft.price
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=1))
    # stopentry and the lock still happen, and the failure is alerted with priority urgent
    assert ft.calls == ["stopentry"]
    assert (settings.guard_dir / "killswitch.lock").exists()
    titles = [a[0] for a in alerter.sent]
    assert titles[:2] == ["Guard: Tagesverlust-Limit erreicht", "Guard: KILL-SWITCH ausgelöst"]
    assert titles[-1] == "Guard: Aktion forceexit fehlgeschlagen"
    assert alerter.sent[-1][2] == "urgent" and "502" in alerter.sent[-1][1]
    assert state.last_alerts["action_failed:forceexit"] == T0 + timedelta(minutes=1)
    # forceexit runs first, so its failure event precedes the daily_loss/killswitch events
    assert _events(settings) == ["day_start", "action_failed", "daily_loss", "killswitch"]
    # next run: the trade is still open under the lock, forceexit is sent again and succeeds
    ft.fail = set()
    run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=2))
    assert ft.calls == ["stopentry", "forceexit:all:market", "stopentry"]
    assert [a[0] for a in alerter.sent][-1] == "Guard: Kill-Switch, Positionen noch offen"


def test_failed_stopentry_is_retried_next_run(settings: Settings, alerter: RecordingAlerter) -> None:
    ft = FakeFT("1000")
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    ft.fail = {"stopentry"}
    ft.equity = ft.eur = Decimal("965")
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=1))
    assert ft.calls == [] and state.daily_loss_day is None
    assert [a[0] for a in alerter.sent][-1] == "Guard: Aktion stopentry fehlgeschlagen"
    # second failure one minute later: retried, alert rate-limited
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=2))
    assert state.daily_loss_day is None
    assert [a[0] for a in alerter.sent].count("Guard: Aktion stopentry fehlgeschlagen") == 1
    assert _events(settings).count("action_failed") == 2
    # Freqtrade back: stopentry goes through and the day is marked as handled
    ft.fail = set()
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=3))
    assert ft.calls == ["stopentry"] and state.daily_loss_day == "2026-09-15"
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=4))
    assert ft.calls == ["stopentry"]


def test_control_actions_run_before_alerts(settings: Settings) -> None:
    """Alerts are slow external HTTP calls (up to 2 x 20 s); forceexit, stopentry and the
    lock must not wait behind them."""
    order: list[str] = []

    class OrderedFT(FakeFT):
        def stopentry(self) -> dict[str, Any]:
            order.append("stopentry")
            return super().stopentry()

        def forceexit(self, tradeid: str = "all", ordertype: str | None = None) -> dict[str, Any]:
            order.append("forceexit")
            return super().forceexit(tradeid, ordertype)

    class OrderedAlerter(RecordingAlerter):
        def send(self, title: str, message: str, priority: str = "default") -> list[str]:
            order.append("alert")
            return super().send(title, message, priority)

    ft = OrderedFT("1000")
    alerter = OrderedAlerter()
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    ft.equity = ft.eur = Decimal("750")
    run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=1))
    assert order == ["forceexit", "stopentry", "alert", "alert"]
    assert (settings.guard_dir / "killswitch.lock").exists()


def test_dry_run_skips_reconciliation(settings: Settings, alerter: RecordingAlerter) -> None:
    ft = FakeFT("1000", dry_run=True)
    for i in range(3):
        state, _ = run_check(
            settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=i), exchange_factory=_no_exchange
        )
    assert state.reconcile_mismatches == 0 and ft.calls == [] and alerter.sent == []


def test_live_reconciliation_needs_ro_key_and_two_mismatches(
    tmp_path: Path, alerter: RecordingAlerter
) -> None:
    no_key = make_settings(tmp_path)
    ft = FakeFT("1000", dry_run=False)
    run_check(no_key, ft=ft, alerter=alerter, now=T0, exchange_factory=_no_exchange)

    settings = make_settings(tmp_path, BITVAVO_API_KEY_RO="k", BITVAVO_API_SECRET_RO="s")
    created: list[tuple[str, str, int]] = []
    exchange = FakeExchange(eur="990", btc="0")

    def factory(key: str, secret: str, operator_id: int) -> FakeExchange:
        created.append((key, secret, operator_id))
        return exchange

    t1, t2 = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=t1, exchange_factory=factory)
    assert created == [("k", "s", 1)]
    assert state.reconcile_mismatches == 1 and ft.calls == []
    state, _ = run_check(settings, ft=ft, alerter=alerter, now=t2, exchange_factory=factory)
    assert state.reconcile_mismatches == 2 and ft.calls == ["stopentry"]
    assert [a[0] for a in alerter.sent] == ["Guard: Bilanz weicht ab"]
    assert _events(settings)[-1] == "reconcile_mismatch"


def test_live_reconciliation_values_btc_with_ticker(tmp_path: Path, alerter: RecordingAlerter) -> None:
    settings = make_settings(tmp_path, BITVAVO_API_KEY_RO="k", BITVAVO_API_SECRET_RO="s")
    ft = FakeFT("1000", dry_run=False, eur="1000", btc="0")  # no BTC in Freqtrade -> no price in /balance
    exchange = FakeExchange(eur="1000", btc="0.001")  # 50 EUR of BTC nobody accounts for
    obs = cli.collect_observations(
        settings,
        ft,
        now=T0,
        guard_dir=settings.guard_dir,
        exchange_factory=lambda *_: exchange,
        price_fetcher=lambda: Decimal("50000"),
    )
    assert obs.btc_price == Decimal("50000") and obs.exchange_btc == Decimal("0.001")
    diff = guard.reconcile_diff_eur(obs.ft_eur, obs.ft_btc, obs.exchange_eur, obs.exchange_btc, obs.btc_price)
    assert diff == Decimal("50.000")


def test_exchange_failure_skips_reconciliation_and_alerts_after_three(
    tmp_path: Path, alerter: RecordingAlerter
) -> None:
    settings = make_settings(tmp_path, BITVAVO_API_KEY_RO="k", BITVAVO_API_SECRET_RO="s")
    ft = FakeFT("1000", dry_run=False)

    def broken(*_: Any) -> Any:
        raise RuntimeError("api down")

    for i in range(4):
        t = T0 + timedelta(minutes=i)
        state, _ = run_check(settings, ft=ft, alerter=alerter, now=t, exchange_factory=broken)
    assert state.reconcile_mismatches == 0 and state.exchange_failures == 4 and ft.calls == []
    assert [a[0] for a in alerter.sent] == ["Guard: Börsen-Bilanz nicht abrufbar"]
    assert "RuntimeError: api down" in alerter.sent[0][1]
    assert _events(settings).count("exchange_unreachable") == 1


def test_ft_unreachable_alerts_after_three_runs(settings: Settings, alerter: RecordingAlerter) -> None:
    ft = FakeFT(unreachable=True)
    for i in range(4):
        state, _ = run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=i))
    assert state.ft_failures == 4
    assert [a[0] for a in alerter.sent] == ["Guard: Freqtrade nicht erreichbar"]
    assert _events(settings) == ["ft_unreachable"] * 4


def test_daily_loss_end_to_end_sends_stopentry_once(settings: Settings, alerter: RecordingAlerter) -> None:
    ft = FakeFT("1000")
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    ft.equity = Decimal("965")
    ft.eur = ft.equity
    run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=1))
    run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=2))
    assert ft.calls == ["stopentry"]
    assert [a[0] for a in alerter.sent] == ["Guard: Tagesverlust-Limit erreicht"]
    assert _events(settings) == ["day_start", "daily_loss"]


def test_advisor_stale_read_from_decision_file(tmp_path: Path, alerter: RecordingAlerter) -> None:
    settings = make_settings(tmp_path, ADVISOR_MODE="gate", ADVISOR_INTERVAL_HOURS="1")
    settings.advisor_dir.mkdir(parents=True)
    ft = FakeFT("1000")
    (settings.advisor_dir / "decision.json").write_text(json.dumps({"created_at": "2026-09-15T09:30:00Z"}))
    run_check(settings, ft=ft, alerter=alerter, now=T0)
    assert alerter.sent == []
    (settings.advisor_dir / "decision.json").write_text(json.dumps({"created_at": "2026-09-15T06:30:00Z"}))
    run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=1))
    run_check(settings, ft=ft, alerter=alerter, now=T0 + timedelta(minutes=2))
    assert [a[0] for a in alerter.sent] == ["Guard: Advisor-Entscheidung veraltet"]
    assert "3.5 h alt" in alerter.sent[0][1]


# -- cancelOrdersAfter -----------------------------------------------------------------


def test_cod_renew_uses_trade_key_from_env(
    tmp_path: Path, alerter: RecordingAlerter, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path, GUARD_COD_ENABLED="true", GUARD_COD_SECONDS="90", BITVAVO_OPERATOR_ID="7"
    )
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    monkeypatch.setenv("GUARD_TRADE_API_KEY", "trade-key")
    monkeypatch.setenv("GUARD_TRADE_API_SECRET", "trade-secret")
    exchange = FakeExchange()
    created: list[tuple[str, str, int]] = []

    def factory(key: str, secret: str, operator_id: int) -> FakeExchange:
        created.append((key, secret, operator_id))
        return exchange

    run_check(settings, ft=FakeFT("1000"), alerter=alerter, now=T0, cod_exchange_factory=factory)
    assert created == [("trade-key", "trade-secret", 7)]
    assert exchange.cod_calls == [(90_000, {})]
    assert "action_failed" not in _events(settings)


def test_cod_without_trade_key_alerts_once_and_does_not_spam_events(
    tmp_path: Path, alerter: RecordingAlerter, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, GUARD_COD_ENABLED="true")
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    monkeypatch.setenv("GUARD_TRADE_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("GUARD_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("GUARD_TRADE_API_SECRET", raising=False)
    ft = FakeFT("1000")
    for i in range(3):
        t = T0 + timedelta(minutes=i)
        state, _ = run_check(settings, ft=ft, alerter=alerter, now=t, cod_exchange_factory=_no_exchange)
    events = read_jsonl_tail(settings.guard_dir / "events.jsonl", 10)
    failed = [e for e in events if e["event"] == "action_failed"]
    assert len(failed) == 1 and failed[0]["details"]["action"] == "cod_renew"
    assert "GUARD_TRADE_API_KEY" in failed[0]["details"]["error"]
    assert [a[0] for a in alerter.sent] == ["Guard: Dead-Man's-Switch nicht aktiv"]
    assert alerter.sent[0][2] == "high"
    assert state.last_alerts["action_failed:cod_renew"] == T0
    # 6 h later: alert and event again
    t = T0 + timedelta(hours=6)
    run_check(settings, ft=FakeFT("1000"), alerter=alerter, now=t, cod_exchange_factory=_no_exchange)
    assert len(alerter.sent) == 2 and _events(settings).count("action_failed") == 2


def test_cod_seconds_out_of_range_alerts(tmp_path: Path, alerter: RecordingAlerter) -> None:
    settings = make_settings(tmp_path, GUARD_COD_ENABLED="true", GUARD_COD_SECONDS="600")
    exchange = FakeExchange()
    run_check(
        settings,
        ft=FakeFT("1000"),
        alerter=alerter,
        now=T0,
        cod_exchange_factory=lambda *_: exchange,
        trade_key=("k", "s"),
    )
    assert exchange.cod_calls == []
    assert [a[0] for a in alerter.sent] == ["Guard: Dead-Man's-Switch nicht aktiv"]
    assert "GUARD_COD_SECONDS" in alerter.sent[0][1]


def test_cod_disabled_never_touches_the_exchange(settings: Settings, alerter: RecordingAlerter) -> None:
    run_check(settings, ft=FakeFT("1000"), alerter=alerter, now=T0, cod_exchange_factory=_no_exchange)


def test_trade_key_prefers_env_then_dedicated_file_then_shared_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shared = tmp_path / "btctrader.env"
    shared.write_text('GUARD_TRADE_API_KEY="from-shared"\nGUARD_TRADE_API_SECRET=shared-secret\n')
    dedicated = tmp_path / "secrets-guard.env"
    dedicated.write_text("GUARD_TRADE_API_KEY=from-dedicated\nGUARD_TRADE_API_SECRET=dedicated-secret\n")
    missing = str(tmp_path / "missing.env")

    base = {"BTCTRADER_ENV_FILE": str(shared), "GUARD_TRADE_ENV_FILE": str(dedicated)}
    assert cli.trade_key_from_env(base) == ("from-dedicated", "dedicated-secret")
    env = {**base, "GUARD_TRADE_API_KEY": "from-env", "GUARD_TRADE_API_SECRET": "env-secret"}
    assert cli.trade_key_from_env(env) == ("from-env", "env-secret")
    # dedicated file missing: fall back to the shared env file, with a warning
    with caplog.at_level("WARNING", logger="btctrader.guard"):
        env = {"BTCTRADER_ENV_FILE": str(shared), "GUARD_TRADE_ENV_FILE": missing}
        assert cli.trade_key_from_env(env) == ("from-shared", "shared-secret")
    assert "secrets-guard.env" in caplog.text and "every service" in caplog.text
    env = {"BTCTRADER_ENV_FILE": str(shared), "GUARD_TRADE_ENV_FILE": missing}
    assert cli.trade_key_from_env({**env, "GUARD_TRADE_API_KEY": "from-env"}) == ("from-env", "shared-secret")
    assert cli.trade_key_from_env({"BTCTRADER_ENV_FILE": "", "GUARD_TRADE_ENV_FILE": missing}) == ("", "")


# -- reset ---------------------------------------------------------------------------


def test_reset_keeps_lock_when_state_cannot_be_saved(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    guard_dir = settings.guard_dir
    guard_dir.mkdir(parents=True)
    (guard_dir / "killswitch.lock").write_text('{"ts": "2026-09-15T10:00:00Z"}')
    cli.save_state(guard_dir, guard.GuardState(peak_equity=Decimal("1000")))

    def broken_save(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "save_state", broken_save)
    with pytest.raises(OSError):
        cli.run_reset(settings, confirm=True, now=T0)
    assert (guard_dir / "killswitch.lock").exists()
    state = read_json(guard_dir / "state.json")
    assert state is not None and Decimal(state["peak_equity"]) == 1000


def test_check_and_reset_share_the_run_lock(settings: Settings, alerter: RecordingAlerter) -> None:
    run_check(settings, ft=FakeFT("1000"), alerter=alerter, now=T0)
    assert (settings.guard_dir / ".run.lock").exists()
    assert cli.run_reset(settings, confirm=True, now=T0 + timedelta(minutes=1)) == 0
    last = read_jsonl_tail(settings.guard_dir / "events.jsonl", 1)[0]
    assert last["event"] == "killswitch_reset" and parse_iso(last["ts"]) == T0 + timedelta(minutes=1)
