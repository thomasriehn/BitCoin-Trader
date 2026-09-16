"""Status endpoint: Freqtrade reachable / unreachable (respx), guard and advisor files."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from freezegun import freeze_time

from btctrader.common.config import Settings
from btctrader.common.jsonl import append_jsonl, atomic_write_json
from btctrader.dashboard import data
from btctrader.dashboard.app import FT_TIMEOUT_S, create_app
from tests.dashboard.conftest import FT_URL, NOW, seed_advisor, seed_guard

API = f"{FT_URL}/api/v1"


def _mock_bot(
    *, state: str = "running", dry_run: bool = True, profit: dict[str, object] | None = None
) -> None:
    respx.post(f"{API}/token/login").mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
    respx.get(f"{API}/health").mock(
        return_value=httpx.Response(
            200, json={"last_process": "2026-09-15T11:59:58+00:00", "last_process_ts": 1789000000}
        )
    )
    respx.get(f"{API}/show_config").mock(
        return_value=httpx.Response(
            200,
            json={
                "dry_run": dry_run,
                "state": state,
                "strategy": "BtcTrend",
                "bot_name": "btc",
                "secret": "x",
            },
        )
    )
    respx.get(f"{API}/profit").mock(
        return_value=httpx.Response(
            200,
            json=profit
            if profit is not None
            else {
                "profit_closed_coin": 12.5,
                "closed_trade_count": 3,
                "trade_count": 4,
                "best_pair": "BTC/EUR",
            },
        )
    )
    respx.get(f"{API}/balance").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1190.0,
                "stake": "EUR",
                "starting_capital": 1000.0,
                "currencies": [
                    {"currency": "EUR", "balance": 500.0, "free": 500.0, "used": 0.0, "est_stake": 500.0},
                    {"currency": "BTC", "balance": 0.01, "free": 0.01, "used": 0.0, "est_stake": 690.0},
                    {"currency": "USDT", "balance": 1.0, "free": 1.0, "used": 0.0, "est_stake": 0.9},
                ],
            },
        )
    )
    respx.get(f"{API}/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"trade_id": 7, "pair": "BTC/EUR", "amount": 0.01, "profit_ratio": 0.02, "open_rate": 67000.0}
            ],
        )
    )


@respx.mock
@freeze_time(NOW)
def test_status_with_reachable_bot(client: TestClient) -> None:
    _mock_bot()
    body = client.get("/api/status").json()
    bot = body["bot"]
    assert bot["reachable"] is True and bot["error"] is None
    assert bot["config"] == {"dry_run": True, "state": "running", "strategy": "BtcTrend", "bot_name": "btc"}
    assert "secret" not in bot["config"]
    assert bot["profit"]["profit_closed_coin"] == 12.5 and bot["profit"]["closed_trade_count"] == 3
    assert bot["balance"]["total"] == 1190.0
    assert [c["currency"] for c in bot["balance"]["currencies"]] == ["EUR", "BTC"]
    assert bot["open_trades"][0]["trade_id"] == 7 and bot["open_trades"][0]["pair"] == "BTC/EUR"
    assert bot["health"]["last_process"].startswith("2026-09-15")

    guard = body["guard"]
    assert guard["state"]["peak_equity"] == "1250.00"
    assert guard["killswitch"] == {"active": False, "content": None}
    assert [e["event"] for e in guard["events"]] == ["daily_loss", "ft_unreachable", "day_start"]

    advisor = body["advisor"]
    assert advisor["decision"]["regime"] == "neutral"
    assert advisor["valid"] is True and advisor["age_s"] == 3178
    assert advisor["last_log"]["error"] == "timeout after 120s"
    assert "raw_response" not in advisor["last_log"]
    assert body["advisor_mode"] == "shadow"
    assert body["generated_at"] == "2026-09-15T12:00:00.000000Z"  # iso_utc: fixed precision

    html = client.get("/").text
    assert "läuft" in html and "BtcTrend" in html and "Neutral" in html


@respx.mock
def test_status_degrades_when_bot_is_down(client: TestClient) -> None:
    respx.post(f"{API}/token/login").mock(side_effect=httpx.ConnectError("connection refused"))
    body = client.get("/api/status").json()
    assert body["bot"]["reachable"] is False
    assert "login failed" in body["bot"]["error"] and "connection refused" in body["bot"]["error"]
    # Guard and advisor data are still served.
    assert body["guard"]["state"]["day_key"] == "2026-09-15"
    assert body["advisor"]["decision"]["decision_id"] == "2026-09-15T11:07:02Z-a1b2c3"
    page = client.get("/")
    assert page.status_code == 200 and "nicht erreichbar" in page.text


@respx.mock
def test_status_degrades_on_http_error(client: TestClient) -> None:
    respx.post(f"{API}/token/login").mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
    respx.get(f"{API}/health").mock(return_value=httpx.Response(500, text="boom"))
    body = client.get("/api/status").json()
    assert body["bot"]["reachable"] is False and "HTTP 500" in body["bot"]["error"]


@respx.mock
@freeze_time(NOW)
def test_killswitch_and_expired_advisor(tmp_path: Path) -> None:
    seed_guard(tmp_path / "guard", killswitch=True)
    seed_advisor(tmp_path / "advisor", valid_until="2026-09-15T11:00:00Z")
    settings = Settings(
        ledger_db_path=tmp_path / "ledger.sqlite",
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
        ft_api_url=FT_URL,
        ft_api_user="bot",
        ft_api_pass="pw",
    )
    _mock_bot(state="paused")
    with TestClient(create_app(settings)) as c:
        body = c.get("/api/status").json()
        assert body["guard"]["killswitch"]["active"] is True
        assert body["guard"]["killswitch"]["content"]["reason"] == "test"
        assert body["advisor"]["valid"] is False
        html = c.get("/").text
    assert "Kill-Switch aktiv" in html and "pausiert" in html and "abgelaufen" in html


# -- malformed files and partial API responses must degrade, never 500 ----------------------


def _bare_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        ledger_db_path=tmp_path / "ledger.sqlite",
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
        **overrides,  # type: ignore[arg-type]
    )


@freeze_time(NOW)
@pytest.mark.parametrize(
    "decision",
    [
        {"schema_version": 1, "regime": "neutral", "confidence": None},
        {"schema_version": 1, "regime": "neutral"},
        {"regime": "risk_off", "confidence": "high", "horizon_days": None, "mode": None},
    ],
    ids=["confidence-null", "confidence-missing", "confidence-string"],
)
def test_malformed_decision_json_does_not_break_the_page(tmp_path: Path, decision: dict[str, object]) -> None:
    atomic_write_json(tmp_path / "advisor" / "decision.json", decision)
    with TestClient(create_app(_bare_settings(tmp_path))) as c:
        for url in ("/", "/partials/tiles"):
            r = c.get(url)
            assert r.status_code == 200, url
            assert "Konfidenz –" in r.text and 'id="advisor"' in r.text
        assert c.get("/api/status").json()["advisor"]["confidence_pct"] is None


@freeze_time(NOW)
def test_events_with_non_dict_details_do_not_break_the_page(tmp_path: Path) -> None:
    events = tmp_path / "guard" / "events.jsonl"
    append_jsonl(events, {"ts": "2026-09-15T01:00:00Z", "event": "x", "details": "plain"})
    append_jsonl(events, {"ts": "2026-09-15T02:00:00Z", "event": "y", "details": [1, 2]})
    append_jsonl(events, {"ts": "2026-09-15T03:00:00Z", "event": "z"})
    append_jsonl(events, {"ts": "2026-09-15T04:00:00Z", "event": "ok", "details": {"n": 1}})
    with TestClient(create_app(_bare_settings(tmp_path))) as c:
        for url in ("/", "/partials/events"):
            r = c.get(url)
            assert r.status_code == 200, url
            assert "raw=plain" in r.text and "raw=[1, 2]" in r.text and "n=1" in r.text
        got = c.get("/api/status").json()["guard"]["events"]
    assert [e["details"] for e in got] == [{"n": 1}, {}, {"raw": "[1, 2]"}, {"raw": "plain"}]


def test_confidence_pct_is_only_derived_from_numbers() -> None:
    assert data.advisor_status.__doc__  # sanity: helper below is what the status uses
    assert data._confidence_pct(0.55) == 55.0
    assert data._confidence_pct("0.5") == 50.0
    assert data._confidence_pct(1) == 100.0
    assert data._confidence_pct(None) is None
    assert data._confidence_pct(True) is None
    assert data._confidence_pct("high") is None
    assert data._confidence_pct("nan") is None
    assert data._confidence_pct([0.5]) is None


@respx.mock
def test_missing_profit_keys_render_without_placeholders(client: TestClient) -> None:
    # Older or partial /profit responses: _pick omits absent keys, the tile must not print "·  Trades".
    _mock_bot(profit={"profit_closed_coin": 1.0})
    r = client.get("/partials/tiles")
    assert r.status_code == 200
    assert "Trades" not in r.text and "läuft" in r.text
    assert "closed_trade_count" not in client.get("/api/status").json()["bot"]["profit"]


@respx.mock
def test_dashboard_ft_client_uses_short_timeout(client: TestClient) -> None:
    _mock_bot()
    assert client.get("/api/status").json()["bot"]["reachable"] is True
    ft = client.app.state.ft_client  # type: ignore[attr-defined]
    assert ft.timeout == FT_TIMEOUT_S == 3.0
