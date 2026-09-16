"""``btctrader-guard`` command line: check, heartbeat, reset --confirm, status.

This module does all the I/O around the pure state machine in ``guard.py``:
it collects observations (Freqtrade API, optional ccxt balance, lock file,
``decision.json``), runs ``evaluate`` and executes the returned actions
(Freqtrade ``stopentry``/``forceexit``, lock file, alerts, ``events.jsonl``,
optional ``cancelOrdersAfter``).

Files in ``GUARD_DIR``: ``state.json``, ``killswitch.lock``, ``events.jsonl`` and
``.run.lock`` (an ``flock`` that serialises ``check`` and ``reset`` runs).

Addition to the contract: the optional dead man's switch (``GUARD_COD_ENABLED``)
needs a key with trade permission. It is read from ``GUARD_TRADE_API_KEY`` and
``GUARD_TRADE_API_SECRET``, never from the read-only key. Because
``btctrader.env`` is the ``EnvironmentFile`` of every service (dashboard,
advisor, ledger, both Freqtrade bots), a trade key must not live there: put it
in a dedicated file (``GUARD_TRADE_ENV_FILE``, default
``/etc/freqtrade/secrets-guard.env``, root:freqtrade 0640) that only the guard
reads. Precedence: process environment, then the dedicated file, then (with a
warning) ``btctrader.env``.

Equity plausibility: Freqtrade values the BTC wallet with its ticker cache and
reports ``est_stake`` 0 when the ticker is missing or the exchange call failed,
which would look like a crash and trip the kill switch. When BTC is held but not
valued, the guard values it with the public Bitvavo ticker instead; if that fails
too, the run counts as "Freqtrade unavailable" and no limit is evaluated.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import httpx

from btctrader.common import bitvavo_public
from btctrader.common.alerts import Alerter, alerter_from_settings
from btctrader.common.config import (
    DEFAULT_ENV_FILE,
    ENV_FILE_VAR,
    ConfigError,
    Settings,
    load_settings,
    parse_env_file,
)
from btctrader.common.db import iso_utc, parse_iso
from btctrader.common.ftapi import FreqtradeError, client_from_settings
from btctrader.common.jsonl import append_jsonl, atomic_write_json, read_json, read_jsonl_tail
from btctrader.common.log import setup_logging
from btctrader.guard import guard
from btctrader.guard.guard import Action, GuardConfig, GuardState, Observations
from btctrader.guard.heartbeat import run_heartbeat

log = logging.getLogger("btctrader.guard")

STATE_FILE = "state.json"
LOCK_FILE = "killswitch.lock"
EVENTS_FILE = "events.jsonl"
RUN_LOCK_FILE = ".run.lock"
DECISION_FILE = "decision.json"

TRADE_KEY_VAR = "GUARD_TRADE_API_KEY"
TRADE_SECRET_VAR = "GUARD_TRADE_API_SECRET"
TRADE_ENV_FILE_VAR = "GUARD_TRADE_ENV_FILE"
DEFAULT_TRADE_ENV_FILE = Path("/etc/freqtrade/secrets-guard.env")

ACTION_FAILED_ALERT_PREFIX = "action_failed:"
"""``last_alerts`` key prefix for rate-limited alerts about failed actions (per action kind)."""

COD_MIN_SECONDS = 10
COD_MAX_SECONDS = 300

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# -- protocols for injected dependencies --------------------------------------------


class FreqtradeApi(Protocol):
    """The part of ``FreqtradeClient`` the guard uses."""

    def health(self) -> dict[str, Any]: ...
    def balance(self) -> dict[str, Any]: ...
    def status(self) -> list[dict[str, Any]]: ...
    def show_config(self) -> dict[str, Any]: ...
    def stopentry(self) -> dict[str, Any]: ...
    def forceexit(self, tradeid: str = "all", ordertype: str | None = None) -> dict[str, Any]: ...


class BalanceExchange(Protocol):
    def fetch_balance(self, params: Any = None) -> dict[str, Any]: ...


class CodExchange(Protocol):
    def cancel_all_orders_after(self, timeout: int, params: Any = None) -> dict[str, Any]: ...


ExchangeFactory = Callable[[str, str, int], Any]
"""``(api_key, api_secret, operator_id) -> ccxt-like exchange``."""


def make_exchange(api_key: str, api_secret: str, operator_id: int) -> Any:
    """Create a ``ccxt.bitvavo`` instance (same options as the ledger)."""
    import ccxt  # imported lazily: heavy and not needed in dry-run

    return ccxt.bitvavo(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"operatorId": int(operator_id)},
        }
    )


def _trade_key_from_file(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "", ""
    try:
        values = parse_env_file(path)
    except OSError as exc:
        log.warning("cannot read env file %s: %s", path, exc)
        return "", ""
    return values.get(TRADE_KEY_VAR, "").strip(), values.get(TRADE_SECRET_VAR, "").strip()


def trade_key_from_env(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Read ``GUARD_TRADE_API_KEY``/``GUARD_TRADE_API_SECRET``.

    ``load_settings`` only knows the contract variables, so this addition is read here.
    Precedence: process environment, then the dedicated guard secrets file
    (``GUARD_TRADE_ENV_FILE``, default ``/etc/freqtrade/secrets-guard.env``), then the
    shared btctrader env file as a fallback with a warning (that file reaches every
    service's environment, a trade key does not belong there).
    """
    base: Mapping[str, str] = os.environ if env is None else env
    key, secret = base.get(TRADE_KEY_VAR, "").strip(), base.get(TRADE_SECRET_VAR, "").strip()
    if key and secret:
        return key, secret

    dedicated = base.get(TRADE_ENV_FILE_VAR, "").strip()
    file_key, file_secret = _trade_key_from_file(Path(dedicated) if dedicated else DEFAULT_TRADE_ENV_FILE)
    key, secret = key or file_key, secret or file_secret
    if key and secret:
        return key, secret

    env_file = base.get(ENV_FILE_VAR, "").strip()
    shared = Path(env_file) if env_file else DEFAULT_ENV_FILE
    file_key, file_secret = _trade_key_from_file(shared)
    if file_key or file_secret:
        log.warning(
            "trade key read from %s: move %s/%s to %s, the shared env file is loaded by every service",
            shared,
            TRADE_KEY_VAR,
            TRADE_SECRET_VAR,
            DEFAULT_TRADE_ENV_FILE,
        )
    return key or file_key, secret or file_secret


# -- files ---------------------------------------------------------------------------


def state_path(guard_dir: Path) -> Path:
    return guard_dir / STATE_FILE


def lock_path(guard_dir: Path) -> Path:
    return guard_dir / LOCK_FILE


def events_path(guard_dir: Path) -> Path:
    return guard_dir / EVENTS_FILE


def load_state(guard_dir: Path) -> GuardState:
    return GuardState.from_dict(read_json(state_path(guard_dir)))


def save_state(guard_dir: Path, state: GuardState) -> None:
    atomic_write_json(state_path(guard_dir), state.to_dict())


def write_event(guard_dir: Path, name: str, details: dict[str, Any], now: datetime) -> None:
    append_jsonl(events_path(guard_dir), {"ts": iso_utc(now), "event": name, "details": details})


@contextmanager
def run_lock(guard_dir: Path) -> Iterator[None]:
    """Exclusive ``flock`` on ``GUARD_DIR/.run.lock`` so a minutely ``check`` and a manual
    ``reset`` never interleave (read state, unlink lock, save stale state). Blocking: a
    ``check`` holds it for a few seconds at most."""
    guard_dir.mkdir(parents=True, exist_ok=True)
    with open(guard_dir / RUN_LOCK_FILE, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# -- observations --------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def balances_from_ft(balance: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal | None]:
    """(EUR, BTC, BTC price) from ``/balance``: ``currencies[].balance`` and ``est_stake``.

    The price is None when BTC is not held or Freqtrade could not value it
    (``est_stake`` 0 or missing: ticker unavailable). A price of 0 is never returned.
    """
    eur = Decimal(0)
    btc = Decimal(0)
    price: Decimal | None = None
    for entry in balance.get("currencies") or []:
        if not isinstance(entry, dict) or entry.get("is_position"):
            continue
        currency = str(entry.get("currency", "")).upper()
        amount = _dec(entry.get("balance")) or Decimal(0)
        if currency == "EUR":
            eur += amount
        elif currency == "BTC":
            btc += amount
            est = _dec(entry.get("est_stake"))
            if est is not None and est > 0 and amount > 0:
                price = est / amount
    return eur, btc, price


def bot_state_from_config(config: dict[str, Any]) -> str | None:
    """``show_config.state`` (``running`` / ``paused`` / ``stopped``) lower-cased, None when absent."""
    raw = config.get("state")
    return raw.strip().lower() or None if isinstance(raw, str) else None


def advisor_created_at(advisor_dir: Path) -> datetime | None:
    data = read_json(advisor_dir / DECISION_FILE)
    if not data:
        return None
    raw = data.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return parse_iso(raw)
    except ValueError:
        return None


def collect_observations(
    settings: Settings,
    ft: FreqtradeApi,
    *,
    now: datetime,
    guard_dir: Path,
    exchange_factory: ExchangeFactory = make_exchange,
    price_fetcher: Callable[[], Decimal] | None = None,
) -> Observations:
    """Query Freqtrade (and, live only, the exchange) and gather the run's observations."""
    lock_exists = lock_path(guard_dir).exists()
    created_at = advisor_created_at(settings.advisor_dir)

    try:
        ft.health()
        balance = ft.balance()
        open_trades = len(ft.status())
        config = ft.show_config()
    except FreqtradeError as exc:
        log.warning("freqtrade unreachable: %s", exc)
        return Observations(
            now=now, ft_ok=False, ft_error=str(exc), lock_exists=lock_exists, advisor_created_at=created_at
        )

    equity = _dec(balance.get("total"))
    if equity is None:
        return Observations(
            now=now,
            ft_ok=False,
            ft_error="/balance has no numeric 'total'",
            lock_exists=lock_exists,
            advisor_created_at=created_at,
        )
    ft_eur, ft_btc, price = balances_from_ft(balance)
    fetch = price_fetcher or bitvavo_public.fetch_ticker_price

    if ft_btc > 0 and price is None:
        # Freqtrade holds BTC but valued it at 0 (ticker missing or exchange error in
        # its rate lookup). Taking ``total`` at face value would look like a crash and
        # trip the kill switch. Value the BTC with the public ticker instead.
        try:
            price = fetch()
        except Exception as exc:  # noqa: BLE001 - any ticker failure means: no valid equity
            log.warning("BTC balance without est_stake in /balance and no ticker price: %s", exc)
            return Observations(
                now=now,
                ft_ok=False,
                ft_error="BTC balance without est_stake in /balance (Freqtrade ticker unavailable) "
                f"and public ticker failed: {exc}",
                open_trades=open_trades,
                lock_exists=lock_exists,
                advisor_created_at=created_at,
            )
        corrected = equity + ft_btc * price
        log.warning(
            "BTC %s without est_stake in /balance; equity corrected from %s to %s with ticker %s",
            ft_btc,
            equity,
            corrected,
            price,
        )
        equity = corrected

    dry_run_raw = config.get("dry_run")
    dry_run = bool(dry_run_raw) if isinstance(dry_run_raw, bool) else None
    bot_state = bot_state_from_config(config)

    exchange_eur: Decimal | None = None
    exchange_btc: Decimal | None = None
    exchange_error = ""
    has_ro_key = bool(settings.bitvavo_api_key_ro and settings.bitvavo_api_secret_ro)
    if dry_run is False and has_ro_key:
        try:
            exchange = exchange_factory(
                settings.bitvavo_api_key_ro, settings.bitvavo_api_secret_ro, settings.bitvavo_operator_id
            )
            data = exchange.fetch_balance({})
            total = data.get("total") or {}
            exchange_eur = _dec(total.get("EUR", 0) or 0) or Decimal(0)
            exchange_btc = _dec(total.get("BTC", 0) or 0) or Decimal(0)
        except Exception as exc:  # noqa: BLE001 - reconciliation is skipped, the rest still runs
            log.warning("exchange balance failed, reconciliation skipped: %s", exc)
            exchange_eur = exchange_btc = None
            exchange_error = f"{type(exc).__name__}: {exc}"
        if exchange_btc is not None and price is None and (ft_btc != exchange_btc):
            try:
                price = fetch()
            except Exception as exc:  # noqa: BLE001
                log.warning("ticker price unavailable: %s", exc)

    return Observations(
        now=now,
        ft_ok=True,
        equity=equity,
        dry_run=dry_run,
        open_trades=open_trades,
        ft_eur=ft_eur,
        ft_btc=ft_btc,
        exchange_eur=exchange_eur,
        exchange_btc=exchange_btc,
        btc_price=price,
        exchange_error=exchange_error,
        bot_state=bot_state,
        lock_exists=lock_exists,
        advisor_created_at=created_at,
    )


# -- action execution ----------------------------------------------------------------


@dataclass(frozen=True)
class FailedAction:
    kind: str
    reason: str | None
    error: str


@dataclass
class ExecutionResult:
    """What ``execute_actions`` did: executed kinds in order, and the failures."""

    done: list[str] = field(default_factory=list)
    failed: list[FailedAction] = field(default_factory=list)

    def failed_kinds(self) -> set[str]:
        return {f.kind for f in self.failed}


def execute_actions(
    actions: Sequence[Action],
    *,
    ft: FreqtradeApi,
    alerter: Alerter,
    guard_dir: Path,
    now: datetime,
    settings: Settings,
    cod_exchange_factory: ExchangeFactory = make_exchange,
    trade_key: tuple[str, str] | None = None,
) -> ExecutionResult:
    """Execute actions ordered by ``guard.ACTION_PRIORITY`` (Freqtrade control and the lock
    file before slow alert HTTP calls). One failing action never stops the rest.

    Failures are logged and returned; all but ``cod_renew`` failures are also written as
    ``action_failed`` events right here (``run_check`` rate-limits the ``cod_renew`` case,
    which would otherwise add a line per minute while the trade key is missing).
    """
    result = ExecutionResult()
    for action in guard.prioritize_actions(list(actions)):
        kind = action.kind
        details = action.details
        try:
            if kind == guard.ACTION_EVENT:
                write_event(guard_dir, str(details["event"]), dict(details.get("details") or {}), now)
            elif kind == guard.ACTION_ALERT:
                channels = alerter.send(
                    str(details["title"]), str(details["message"]), str(details.get("priority", "default"))
                )
                log.info("alert sent via %s: %s", ",".join(channels) or "no channel", details["title"])
            elif kind == guard.ACTION_STOPENTRY:
                ft.stopentry()
                log.info("stopentry sent (%s)", details.get("reason"))
            elif kind == guard.ACTION_FORCEEXIT:
                ordertype = details.get("ordertype")
                response = ft.forceexit(
                    str(details.get("tradeid", "all")), ordertype=str(ordertype) if ordertype else None
                )
                log.warning("forceexit sent (%s, %s): %s", details.get("reason"), ordertype, response)
            elif kind == guard.ACTION_WRITE_LOCK:
                atomic_write_json(lock_path(guard_dir), dict(details["content"]))
                log.warning("killswitch.lock written")
            elif kind == guard.ACTION_COD_RENEW:
                _renew_cod(int(details["seconds"]), settings, cod_exchange_factory, trade_key)
            else:
                log.error("unknown action kind %r", kind)
                continue
            result.done.append(kind)
        except Exception as exc:  # noqa: BLE001 - one failing action must not stop the rest
            reason = details.get("reason")
            error = f"{type(exc).__name__}: {exc}"
            log.error("action %s failed (%s): %s", kind, reason, error)
            result.failed.append(FailedAction(kind, str(reason) if reason is not None else None, error))
            if kind == guard.ACTION_COD_RENEW:
                continue
            try:
                write_event(
                    guard_dir,
                    guard.EVENT_ACTION_FAILED,
                    {"action": kind, "reason": reason, "error": error},
                    now,
                )
            except OSError as io_exc:
                log.error("cannot write event: %s", io_exc)
    return result


def _handle_failures(
    state: GuardState,
    previous: GuardState,
    result: ExecutionResult,
    *,
    cfg: GuardConfig,
    alerter: Alerter,
    guard_dir: Path,
    now: datetime,
) -> GuardState:
    """Fold action failures back into the state:

    * a failed ``stopentry`` must be retried next minute, so ``daily_loss_day`` is
      restored to its previous value (the state machine would otherwise believe the
      limit was handled for the rest of the day);
    * failed critical actions (forceexit, stopentry, write_lock) are alerted with
      priority urgent, rate-limited per kind;
    * a failed ``cod_renew`` (dead man's switch not armed) gets one ``action_failed``
      event plus a high-priority alert, rate-limited, instead of a line per minute.
    """
    if not result.failed:
        return state
    failed_kinds = result.failed_kinds()
    if guard.ACTION_STOPENTRY in failed_kinds:
        state = replace(state, daily_loss_day=previous.daily_loss_day)

    alerts = dict(state.last_alerts)
    for failure in result.failed:
        key = ACTION_FAILED_ALERT_PREFIX + failure.kind
        critical = failure.kind in guard.CRITICAL_ACTIONS
        if not critical and failure.kind != guard.ACTION_COD_RENEW:
            continue
        if not guard.may_alert(state, key, now, cfg.alert_repeat_hours):
            continue
        alerts[key] = now
        state = replace(state, last_alerts=alerts)
        if failure.kind == guard.ACTION_COD_RENEW:
            title = "Guard: Dead-Man's-Switch nicht aktiv"
            message = (
                f"cancelOrdersAfter konnte nicht erneuert werden: {failure.error}. "
                "GUARD_COD_ENABLED ist gesetzt, die Börse hat aber keinen aktiven Countdown. "
                f"Prüfe {TRADE_KEY_VAR}/{TRADE_SECRET_VAR} und GUARD_COD_SECONDS "
                f"({COD_MIN_SECONDS}..{COD_MAX_SECONDS})."
            )
            priority = "high"
            try:
                write_event(
                    guard_dir,
                    guard.EVENT_ACTION_FAILED,
                    {"action": failure.kind, "reason": failure.reason, "error": failure.error},
                    now,
                )
            except OSError as io_exc:
                log.error("cannot write event: %s", io_exc)
        else:
            title = f"Guard: Aktion {failure.kind} fehlgeschlagen"
            message = (
                f"{failure.kind} (Grund {failure.reason or '-'}) ist fehlgeschlagen: {failure.error}. "
                "Der Guard wiederholt die Aktion beim nächsten Lauf. Prüfe Freqtrade "
                "(Status, Login) und das Börsenkonto: Positionen sind eventuell noch offen."
            )
            priority = "urgent"
        try:
            channels = alerter.send(title, message, priority)
            log.info("alert sent via %s: %s", ",".join(channels) or "no channel", title)
        except Exception as exc:  # noqa: BLE001 - alerting must never break the run
            log.error("failure alert could not be sent: %s", exc)
    return state


def _renew_cod(
    seconds: int,
    settings: Settings,
    factory: ExchangeFactory,
    trade_key: tuple[str, str] | None,
) -> None:
    """Renew Bitvavo ``cancelOrdersAfter`` with the trade key (ccxt ``cancel_all_orders_after``)."""
    key, secret = trade_key if trade_key is not None else trade_key_from_env()
    if not (key and secret):
        raise RuntimeError(f"GUARD_COD_ENABLED needs {TRADE_KEY_VAR} and {TRADE_SECRET_VAR}")
    if not (COD_MIN_SECONDS <= seconds <= COD_MAX_SECONDS):
        raise RuntimeError(f"GUARD_COD_SECONDS must be between {COD_MIN_SECONDS} and {COD_MAX_SECONDS}")
    exchange = factory(key, secret, settings.bitvavo_operator_id)
    result = exchange.cancel_all_orders_after(seconds * 1000, {})
    log.info("cancelOrdersAfter renewed for %s s: %s", seconds, result)


# -- subcommands ---------------------------------------------------------------------


def run_check(
    settings: Settings,
    *,
    ft: FreqtradeApi,
    alerter: Alerter,
    now: datetime | None = None,
    exchange_factory: ExchangeFactory = make_exchange,
    cod_exchange_factory: ExchangeFactory = make_exchange,
    trade_key: tuple[str, str] | None = None,
    price_fetcher: Callable[[], Decimal] | None = None,
) -> tuple[GuardState, list[Action]]:
    """One guard run: observe, evaluate, execute, persist. Returns the new state and actions."""
    now = now or datetime.now(UTC)
    guard_dir = settings.guard_dir
    guard_dir.mkdir(parents=True, exist_ok=True)
    cfg = GuardConfig.from_settings(settings)
    with run_lock(guard_dir):
        state = load_state(guard_dir)
        obs = collect_observations(
            settings,
            ft,
            now=now,
            guard_dir=guard_dir,
            exchange_factory=exchange_factory,
            price_fetcher=price_fetcher,
        )
        new_state, actions = guard.evaluate(state, obs, cfg)
        # Persist bookkeeping (day start, peak) before the slow part: a SIGTERM from the
        # unit timeout during an alert must not replay the whole sequence next minute.
        save_state(guard_dir, new_state)
        result = execute_actions(
            actions,
            ft=ft,
            alerter=alerter,
            guard_dir=guard_dir,
            now=now,
            settings=settings,
            cod_exchange_factory=cod_exchange_factory,
            trade_key=trade_key,
        )
        new_state = _handle_failures(
            new_state, state, result, cfg=cfg, alerter=alerter, guard_dir=guard_dir, now=now
        )
        save_state(guard_dir, new_state)
    log.info(
        "check done: equity=%s day_start=%s peak=%s dry_run=%s bot=%s lock=%s actions=%s failed=%s",
        obs.equity,
        new_state.day_start_equity,
        new_state.peak_equity,
        obs.dry_run,
        obs.bot_state,
        obs.lock_exists,
        ",".join(result.done) or "-",
        ",".join(f.kind for f in result.failed) or "-",
    )
    return new_state, actions


def run_reset(settings: Settings, *, confirm: bool, now: datetime | None = None) -> int:
    """Remove ``killswitch.lock`` and reset the peak so the next run starts fresh.

    Runs under the same ``flock`` as ``check`` and saves the reset state before the lock
    is unlinked: if the save fails, the lock stays and nothing re-triggers.
    """
    now = now or datetime.now(UTC)
    guard_dir = settings.guard_dir
    lock = lock_path(guard_dir)
    if not confirm:
        print("Kill-Switch wird nur mit --confirm zurückgesetzt. Nichts geändert.", file=sys.stderr)
        return EXIT_USAGE
    guard_dir.mkdir(parents=True, exist_ok=True)
    with run_lock(guard_dir):
        existed = lock.exists()
        previous = read_json(lock) if existed else None
        state = guard.reset_state_after_unlock(load_state(guard_dir))
        save_state(guard_dir, state)
        if existed:
            lock.unlink()
        write_event(guard_dir, guard.EVENT_KILLSWITCH_RESET, {"lock_existed": existed, "lock": previous}, now)
    if existed:
        print("killswitch.lock entfernt. Der Bot bleibt pausiert, bis Du in Freqtrade /start sendest.")
    else:
        print("Keine killswitch.lock vorhanden. Zustand (Peak, Zähler) trotzdem zurückgesetzt.")
    return EXIT_OK


def status_report(settings: Settings, *, events: int = 10) -> dict[str, Any]:
    guard_dir = settings.guard_dir
    lock = lock_path(guard_dir)
    return {
        "guard_dir": str(guard_dir),
        "state": load_state(guard_dir).to_dict(),
        "killswitch_lock": read_json(lock) if lock.exists() else None,
        "events": read_jsonl_tail(events_path(guard_dir), events),
    }


def run_status(settings: Settings, *, as_json: bool, events: int = 10) -> int:
    report = status_report(settings, events=events)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK
    state = report["state"]
    lock = report["killswitch_lock"]
    print(f"Guard-Verzeichnis: {report['guard_dir']}")
    print(f"Letzter Lauf:      {state.get('last_check') or '-'}")
    print(f"Equity:            {state.get('last_equity') or '-'} EUR")
    print(f"Tagesstart:        {state.get('day_start_equity') or '-'} EUR ({state.get('day_key') or '-'})")
    print(f"Equity-Hoch:       {state.get('peak_equity') or '-'} EUR")
    loss_day = state.get("daily_loss_day")
    print(f"Tagesverlust:      {'ausgelöst am ' + loss_day if loss_day else '-'}")
    print(f"FT-Fehlläufe:      {state.get('ft_failures', 0)}")
    print(f"Bilanz-Abweichung: {state.get('reconcile_mismatches', 0)} Läufe in Folge")
    if lock:
        print(f"Kill-Switch:       AKTIV seit {lock.get('ts')} (Equity {lock.get('equity_eur')} EUR)")
    else:
        print("Kill-Switch:       aus")
    print(f"Letzte Events ({len(report['events'])}):")
    for ev in report["events"]:
        print(f"  {ev.get('ts')}  {ev.get('event')}  {json.dumps(ev.get('details'), ensure_ascii=False)}")
    return EXIT_OK


# -- argparse / main -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btctrader-guard",
        description="Guard: Tagesverlust, Drawdown-Kill-Switch, Bilanzabgleich, Heartbeat",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--guard-dir", type=Path, default=None, help="Default: GUARD_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="alle Prüfungen einmal ausführen (Timer jede Minute)")
    sub.add_parser("heartbeat", help="Freqtrade-Health prüfen und Healthchecks anpingen")

    p_reset = sub.add_parser("reset", help="killswitch.lock entfernen")
    p_reset.add_argument("--confirm", action="store_true", help="ohne --confirm passiert nichts")

    p_status = sub.add_parser("status", help="Zustand, Lock und letzte Events anzeigen")
    p_status.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_status.add_argument("--events", type=int, default=10, help="Anzahl der letzten Events")
    return parser


def _settings_for(args: argparse.Namespace, require: Sequence[str]) -> Settings:
    settings = load_settings(require=require)
    if args.guard_dir is not None:
        settings = replace(settings, guard_dir=args.guard_dir)
    return settings


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("btctrader.guard", args.log_level)
    try:
        if args.command == "check":
            settings = _settings_for(args, ("ft_api_user", "ft_api_pass"))
            with client_from_settings(settings) as ft:
                run_check(settings, ft=ft, alerter=alerter_from_settings(settings))
            return EXIT_OK
        if args.command == "heartbeat":
            settings = _settings_for(args, ("ft_api_user", "ft_api_pass"))
            with client_from_settings(settings) as ft, httpx.Client() as http:
                result = run_heartbeat(
                    ft, healthchecks_url=settings.healthchecks_url, guard_dir=settings.guard_dir, client=http
                )
            return EXIT_OK if result.pinged or result.url is None else EXIT_ERROR
        if args.command == "reset":
            return run_reset(_settings_for(args, ()), confirm=args.confirm)
        if args.command == "status":
            return run_status(_settings_for(args, ()), as_json=args.json, events=args.events)
    except ConfigError as exc:
        log.error("configuration: %s", exc)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero, never a traceback for the timer
        log.exception("guard %s failed: %s", args.command, exc)
        return EXIT_ERROR
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
