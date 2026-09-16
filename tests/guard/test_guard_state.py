"""Tests for the pure state machine in btctrader.guard.guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from btctrader.common.db import parse_iso
from btctrader.guard import guard
from btctrader.guard.guard import Action, GuardConfig, GuardState, evaluate
from tests.guard.conftest import T0, events, fresh_state, kinds, obs

# -- day rollover and daily loss -----------------------------------------------------


def test_first_run_sets_day_start_and_peak(cfg: GuardConfig) -> None:
    state, actions = evaluate(fresh_state(), obs("1000"), cfg)
    assert state.day_key == "2026-09-15"
    assert state.day_start_equity == Decimal("1000")
    assert state.peak_equity == Decimal("1000")
    assert events(actions) == [guard.EVENT_DAY_START]
    assert "stopentry" not in kinds(actions)


def test_day_rollover_sets_day_start_equity(cfg: GuardConfig) -> None:
    state, _ = evaluate(fresh_state(), obs("1000"), cfg)
    # later the same UTC day: day start stays
    state, actions = evaluate(state, obs("990", now=T0 + timedelta(hours=13)), cfg)
    assert state.day_start_equity == Decimal("1000")
    assert events(actions) == []
    # first run after 00:00 UTC: new day key and day start equity
    state, actions = evaluate(state, obs("990", now=T0 + timedelta(hours=14, minutes=1)), cfg)
    assert state.day_key == "2026-09-16"
    assert state.day_start_equity == Decimal("990")
    assert events(actions) == [guard.EVENT_DAY_START]


def test_daily_loss_triggers_stopentry_once_per_day(cfg: GuardConfig) -> None:
    state, _ = evaluate(fresh_state(), obs("1000"), cfg)
    # 2.9 % loss: nothing
    state, actions = evaluate(state, obs("971", now=T0 + timedelta(minutes=1)), cfg)
    assert kinds(actions) == []
    # 3.0 % loss: stopentry, alert, event
    state, actions = evaluate(state, obs("970", now=T0 + timedelta(minutes=2)), cfg)
    assert kinds(actions) == ["stopentry", "alert", "event"]
    assert events(actions) == [guard.EVENT_DAILY_LOSS]
    assert actions[0].details["reason"] == guard.EVENT_DAILY_LOSS
    assert state.daily_loss_day == "2026-09-15"
    # deeper loss the same day: no second stopentry
    state, actions = evaluate(state, obs("950", now=T0 + timedelta(minutes=3)), cfg)
    assert kinds(actions) == []
    # next day resets: the new day start is 950, so 950 is no loss
    state, actions = evaluate(state, obs("950", now=T0 + timedelta(days=1)), cfg)
    assert "stopentry" not in kinds(actions)
    assert state.day_start_equity == Decimal("950")
    # ... and a fresh 3 % loss on the new day triggers again
    state, actions = evaluate(state, obs("921.5", now=T0 + timedelta(days=1, minutes=1)), cfg)
    assert "stopentry" in kinds(actions)
    assert state.daily_loss_day == "2026-09-16"


# -- drawdown kill switch --------------------------------------------------------------


def test_peak_equity_follows_new_highs(cfg: GuardConfig) -> None:
    state, _ = evaluate(fresh_state(), obs("1000"), cfg)
    state, _ = evaluate(state, obs("1100", now=T0 + timedelta(minutes=1)), cfg)
    assert state.peak_equity == Decimal("1100")
    state, _ = evaluate(state, obs("1050", now=T0 + timedelta(minutes=2)), cfg)
    assert state.peak_equity == Decimal("1100")


def test_drawdown_triggers_forceexit_stopentry_and_lock(cfg: GuardConfig) -> None:
    state, _ = evaluate(fresh_state(), obs("1000"), cfg)
    state, actions = evaluate(state, obs("801", now=T0 + timedelta(minutes=1)), cfg)
    assert "forceexit" not in kinds(actions)  # 19.9 %: not yet
    state, actions = evaluate(state, obs("800", now=T0 + timedelta(minutes=2)), cfg)
    ks = [a for a in actions if a.kind in ("forceexit", "stopentry", "write_lock")]
    assert [a.kind for a in ks] == ["forceexit", "stopentry", "write_lock"]
    # market order: a resting limit exit may never fill in the crash that triggered the kill switch
    assert ks[0].details == {"reason": "killswitch", "tradeid": "all", "ordertype": "market"}
    lock = ks[2].details["content"]
    assert lock["equity_eur"] == "800.00"
    assert lock["peak_equity_eur"] == "1000.00"
    assert lock["drawdown_pct"] == "20.00"
    assert parse_iso(lock["ts"]) == T0 + timedelta(minutes=2) and lock["ts"].endswith("Z")
    assert guard.EVENT_KILLSWITCH in events(actions)
    urgent = [a for a in actions if a.kind == "alert" and a.details["priority"] == "urgent"]
    assert len(urgent) == 1 and "Market-Order" in urgent[0].details["message"]
    assert state.killswitch_at == T0 + timedelta(minutes=2)


def test_lock_reapplies_stopentry_without_new_killswitch(cfg: GuardConfig) -> None:
    state = fresh_state(day_key="2026-09-15", day_start_equity=Decimal("800"), peak_equity=Decimal("1000"))
    state, actions = evaluate(state, obs("700", lock_exists=True), cfg)
    assert kinds(actions) == ["stopentry"]
    assert actions[0].details["reason"] == "killswitch_lock"
    # even when equity recovers, the lock keeps the bot from entering
    state, actions = evaluate(state, obs("1200", now=T0 + timedelta(minutes=1), lock_exists=True), cfg)
    assert kinds(actions) == ["stopentry"]
    assert state.peak_equity == Decimal("1200")


def test_lock_retries_forceexit_while_trades_are_still_open(cfg: GuardConfig) -> None:
    """A failed, cancelled or unfilled exit leaves a position open under the lock:
    forceexit is re-issued on every run, the alert is rate-limited."""
    state = fresh_state(day_key="2026-09-15", day_start_equity=Decimal("800"), peak_equity=Decimal("1000"))
    state, actions = evaluate(state, obs("700", lock_exists=True, open_trades=1), cfg)
    assert kinds(actions) == ["stopentry", "forceexit", "alert", "event"]
    assert actions[1].details == {"reason": "killswitch_open_trades", "tradeid": "all", "ordertype": "market"}
    assert actions[2].details["priority"] == "urgent" and "noch offen" in actions[2].details["title"]
    assert events(actions) == [guard.EVENT_KILLSWITCH_OPEN_TRADES]
    assert state.last_alerts[guard.EVENT_KILLSWITCH_OPEN_TRADES] == T0
    # one minute later, still open: forceexit again, but no second alert
    t1 = T0 + timedelta(minutes=1)
    state, actions = evaluate(state, obs("700", now=t1, lock_exists=True, open_trades=1), cfg)
    assert kinds(actions) == ["stopentry", "forceexit"]
    # 6 h later: alert again
    later = T0 + timedelta(hours=6)
    state, actions = evaluate(state, obs("700", now=later, lock_exists=True, open_trades=1), cfg)
    assert kinds(actions) == ["stopentry", "forceexit", "alert", "event"]
    # flat: only the stopentry re-apply, no forceexit
    state, actions = evaluate(state, obs("700", now=later + timedelta(minutes=1), lock_exists=True), cfg)
    assert kinds(actions) == ["stopentry"]


def test_lock_leaves_a_paused_or_stopped_bot_alone(cfg: GuardConfig) -> None:
    """/stopentry on a stopped bot restarts it in paused state; the guard must not undo an
    operator's /stop or /pause. Unknown state counts as running."""
    state = fresh_state(day_key="2026-09-15", day_start_equity=Decimal("800"), peak_equity=Decimal("1000"))
    for bot_state in ("paused", "stopped"):
        _, actions = evaluate(state, obs("700", lock_exists=True, bot_state=bot_state), cfg)
        assert kinds(actions) == [], bot_state
    for bot_state in ("running", None):
        _, actions = evaluate(state, obs("700", lock_exists=True, bot_state=bot_state), cfg)
        assert kinds(actions) == ["stopentry"], bot_state
    # open trades are force-exited regardless of the bot state (the failure is alerted by the CLI)
    _, actions = evaluate(state, obs("700", lock_exists=True, bot_state="stopped", open_trades=1), cfg)
    assert kinds(actions) == ["forceexit", "alert", "event"]


def test_prioritize_actions_puts_control_before_alerts() -> None:
    actions = [
        Action("stopentry", {"reason": "daily_loss"}),
        Action("alert", {"title": "a"}),
        Action("event", {"event": "daily_loss"}),
        Action("forceexit", {"reason": "killswitch"}),
        Action("write_lock", {}),
        Action("alert", {"title": "b"}),
        Action("event", {"event": "killswitch"}),
        Action("cod_renew", {"seconds": 120}),
    ]
    ordered = guard.prioritize_actions(actions)
    assert [a.kind for a in ordered] == [
        "forceexit", "stopentry", "write_lock", "event", "event", "alert", "alert", "cod_renew"
    ]
    # stable: the relative order within a kind is kept
    assert [a.details["title"] for a in ordered if a.kind == "alert"] == ["a", "b"]


def test_lock_suspends_daily_loss_but_new_conditions_still_alert(cfg: GuardConfig) -> None:
    # Day rollover under the lock: bookkeeping only, no daily-loss alert for the crash that caused it
    state = fresh_state(day_key="2026-09-14", day_start_equity=Decimal("1000"), peak_equity=Decimal("1000"))
    state, actions = evaluate(state, obs("700", lock_exists=True), cfg)
    assert kinds(actions) == ["event", "stopentry"] and events(actions) == [guard.EVENT_DAY_START]
    assert state.day_start_equity == Decimal("700") and state.daily_loss_day is None
    # A reconciliation mismatch is a new condition: it alerts, but stopentry is still sent only once
    state = replace(state, reconcile_mismatches=1)
    live = _live_obs(T0 + timedelta(minutes=1), "700", "600", lock_exists=True)
    state, actions = evaluate(state, live, cfg)
    assert kinds(actions) == ["stopentry", "alert", "event"]
    assert actions[0].details["reason"] == "killswitch_lock"
    assert events(actions) == [guard.EVENT_RECONCILE_MISMATCH]


def test_reset_state_clears_peak_so_it_does_not_retrigger(cfg: GuardConfig) -> None:
    state = fresh_state(peak_equity=Decimal("1000"), killswitch_at=T0, reconcile_mismatches=3)
    state = guard.reset_state_after_unlock(state)
    assert state.peak_equity is None and state.killswitch_at is None and state.reconcile_mismatches == 0
    state, actions = evaluate(state, obs("700"), cfg)
    assert "forceexit" not in kinds(actions)
    assert state.peak_equity == Decimal("700")


# -- reconciliation --------------------------------------------------------------------


def _live_obs(now, ft_eur: str, ex_eur: str, **kw):  # type: ignore[no-untyped-def]
    return obs(
        "1000",
        now=now,
        dry_run=False,
        ft_eur=ft_eur,
        ft_btc="0",
        exchange_eur=ex_eur,
        exchange_btc="0",
        btc_price="50000",
        **kw,
    )


def test_reconcile_mismatch_needs_two_consecutive_runs(cfg: GuardConfig) -> None:
    state, actions = evaluate(fresh_state(), _live_obs(T0, "1000", "990"), cfg)
    assert state.reconcile_mismatches == 1
    assert "stopentry" not in kinds(actions)
    # a matching run in between resets the counter
    state, actions = evaluate(state, _live_obs(T0 + timedelta(minutes=1), "1000", "1001"), cfg)
    assert state.reconcile_mismatches == 0
    state, actions = evaluate(state, _live_obs(T0 + timedelta(minutes=2), "1000", "990"), cfg)
    assert state.reconcile_mismatches == 1
    state, actions = evaluate(state, _live_obs(T0 + timedelta(minutes=3), "1000", "990"), cfg)
    assert state.reconcile_mismatches == 2
    assert kinds(actions) == ["stopentry", "alert", "event"]
    assert events(actions) == [guard.EVENT_RECONCILE_MISMATCH]
    assert actions[2].details["details"]["diff_eur"] == "10.00"
    # third run: stopentry again, but the alert is rate-limited
    state, actions = evaluate(state, _live_obs(T0 + timedelta(minutes=4), "1000", "990"), cfg)
    assert kinds(actions) == ["stopentry"]


def test_reconcile_counter_survives_exchange_errors(cfg: GuardConfig) -> None:
    """mismatch, exchange error, mismatch must reach the threshold: a run without exchange
    data is neither a match nor a mismatch."""
    state, _ = evaluate(fresh_state(), _live_obs(T0, "1000", "990"), cfg)
    assert state.reconcile_mismatches == 1
    broken = obs("1000", now=T0 + timedelta(minutes=1), dry_run=False, ft_eur="1000", ft_btc="0",
                 exchange_error="RequestTimeout: bitvavo GET /balance")
    state, actions = evaluate(state, broken, cfg)
    assert state.reconcile_mismatches == 1 and "stopentry" not in kinds(actions)
    state, actions = evaluate(state, _live_obs(T0 + timedelta(minutes=2), "1000", "990"), cfg)
    assert state.reconcile_mismatches == 2
    assert kinds(actions) == ["stopentry", "alert", "event"]
    # dry-run resets the counter
    state, _ = evaluate(state, obs("1000", now=T0 + timedelta(minutes=3), dry_run=True), cfg)
    assert state.reconcile_mismatches == 0


def test_exchange_failures_alert_after_three_runs_and_recover(cfg: GuardConfig) -> None:
    state = fresh_state(day_key="2026-09-15", day_start_equity=Decimal("1000"), peak_equity=Decimal("1000"))
    for i in range(1, 5):
        broken = obs("1000", now=T0 + timedelta(minutes=i), dry_run=False, ft_eur="1000", ft_btc="0",
                     exchange_error="AuthenticationError: invalid key")
        state, actions = evaluate(state, broken, cfg)
        assert state.exchange_failures == i
        if i == 3:
            assert kinds(actions) == ["event", "alert"]
            assert events(actions) == [guard.EVENT_EXCHANGE_UNREACHABLE]
            assert "invalid key" in actions[1].details["message"]
        else:
            assert kinds(actions) == []  # below threshold or rate-limited
    # live run without RO key (no attempt): nothing counted, nothing reset
    state, actions = evaluate(state, obs("1000", now=T0 + timedelta(minutes=5), dry_run=False), cfg)
    assert state.exchange_failures == 4 and kinds(actions) == []
    # exchange back: recovery event + info alert, counter reset, rate limit cleared
    state, actions = evaluate(state, _live_obs(T0 + timedelta(minutes=6), "1000", "1000"), cfg)
    assert state.exchange_failures == 0 and state.reconcile_mismatches == 0
    assert events(actions) == [guard.EVENT_EXCHANGE_RECOVERED]
    assert guard.EVENT_EXCHANGE_UNREACHABLE not in state.last_alerts


def test_reconcile_values_btc_difference_with_price(cfg: GuardConfig) -> None:
    diff = guard.reconcile_diff_eur(
        Decimal("100"), Decimal("0.01"), Decimal("100"), Decimal("0.0099"), Decimal("50000")
    )
    assert diff == Decimal("5.0000")
    assert guard.reconcile_diff_eur(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("2"), None) is None


def test_dry_run_skips_reconciliation_in_state_machine(cfg: GuardConfig) -> None:
    state = fresh_state()
    for i in range(3):
        o = obs(
            "1000",
            now=T0 + timedelta(minutes=i),
            dry_run=True,
            ft_eur="1000",
            ft_btc="0",
            exchange_eur="500",
            exchange_btc="0",
        )
        state, actions = evaluate(state, o, cfg)
        assert "stopentry" not in kinds(actions)
    assert state.reconcile_mismatches == 0


# -- advisor freshness ------------------------------------------------------------------


def test_advisor_staleness_alert_rate_limited_to_6h() -> None:
    cfg = GuardConfig(advisor_mode="gate", advisor_interval_hours=1.0)
    fresh = T0 - timedelta(hours=2)
    stale = T0 - timedelta(hours=3, minutes=1)
    state, actions = evaluate(fresh_state(), obs("1000", advisor_created_at=fresh), cfg)
    assert guard.EVENT_ADVISOR_STALE not in events(actions)

    later = T0 + timedelta(minutes=1)
    state, actions = evaluate(state, obs("1000", now=later, advisor_created_at=stale), cfg)
    assert events(actions) == [guard.EVENT_ADVISOR_STALE]
    assert sum(1 for a in actions if a.kind == "alert") == 1
    assert state.last_alerts[guard.EVENT_ADVISOR_STALE] == T0 + timedelta(minutes=1)

    # 5 h 59 min later: still stale, no new alert
    later = T0 + timedelta(hours=6)
    state, actions = evaluate(state, obs("1000", now=later, advisor_created_at=stale), cfg)
    assert kinds(actions) == []
    # 6 h after the first alert: alert again
    later = T0 + timedelta(hours=6, minutes=1)
    state, actions = evaluate(state, obs("1000", now=later, advisor_created_at=stale), cfg)
    assert events(actions) == [guard.EVENT_ADVISOR_STALE]


def test_advisor_missing_file_is_stale_but_shadow_mode_ignores_it() -> None:
    gate = GuardConfig(advisor_mode="gate")
    _, actions = evaluate(fresh_state(), obs("1000", advisor_created_at=None), gate)
    assert guard.EVENT_ADVISOR_STALE in events(actions)
    shadow = GuardConfig(advisor_mode="shadow")
    _, actions = evaluate(fresh_state(), obs("1000", advisor_created_at=None), shadow)
    assert guard.EVENT_ADVISOR_STALE not in events(actions)


# -- freqtrade unreachable ---------------------------------------------------------------


def test_ft_unreachable_alerts_after_three_failures(cfg: GuardConfig) -> None:
    state = fresh_state(day_key="2026-09-15", day_start_equity=Decimal("1000"), peak_equity=Decimal("1000"))
    down = obs(None, ft_ok=False, ft_error="connection refused")
    for i in range(1, 3):
        state, actions = evaluate(state, down, cfg)
        assert state.ft_failures == i
        assert events(actions) == [guard.EVENT_FT_UNREACHABLE]
        assert "alert" not in kinds(actions)
    state, actions = evaluate(state, down, cfg)
    assert state.ft_failures == 3
    assert kinds(actions) == ["event", "alert"]
    assert "3 Fehlläufe" in actions[1].details["message"]
    # fourth failure: event, but the alert is rate-limited
    state, actions = evaluate(state, down, cfg)
    assert kinds(actions) == ["event"]
    # state untouched by the outage
    assert state.peak_equity == Decimal("1000") and state.day_start_equity == Decimal("1000")
    # recovery: counter reset, recovery event and info alert
    state, actions = evaluate(state, obs("990", now=T0 + timedelta(minutes=5)), cfg)
    assert state.ft_failures == 0
    assert guard.EVENT_FT_RECOVERED in events(actions)
    assert guard.EVENT_FT_UNREACHABLE not in state.last_alerts


def test_ft_unreachable_skips_cod_renewal() -> None:
    cfg = GuardConfig(cod_enabled=True)
    _, actions = evaluate(fresh_state(), obs("1000"), cfg)
    assert guard.ACTION_COD_RENEW in kinds(actions)
    _, actions = evaluate(fresh_state(), obs(None, ft_ok=False), cfg)
    assert guard.ACTION_COD_RENEW not in kinds(actions)


# -- persistence round trip --------------------------------------------------------------


def test_state_round_trip_and_tolerant_parsing() -> None:
    state = GuardState(
        day_key="2026-09-15",
        day_start_equity=Decimal("1000.50"),
        daily_loss_day="2026-09-15",
        peak_equity=Decimal("1200"),
        ft_failures=2,
        reconcile_mismatches=1,
        exchange_failures=3,
        last_alerts={"advisor_stale": T0},
        last_equity=Decimal("990"),
        last_check=T0,
        last_error=None,
        killswitch_at=None,
    )
    data = state.to_dict()
    assert data["schema_version"] == 1
    assert data["day_start_equity"] == "1000.50"
    assert list(data["last_alerts"]) == ["advisor_stale"]
    assert data["last_alerts"]["advisor_stale"].endswith("Z")
    assert parse_iso(data["last_alerts"]["advisor_stale"]) == T0
    assert data["exchange_failures"] == 3
    assert GuardState.from_dict(data) == state
    broken = GuardState.from_dict({"peak_equity": "abc", "ft_failures": "x", "last_alerts": "no"})
    assert broken == GuardState()
    assert GuardState.from_dict(None) == GuardState()
