"""Heartbeat: Freqtrade health freshness -> Healthchecks ping (success or ``/fail``).

Rule (docs/KOMPONENTEN.md section 8): ``GET /api/v1/health``; when ``last_process``
is younger than 90 s and no ``killswitch.lock`` exists, ``GET HEALTHCHECKS_URL``,
otherwise ``GET HEALTHCHECKS_URL/fail``. Without ``HEALTHCHECKS_URL`` only a log line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from btctrader.common.db import parse_iso

log = logging.getLogger(__name__)

HEALTH_MAX_AGE_S = 90.0
PING_TIMEOUT_S = 10.0
LOCK_FILE = "killswitch.lock"


class HealthSource(Protocol):
    """The part of ``FreqtradeClient`` the heartbeat needs (lets tests inject a fake)."""

    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HeartbeatResult:
    healthy: bool
    reason: str
    url: str | None
    pinged: bool


def last_process_age_s(health: dict[str, Any], now: datetime) -> float | None:
    """Age of ``last_process`` in seconds, from ``last_process_ts`` (epoch) or the ISO string."""
    ts = health.get("last_process_ts")
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return now.timestamp() - float(ts)
    raw = health.get("last_process")
    if isinstance(raw, str) and raw:
        try:
            return (now - parse_iso(raw)).total_seconds()
        except ValueError:
            return None
    return None


def assess(
    health: dict[str, Any] | None,
    *,
    lock_exists: bool,
    now: datetime,
    max_age_s: float = HEALTH_MAX_AGE_S,
) -> tuple[bool, str]:
    """Pure decision: (healthy, reason)."""
    if lock_exists:
        return False, "killswitch.lock vorhanden"
    if health is None:
        return False, "Freqtrade /health nicht erreichbar"
    age = last_process_age_s(health, now)
    if age is None:
        return False, "last_process fehlt in /health"
    if age > max_age_s:
        return False, f"last_process ist {age:.0f} s alt (Grenze {max_age_s:.0f} s)"
    return True, f"last_process vor {age:.0f} s"


def ping_url(base_url: str, healthy: bool) -> str:
    base = base_url.strip().rstrip("/")
    return base if healthy else f"{base}/fail"


def send_ping(url: str, client: httpx.Client) -> bool:
    """GET the ping URL; returns True on 2xx. Never raises."""
    try:
        resp = client.get(url, timeout=PING_TIMEOUT_S, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("heartbeat ping failed: %s", exc)
        return False
    if resp.status_code >= 300:
        log.warning("heartbeat ping returned HTTP %s", resp.status_code)
        return False
    return True


def run_heartbeat(
    ft: HealthSource,
    *,
    healthchecks_url: str,
    guard_dir: Path,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> HeartbeatResult:
    """Fetch health, decide, ping. Errors from Freqtrade count as unhealthy."""
    now = now or datetime.now(UTC)
    health: dict[str, Any] | None
    try:
        health = ft.health()
    except Exception as exc:  # noqa: BLE001 - any failure means "not healthy", never a crash
        log.warning("freqtrade /health failed: %s", exc)
        health = None
    lock_exists = (guard_dir / LOCK_FILE).exists()
    healthy, reason = assess(health, lock_exists=lock_exists, now=now)

    if not healthchecks_url.strip():
        log.info("heartbeat: %s (%s), kein HEALTHCHECKS_URL, kein Ping", "ok" if healthy else "fail", reason)
        return HeartbeatResult(healthy=healthy, reason=reason, url=None, pinged=False)

    url = ping_url(healthchecks_url, healthy)
    own_client = client is None
    http = client or httpx.Client(timeout=PING_TIMEOUT_S)
    try:
        pinged = send_ping(url, http)
    finally:
        if own_client:
            http.close()
    log.info(
        "heartbeat: %s (%s), ping %s",
        "ok" if healthy else "fail",
        reason,
        "gesendet" if pinged else "FEHLER",
    )
    return HeartbeatResult(healthy=healthy, reason=reason, url=url, pinged=pinged)
