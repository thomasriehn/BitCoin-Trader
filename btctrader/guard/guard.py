"""Guard state machine (docs/KOMPONENTEN.md section 8).

Everything in this module is pure: ``evaluate`` takes the previous ``GuardState``,
the ``Observations`` of the current run and the ``GuardConfig`` and returns the
new state plus a list of ``Action`` objects. The CLI performs the I/O (Freqtrade
calls, lock file, alerts, events). This keeps every rule testable without
network or filesystem access.

Checks in order (numbers refer to the contract):

  7. Freqtrade unreachable: event on every failed run, alert after 3 consecutive
     failures (repeated at most every 6 hours), recovery event when it is back.
  2. Day rollover at 00:00 UTC sets ``day_start_equity``; a loss of at least
     ``daily_loss_pct`` triggers ``stopentry`` once per UTC day.
  3. ``peak_equity`` tracks the equity high; a drawdown of at least
     ``max_drawdown_pct`` triggers ``forceexit`` + ``stopentry`` + lock file.
     While the lock exists, ``stopentry`` is re-applied on every run.
  4. Reconciliation (live only): exchange vs. Freqtrade balances; a mismatch
     above the tolerance on two consecutive runs triggers ``stopentry``.
  5. Advisor freshness (``ADVISOR_MODE=gate`` only): alert at most every 6 hours
     when ``decision.json`` is missing or older than 3 x interval.
  6. Optional ``cancelOrdersAfter`` renewal when the bot is reachable.

State persists in ``state.json``; the lock file ``killswitch.lock`` is the source
of truth for the kill switch (the CLI reports its existence as an observation).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from btctrader.common.db import iso_utc, parse_iso

if TYPE_CHECKING:
    from btctrader.common.config import Settings

STATE_SCHEMA_VERSION = 1

FT_FAILURE_ALERT_AFTER = 3
"""Consecutive failed runs before an ``ft_unreachable`` alert is sent."""

ALERT_REPEAT_HOURS = 6.0
"""Minimum distance between two alerts of the same kind (advisor, reconcile, ft)."""

ADVISOR_STALE_FACTOR = 3
"""``decision.json`` is stale when older than this many advisor intervals."""

RECONCILE_RUNS_REQUIRED = 2
"""Consecutive runs with a balance mismatch before the guard reacts."""

# Event names written to events.jsonl
EVENT_DAY_START = "day_start"
EVENT_DAILY_LOSS = "daily_loss"
EVENT_KILLSWITCH = "killswitch"
EVENT_KILLSWITCH_RESET = "killswitch_reset"
EVENT_RECONCILE_MISMATCH = "reconcile_mismatch"
EVENT_ADVISOR_STALE = "advisor_stale"
EVENT_FT_UNREACHABLE = "ft_unreachable"
EVENT_FT_RECOVERED = "ft_recovered"
EVENT_ACTION_FAILED = "action_failed"

# Action kinds
ACTION_STOPENTRY = "stopentry"
ACTION_FORCEEXIT = "forceexit"
ACTION_WRITE_LOCK = "write_lock"
ACTION_ALERT = "alert"
ACTION_EVENT = "event"
ACTION_COD_RENEW = "cod_renew"

ZERO = Decimal(0)
HUNDRED = Decimal(100)
Q2 = Decimal("0.01")


# -- data types ----------------------------------------------------------------------


@dataclass(frozen=True)
class GuardConfig:
    """The subset of ``Settings`` the state machine needs."""

    daily_loss_pct: Decimal = Decimal("3.0")
    max_drawdown_pct: Decimal = Decimal("20.0")
    reconcile_tol_eur: Decimal = Decimal("2.0")
    advisor_mode: str = "shadow"
    advisor_interval_hours: float = 1.0
    cod_enabled: bool = False
    cod_seconds: int = 120
    ft_failure_alert_after: int = FT_FAILURE_ALERT_AFTER
    alert_repeat_hours: float = ALERT_REPEAT_HOURS

    @classmethod
    def from_settings(cls, settings: Settings) -> GuardConfig:
        return cls(
            daily_loss_pct=settings.guard_daily_loss_pct,
            max_drawdown_pct=settings.guard_max_drawdown_pct,
            reconcile_tol_eur=settings.guard_reconcile_tol_eur,
            advisor_mode=settings.advisor_mode,
            advisor_interval_hours=settings.advisor_interval_hours,
            cod_enabled=settings.guard_cod_enabled,
            cod_seconds=settings.guard_cod_seconds,
        )


@dataclass(frozen=True)
class Observations:
    """What the CLI observed in one run. ``equity`` is None when Freqtrade failed."""

    now: datetime
    ft_ok: bool = True
    ft_error: str = ""
    equity: Decimal | None = None
    dry_run: bool | None = None
    open_trades: int = 0
    ft_eur: Decimal | None = None
    ft_btc: Decimal | None = None
    exchange_eur: Decimal | None = None
    exchange_btc: Decimal | None = None
    btc_price: Decimal | None = None
    lock_exists: bool = False
    advisor_created_at: datetime | None = None

    @property
    def freqtrade_available(self) -> bool:
        return self.ft_ok and self.equity is not None

    @property
    def exchange_available(self) -> bool:
        return self.exchange_eur is not None and self.exchange_btc is not None


@dataclass(frozen=True)
class GuardState:
    """Persisted guard state (``state.json``). Money as Decimal, times as aware UTC datetimes."""

    day_key: str | None = None
    day_start_equity: Decimal | None = None
    daily_loss_day: str | None = None
    peak_equity: Decimal | None = None
    ft_failures: int = 0
    reconcile_mismatches: int = 0
    last_alerts: dict[str, datetime] = field(default_factory=dict)
    last_equity: Decimal | None = None
    last_check: datetime | None = None
    last_error: str | None = None
    killswitch_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "day_key": self.day_key,
            "day_start_equity": _dec_str(self.day_start_equity),
            "daily_loss_day": self.daily_loss_day,
            "peak_equity": _dec_str(self.peak_equity),
            "ft_failures": self.ft_failures,
            "reconcile_mismatches": self.reconcile_mismatches,
            "last_alerts": {k: iso_utc(v) for k, v in sorted(self.last_alerts.items())},
            "last_equity": _dec_str(self.last_equity),
            "last_check": iso_utc(self.last_check) if self.last_check else None,
            "last_error": self.last_error,
            "killswitch_at": iso_utc(self.killswitch_at) if self.killswitch_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GuardState:
        """Build a state from ``state.json`` content; unknown or broken fields fall back to defaults."""
        if not data:
            return cls()
        alerts: dict[str, datetime] = {}
        raw_alerts = data.get("last_alerts")
        if isinstance(raw_alerts, dict):
            for key, value in raw_alerts.items():
                dt = _parse_dt(value)
                if dt is not None:
                    alerts[str(key)] = dt
        return cls(
            day_key=_opt_str(data.get("day_key")),
            day_start_equity=_parse_dec(data.get("day_start_equity")),
            daily_loss_day=_opt_str(data.get("daily_loss_day")),
            peak_equity=_parse_dec(data.get("peak_equity")),
            ft_failures=_parse_int(data.get("ft_failures")),
            reconcile_mismatches=_parse_int(data.get("reconcile_mismatches")),
            last_alerts=alerts,
            last_equity=_parse_dec(data.get("last_equity")),
            last_check=_parse_dt(data.get("last_check")),
            last_error=_opt_str(data.get("last_error")),
            killswitch_at=_parse_dt(data.get("killswitch_at")),
        )


@dataclass(frozen=True)
class Action:
    """Something the CLI has to do. ``details`` depends on ``kind``.

    * ``stopentry`` / ``forceexit``: ``reason``
    * ``write_lock``: ``content`` (dict written to ``killswitch.lock``)
    * ``alert``: ``title``, ``message``, ``priority``
    * ``event``: ``event``, ``details``
    * ``cod_renew``: ``seconds``
    """

    kind: str
    details: dict[str, Any] = field(default_factory=dict)


def event(name: str, **details: Any) -> Action:
    return Action(ACTION_EVENT, {"event": name, "details": details})


def alert(title: str, message: str, priority: str = "high") -> Action:
    return Action(ACTION_ALERT, {"title": title, "message": message, "priority": priority})


# -- helpers -------------------------------------------------------------------------


def _dec_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _parse_dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result.is_finite() else None


def _parse_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2)


def fmt_eur(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{q2(value)} EUR"


def day_key_for(now: datetime) -> str:
    """UTC calendar day key ``YYYY-MM-DD``."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def pct_change(base: Decimal, current: Decimal) -> Decimal:
    """Percentage of ``current`` relative to ``base`` (negative = loss). 0 when ``base`` is 0."""
    if base <= ZERO:
        return ZERO
    return (current - base) / base * HUNDRED


def drawdown_pct(peak: Decimal, current: Decimal) -> Decimal:
    """Drawdown from ``peak`` in percent (>= 0)."""
    if peak <= ZERO:
        return ZERO
    return max(ZERO, (peak - current) / peak * HUNDRED)


def reconcile_diff_eur(
    ft_eur: Decimal,
    ft_btc: Decimal,
    exchange_eur: Decimal,
    exchange_btc: Decimal,
    btc_price: Decimal | None,
) -> Decimal | None:
    """Absolute balance difference in EUR (EUR diff + BTC diff x price).

    Returns None when BTC balances differ but no price is known (cannot be valued).
    """
    eur_diff = abs(ft_eur - exchange_eur)
    btc_diff = abs(ft_btc - exchange_btc)
    if btc_diff == ZERO:
        return eur_diff
    if btc_price is None or btc_price <= ZERO:
        return None
    return eur_diff + btc_diff * btc_price


def advisor_is_stale(created_at: datetime | None, now: datetime, interval_hours: float) -> bool:
    """True when ``decision.json`` is missing or older than ``ADVISOR_STALE_FACTOR`` x interval."""
    if created_at is None:
        return True
    max_age = timedelta(hours=interval_hours * ADVISOR_STALE_FACTOR)
    return now - created_at > max_age


def _may_alert(state: GuardState, key: str, now: datetime, hours: float) -> bool:
    last = state.last_alerts.get(key)
    return last is None or now - last >= timedelta(hours=hours)


def lock_content(now: datetime, equity: Decimal, peak: Decimal, reason: str) -> dict[str, Any]:
    """Content of ``killswitch.lock`` (time, equity, peak, plus drawdown and reason)."""
    return {
        "ts": iso_utc(now),
        "equity_eur": _dec_str(q2(equity)),
        "peak_equity_eur": _dec_str(q2(peak)),
        "drawdown_pct": _dec_str(q2(drawdown_pct(peak, equity))),
        "reason": reason,
    }


# -- the state machine ---------------------------------------------------------------


def evaluate(state: GuardState, obs: Observations, cfg: GuardConfig) -> tuple[GuardState, list[Action]]:
    """Run all checks for one guard run. Pure: no I/O, no clock access."""
    actions: list[Action] = []
    now = obs.now
    alerts = dict(state.last_alerts)
    new = replace(state, last_check=now, last_alerts=alerts)

    if not obs.freqtrade_available:
        new, actions = _check_ft_unreachable(new, obs, cfg, actions)
        new, actions = _check_advisor(new, obs, cfg, actions)
        return new, actions

    equity = obs.equity
    assert equity is not None  # guaranteed by freqtrade_available

    # Recovery after an outage
    if state.ft_failures >= cfg.ft_failure_alert_after:
        actions.append(event(EVENT_FT_RECOVERED, failures=state.ft_failures))
        actions.append(
            alert(
                "Guard: Freqtrade wieder erreichbar",
                f"Nach {state.ft_failures} Fehlläufen antwortet Freqtrade wieder. Equity {fmt_eur(equity)}.",
                priority="default",
            )
        )
    alerts.pop(EVENT_FT_UNREACHABLE, None)
    new = replace(new, ft_failures=0, last_error=None, last_equity=equity)

    new, actions = _check_daily_loss(new, obs, cfg, equity, actions)
    new, actions = _check_drawdown(new, obs, cfg, equity, actions)
    new, actions = _check_reconcile(new, obs, cfg, actions)
    new, actions = _check_advisor(new, obs, cfg, actions)
    if cfg.cod_enabled:
        actions.append(Action(ACTION_COD_RENEW, {"seconds": cfg.cod_seconds}))
    return new, actions


def _check_ft_unreachable(
    state: GuardState, obs: Observations, cfg: GuardConfig, actions: list[Action]
) -> tuple[GuardState, list[Action]]:
    failures = state.ft_failures + 1
    error = obs.ft_error or "no equity in /balance"
    actions.append(event(EVENT_FT_UNREACHABLE, failures=failures, error=error))
    if failures >= cfg.ft_failure_alert_after and _may_alert(
        state, EVENT_FT_UNREACHABLE, obs.now, cfg.alert_repeat_hours
    ):
        actions.append(
            alert(
                "Guard: Freqtrade nicht erreichbar",
                f"{failures} Fehlläufe in Folge. Letzter Fehler: {error}",
                priority="urgent",
            )
        )
        state.last_alerts[EVENT_FT_UNREACHABLE] = obs.now
    return replace(state, ft_failures=failures, last_error=error), actions


def _check_daily_loss(
    state: GuardState, obs: Observations, cfg: GuardConfig, equity: Decimal, actions: list[Action]
) -> tuple[GuardState, list[Action]]:
    day = day_key_for(obs.now)
    if state.day_key != day or state.day_start_equity is None:
        actions.append(event(EVENT_DAY_START, day=day, day_start_equity=_dec_str(q2(equity))))
        state = replace(state, day_key=day, day_start_equity=equity)
    start = state.day_start_equity
    assert start is not None
    change = pct_change(start, equity)
    if change <= -cfg.daily_loss_pct and state.daily_loss_day != day:
        details = {
            "day": day,
            "day_start_equity": _dec_str(q2(start)),
            "equity": _dec_str(q2(equity)),
            "loss_pct": _dec_str(q2(-change)),
            "limit_pct": _dec_str(cfg.daily_loss_pct),
        }
        actions.append(Action(ACTION_STOPENTRY, {"reason": EVENT_DAILY_LOSS}))
        actions.append(
            alert(
                "Guard: Tagesverlust-Limit erreicht",
                f"Equity {fmt_eur(equity)} liegt {q2(-change)} % unter dem Tagesstart "
                f"({fmt_eur(start)}). Limit {cfg.daily_loss_pct} %. stopentry gesetzt, "
                "der Bot eröffnet heute keine neuen Positionen.",
                priority="high",
            )
        )
        actions.append(event(EVENT_DAILY_LOSS, **details))
        state = replace(state, daily_loss_day=day)
    return state, actions


def _check_drawdown(
    state: GuardState, obs: Observations, cfg: GuardConfig, equity: Decimal, actions: list[Action]
) -> tuple[GuardState, list[Action]]:
    peak = state.peak_equity
    if peak is None or equity > peak:
        peak = equity
    state = replace(state, peak_equity=peak)
    dd = drawdown_pct(peak, equity)

    if obs.lock_exists:
        # Kill switch active: keep the bot from entering, whatever anyone sent via /start.
        actions.append(Action(ACTION_STOPENTRY, {"reason": "killswitch_lock"}))
        return state, actions

    if dd >= cfg.max_drawdown_pct:
        content = lock_content(obs.now, equity, peak, EVENT_KILLSWITCH)
        actions.append(Action(ACTION_FORCEEXIT, {"reason": EVENT_KILLSWITCH, "tradeid": "all"}))
        actions.append(Action(ACTION_STOPENTRY, {"reason": EVENT_KILLSWITCH}))
        actions.append(Action(ACTION_WRITE_LOCK, {"content": content}))
        actions.append(
            alert(
                "Guard: KILL-SWITCH ausgelöst",
                f"Drawdown {q2(dd)} % vom Hoch ({fmt_eur(peak)}), Equity {fmt_eur(equity)}, "
                f"Limit {cfg.max_drawdown_pct} %. Alle Positionen werden geschlossen, "
                "stopentry gesetzt, killswitch.lock geschrieben. Zurücksetzen nur mit "
                "'btctrader-guard reset --confirm'.",
                priority="urgent",
            )
        )
        actions.append(
            event(
                EVENT_KILLSWITCH,
                equity=_dec_str(q2(equity)),
                peak_equity=_dec_str(q2(peak)),
                drawdown_pct=_dec_str(q2(dd)),
                limit_pct=_dec_str(cfg.max_drawdown_pct),
                open_trades=obs.open_trades,
            )
        )
        state = replace(state, killswitch_at=obs.now)
    return state, actions


def _check_reconcile(
    state: GuardState, obs: Observations, cfg: GuardConfig, actions: list[Action]
) -> tuple[GuardState, list[Action]]:
    """Check 4: only live (``dry_run`` False) and only when exchange balances were fetched."""
    if obs.dry_run is not False or not obs.exchange_available:
        return replace(state, reconcile_mismatches=0), actions
    if obs.ft_eur is None or obs.ft_btc is None:
        return replace(state, reconcile_mismatches=0), actions
    assert obs.exchange_eur is not None and obs.exchange_btc is not None
    diff = reconcile_diff_eur(obs.ft_eur, obs.ft_btc, obs.exchange_eur, obs.exchange_btc, obs.btc_price)
    mismatch = diff is None or diff > cfg.reconcile_tol_eur
    if not mismatch:
        return replace(state, reconcile_mismatches=0), actions

    runs = state.reconcile_mismatches + 1
    state = replace(state, reconcile_mismatches=runs)
    if runs < RECONCILE_RUNS_REQUIRED:
        return state, actions

    actions.append(Action(ACTION_STOPENTRY, {"reason": EVENT_RECONCILE_MISMATCH}))
    if runs == RECONCILE_RUNS_REQUIRED or _may_alert(
        state, EVENT_RECONCILE_MISMATCH, obs.now, cfg.alert_repeat_hours
    ):
        diff_text = "nicht bewertbar (kein BTC-Kurs)" if diff is None else fmt_eur(diff)
        actions.append(
            alert(
                "Guard: Bilanz weicht ab",
                f"Börse vs. Freqtrade: Abweichung {diff_text} (Toleranz {fmt_eur(cfg.reconcile_tol_eur)}) "
                f"an {runs} Läufen in Folge. Börse EUR {q2(obs.exchange_eur)} / BTC {obs.exchange_btc}, "
                f"Freqtrade EUR {q2(obs.ft_eur)} / BTC {obs.ft_btc}. stopentry gesetzt.",
                priority="high",
            )
        )
        actions.append(
            event(
                EVENT_RECONCILE_MISMATCH,
                runs=runs,
                diff_eur=_dec_str(q2(diff)) if diff is not None else None,
                tolerance_eur=_dec_str(cfg.reconcile_tol_eur),
                exchange_eur=_dec_str(obs.exchange_eur),
                exchange_btc=_dec_str(obs.exchange_btc),
                ft_eur=_dec_str(obs.ft_eur),
                ft_btc=_dec_str(obs.ft_btc),
                btc_price=_dec_str(obs.btc_price),
            )
        )
        state.last_alerts[EVENT_RECONCILE_MISMATCH] = obs.now
    return state, actions


def _check_advisor(
    state: GuardState, obs: Observations, cfg: GuardConfig, actions: list[Action]
) -> tuple[GuardState, list[Action]]:
    """Check 5: only in gate mode; alert rate-limited to one per ``alert_repeat_hours``."""
    if cfg.advisor_mode != "gate":
        return state, actions
    if not advisor_is_stale(obs.advisor_created_at, obs.now, cfg.advisor_interval_hours):
        return state, actions
    if not _may_alert(state, EVENT_ADVISOR_STALE, obs.now, cfg.alert_repeat_hours):
        return state, actions
    if obs.advisor_created_at is None:
        age_text = "decision.json fehlt"
    else:
        hours = (obs.now - obs.advisor_created_at).total_seconds() / 3600
        age_text = f"decision.json ist {hours:.1f} h alt"
    limit_h = cfg.advisor_interval_hours * ADVISOR_STALE_FACTOR
    actions.append(
        alert(
            "Guard: Advisor-Entscheidung veraltet",
            f"{age_text} (Grenze {limit_h:g} h). ADVISOR_MODE=gate, die Strategie handelt "
            "ohne gültiges Gate wie BtcTrend. Prüfe den vLLM-Host und den Advisor-Timer.",
            priority="default",
        )
    )
    actions.append(
        event(
            EVENT_ADVISOR_STALE,
            created_at=iso_utc(obs.advisor_created_at) if obs.advisor_created_at else None,
            limit_hours=limit_h,
        )
    )
    state.last_alerts[EVENT_ADVISOR_STALE] = obs.now
    return state, actions


def reset_state_after_unlock(state: GuardState) -> GuardState:
    """State after ``reset --confirm``: peak and counters start fresh, so the next run
    does not re-trigger the kill switch immediately."""
    return replace(
        state,
        peak_equity=None,
        killswitch_at=None,
        reconcile_mismatches=0,
    )
