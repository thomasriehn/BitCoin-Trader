"""Freqtrade REST client (verified against Freqtrade 2026.8 ``rpc/api_server``).

Auth flow: ``POST /api/v1/token/login`` with HTTP Basic credentials returns
``{"access_token", "refresh_token"}``. Every other call sends
``Authorization: Bearer <access_token>``; on a 401 the client logs in again
once and retries the request. No other retries (timeouts 10 s).

Endpoint notes from the installed version:
  * ``/ping`` is public (no auth).
  * ``/daily`` takes the query parameter ``timescale`` (number of days).
  * ``/trades`` takes ``limit``, ``offset`` and ``order_by_id``.
  * ``/forceexit`` expects ``{"tradeid": str | int, "ordertype"?: "market" | "limit"}``.
  * ``/stopentry`` is an alias of ``/pause`` (and the deprecated ``/stopbuy``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from btctrader.common.config import Settings

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


class FreqtradeError(Exception):
    """Raised for transport errors and non-2xx responses from the Freqtrade API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FreqtradeClient:
    """Small synchronous client for the Freqtrade REST API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._token: str | None = None

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FreqtradeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth ----------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    def login(self) -> str:
        """Fetch a fresh JWT access token via HTTP Basic auth."""
        try:
            resp = self._client.post(
                self._url("/token/login"),
                auth=(self.username, self.password),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise FreqtradeError(f"login failed: {exc}") from exc
        if resp.status_code != 200:
            raise FreqtradeError(
                f"login failed with HTTP {resp.status_code}: {_short(resp.text)}",
                status_code=resp.status_code,
            )
        try:
            token = resp.json()["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise FreqtradeError("login response has no access_token") from exc
        if not isinstance(token, str) or not token:
            raise FreqtradeError("login response has an empty access_token")
        self._token = token
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        if auth and self._token is None:
            self.login()
        resp = self._send(method, path, params=params, json=json, auth=auth)
        if auth and resp.status_code == 401:
            log.info("freqtrade api returned 401, refreshing token")
            self.login()
            resp = self._send(method, path, params=params, json=json, auth=auth)
        if resp.status_code >= 400:
            raise FreqtradeError(
                f"{method} {path} failed with HTTP {resp.status_code}: {_short(resp.text)}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise FreqtradeError(f"{method} {path}: response is not JSON") from exc

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        auth: bool,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
        try:
            return self._client.request(
                method,
                self._url(path),
                params=params,
                json=json,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise FreqtradeError(f"{method} {path} failed: {exc}") from exc

    # -- info endpoints ------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """``GET /ping`` (public, no auth): ``{"status": "pong"}``."""
        return self._request("GET", "/ping", auth=False)

    def health(self) -> dict[str, Any]:
        """``GET /health``: ``last_process``, ``last_process_ts``, ``bot_start`` ..."""
        return self._request("GET", "/health")

    def balance(self) -> dict[str, Any]:
        """``GET /balance``: ``currencies``, ``total``, ``stake`` ..."""
        return self._request("GET", "/balance")

    def status(self) -> list[dict[str, Any]]:
        """``GET /status``: list of open trades (empty when none)."""
        result = self._request("GET", "/status")
        return result if isinstance(result, list) else []

    def profit(self) -> dict[str, Any]:
        return self._request("GET", "/profit")

    def show_config(self) -> dict[str, Any]:
        return self._request("GET", "/show_config")

    def trades(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """``GET /trades``: ``{"trades": [...], "trades_count", "total_trades", "offset"}``."""
        return self._request("GET", "/trades", params={"limit": limit, "offset": offset})

    def daily(self, days: int = 7) -> dict[str, Any]:
        """``GET /daily?timescale=<days>``."""
        return self._request("GET", "/daily", params={"timescale": days})

    # -- bot control ---------------------------------------------------------------

    def stopentry(self) -> dict[str, Any]:
        """``POST /stopentry``: bot keeps running but opens no new positions."""
        return self._request("POST", "/stopentry")

    def start(self) -> dict[str, Any]:
        return self._request("POST", "/start")

    def stop(self) -> dict[str, Any]:
        return self._request("POST", "/stop")

    def forceexit(self, tradeid: str = "all", ordertype: str | None = None) -> dict[str, Any]:
        """``POST /forceexit`` with ``{"tradeid": ..., "ordertype": ...}``."""
        payload: dict[str, Any] = {"tradeid": tradeid}
        if ordertype is not None:
            payload["ordertype"] = ordertype
        return self._request("POST", "/forceexit", json=payload)


def _short(text: str, limit: int = 200) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def client_from_settings(settings: Settings) -> FreqtradeClient:
    """Create a client from ``Settings`` (FT_API_URL, FT_API_USER, FT_API_PASS)."""
    return FreqtradeClient(settings.ft_api_url, settings.ft_api_user, settings.ft_api_pass)
