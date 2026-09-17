"""Tests for btctrader.common.ftapi (respx-mocked Freqtrade API)."""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from btctrader.common.config import load_settings
from btctrader.common.ftapi import FreqtradeClient, FreqtradeError, client_from_settings

BASE = "http://127.0.0.1:8080"
LOGIN = f"{BASE}/api/v1/token/login"


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture
def client() -> FreqtradeClient:
    return FreqtradeClient(BASE + "/", "bot", "pw", client=httpx.Client())


@respx.mock
def test_login_uses_basic_auth_and_bearer_token(client: FreqtradeClient) -> None:
    login = respx.post(LOGIN).mock(
        return_value=httpx.Response(200, json={"access_token": "tok1", "refresh_token": "ref1"})
    )
    health = respx.get(f"{BASE}/api/v1/health").mock(
        return_value=httpx.Response(200, json={"last_process": "2026-09-15T12:00:00+00:00"})
    )
    assert client.health()["last_process"].startswith("2026")
    assert login.call_count == 1
    assert login.calls[0].request.headers["Authorization"] == _basic("bot", "pw")
    assert health.calls[0].request.headers["Authorization"] == "Bearer tok1"

    # second call reuses the token, no second login
    client.health()
    assert login.call_count == 1


@respx.mock
def test_relogin_once_on_401(client: FreqtradeClient) -> None:
    tokens = iter(["old", "new"])
    respx.post(LOGIN).mock(
        side_effect=lambda request: httpx.Response(200, json={"access_token": next(tokens)})
    )

    def balance(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"detail": "Invalid token"})
        return httpx.Response(200, json={"total": 1000.0, "currencies": []})

    route = respx.get(f"{BASE}/api/v1/balance").mock(side_effect=balance)
    assert client.balance()["total"] == 1000.0
    assert route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer new"


@respx.mock
def test_persistent_401_raises_after_one_retry(client: FreqtradeClient) -> None:
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    route = respx.get(f"{BASE}/api/v1/profit").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(FreqtradeError) as exc:
        client.profit()
    assert exc.value.status_code == 401
    assert route.call_count == 2


@respx.mock
def test_login_failure_raises(client: FreqtradeClient) -> None:
    respx.post(LOGIN).mock(return_value=httpx.Response(401, json={"detail": "Incorrect username"}))
    with pytest.raises(FreqtradeError) as exc:
        client.status()
    assert exc.value.status_code == 401


@respx.mock
def test_transport_error_raises_without_status(client: FreqtradeClient) -> None:
    respx.post(LOGIN).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FreqtradeError) as exc:
        client.show_config()
    assert exc.value.status_code is None


@respx.mock
def test_ping_is_public(client: FreqtradeClient) -> None:
    login = respx.post(LOGIN)
    route = respx.get(f"{BASE}/api/v1/ping").mock(return_value=httpx.Response(200, json={"status": "pong"}))
    assert client.ping() == {"status": "pong"}
    assert login.call_count == 0
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
def test_endpoint_paths_and_payloads(client: FreqtradeClient) -> None:
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    ok = httpx.Response(200, json={"status": "ok"})
    status = respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(200, json=[]))
    daily = respx.get(f"{BASE}/api/v1/daily").mock(return_value=httpx.Response(200, json={"data": []}))
    trades = respx.get(f"{BASE}/api/v1/trades").mock(return_value=httpx.Response(200, json={"trades": []}))
    stopentry = respx.post(f"{BASE}/api/v1/stopentry").mock(return_value=ok)
    start = respx.post(f"{BASE}/api/v1/start").mock(return_value=ok)
    stop = respx.post(f"{BASE}/api/v1/stop").mock(return_value=ok)
    forceexit = respx.post(f"{BASE}/api/v1/forceexit").mock(
        return_value=httpx.Response(200, json={"result": "Created exit order for trade 1."})
    )
    show_config = respx.get(f"{BASE}/api/v1/show_config").mock(
        return_value=httpx.Response(200, json={"dry_run": True})
    )

    assert client.status() == []
    client.daily(days=30)
    client.trades(limit=10)
    client.stopentry()
    client.start()
    client.stop()
    client.forceexit()
    client.forceexit("7", ordertype="market")
    assert client.show_config()["dry_run"] is True

    assert status.called and stopentry.called and start.called and stop.called and show_config.called
    assert daily.calls[0].request.url.params["timescale"] == "30"
    assert trades.calls[0].request.url.params["limit"] == "10"
    import json

    assert json.loads(forceexit.calls[0].request.content) == {"tradeid": "all"}
    assert json.loads(forceexit.calls[1].request.content) == {"tradeid": "7", "ordertype": "market"}


@respx.mock
def test_http_error_raises_with_status(client: FreqtradeClient) -> None:
    respx.post(LOGIN).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(f"{BASE}/api/v1/forceexit").mock(
        return_value=httpx.Response(502, json={"detail": "trade not found"})
    )
    with pytest.raises(FreqtradeError) as exc:
        client.forceexit("99")
    assert exc.value.status_code == 502
    assert "trade not found" in str(exc.value)


def test_client_from_settings() -> None:
    s = load_settings({"FT_API_URL": "http://10.0.0.1:8081/", "FT_API_USER": "a", "FT_API_PASS": "b"})
    c = client_from_settings(s)
    try:
        assert c.base_url == "http://10.0.0.1:8081"
        assert c.username == "a" and c.password == "b"
        assert c.timeout == 10.0
    finally:
        c.close()
