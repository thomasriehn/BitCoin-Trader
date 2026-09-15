"""Alerts via Telegram and ntfy. Both channels are optional; sending never raises."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from btctrader.common.config import Settings

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_LEN = 4096
DEFAULT_TIMEOUT = 10.0

# ntfy priorities: 1=min, 2=low, 3=default, 4=high, 5=max/urgent
_NTFY_PRIORITY = {
    "min": "1",
    "low": "2",
    "default": "3",
    "high": "4",
    "max": "5",
    "urgent": "5",
}


def _header_value(text: str) -> str:
    """Encode a header value so non-latin-1 text survives (RFC 2047, supported by ntfy)."""
    one_line = " ".join(text.split())
    try:
        one_line.encode("latin-1")
        return one_line
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(one_line.encode("utf-8")).decode("ascii") + "?="


class Alerter:
    """Send a short alert to every configured channel; report which ones succeeded."""

    def __init__(
        self,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        ntfy_url: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self.telegram_bot_token = telegram_bot_token.strip()
        self.telegram_chat_id = telegram_chat_id.strip()
        self.ntfy_url = ntfy_url.strip()
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    @property
    def channels(self) -> list[str]:
        """Configured channel names."""
        names: list[str] = []
        if self.telegram_bot_token and self.telegram_chat_id:
            names.append("telegram")
        if self.ntfy_url:
            names.append("ntfy")
        return names

    def send(self, title: str, message: str, priority: str = "default") -> list[str]:
        """Send to all configured channels. Returns the channels that succeeded, never raises."""
        ok: list[str] = []
        if not self.channels:
            log.info("alert (no channels configured): %s: %s", title, message)
            return ok
        if "telegram" in self.channels:
            try:
                if self._send_telegram(title, message):
                    ok.append("telegram")
            except Exception as exc:  # noqa: BLE001 - alerts must never take the caller down
                log.warning("telegram alert failed: %s", exc)
        if "ntfy" in self.channels:
            try:
                if self._send_ntfy(title, message, priority):
                    ok.append("ntfy")
            except Exception as exc:  # noqa: BLE001
                log.warning("ntfy alert failed: %s", exc)
        return ok

    def _send_telegram(self, title: str, message: str) -> bool:
        text = f"{title}\n{message}" if title else message
        text = text[:TELEGRAM_MAX_LEN]
        url = f"{TELEGRAM_API}/bot{self.telegram_bot_token}/sendMessage"
        resp = self._client.post(
            url,
            json={"chat_id": self.telegram_chat_id, "text": text, "disable_web_page_preview": True},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning("telegram alert failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("ok") is False:
            log.warning("telegram alert rejected: %s", body.get("description"))
            return False
        return True

    def _send_ntfy(self, title: str, message: str, priority: str) -> bool:
        headers = {
            "Title": _header_value(title) if title else "btctrader",
            "Priority": _NTFY_PRIORITY.get(priority.lower(), "3"),
            "Content-Type": "text/plain; charset=utf-8",
        }
        resp = self._client.post(
            self.ntfy_url, content=message.encode("utf-8"), headers=headers, timeout=DEFAULT_TIMEOUT
        )
        if resp.status_code >= 400:
            log.warning("ntfy alert failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        return True


def alerter_from_settings(settings: Settings) -> Alerter:
    return Alerter(settings.telegram_bot_token, settings.telegram_chat_id, settings.ntfy_url)
