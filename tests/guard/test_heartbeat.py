"""Heartbeat: health freshness and lock file decide between the success and the /fail ping."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from btctrader.guard import heartbeat
from btctrader.guard.cli import main
from tests.guard.conftest import T0, FakeFT

HC_URL = "https://hc.example.org/ping/abc-123"
FT_URL = "http://127.0.0.1:8080"


def test_assess_is_pure() -> None:
    fresh = {"last_process_ts": int(T0.timestamp()) - 30}
    assert heartbeat.assess(fresh, lock_exists=False, now=T0) == (True, "last_process vor 30 s")
    assert heartbeat.assess(fresh, lock_exists=True, now=T0)[0] is False
    stale = {"last_process_ts": int(T0.timestamp()) - 91}
    assert heartbeat.assess(stale, lock_exists=False, now=T0)[0] is False
    assert heartbeat.assess(None, lock_exists=False, now=T0)[0] is False
    assert heartbeat.assess({}, lock_exists=False, now=T0)[0] is False
    # ISO string fallback when the epoch field is missing
    iso_only = {"last_process": (T0 - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")}
    assert heartbeat.assess(iso_only, lock_exists=False, now=T0)[0] is True


def test_ping_url() -> None:
    assert heartbeat.ping_url(HC_URL + "/", True) == HC_URL
    assert heartbeat.ping_url(HC_URL, False) == HC_URL + "/fail"


@respx.mock
def test_healthy_pings_success_url(tmp_path: Path) -> None:
    ok = respx.get(HC_URL).mock(return_value=httpx.Response(200, text="OK"))
    fail = respx.get(HC_URL + "/fail").mock(return_value=httpx.Response(200, text="OK"))
    with httpx.Client() as http:
        result = heartbeat.run_heartbeat(
            FakeFT(), healthchecks_url=HC_URL, guard_dir=tmp_path, client=http, now=T0
        )
    assert result.healthy and result.pinged and result.url == HC_URL
    assert ok.call_count == 1 and fail.call_count == 0


@respx.mock
def test_lock_pings_fail_url(tmp_path: Path) -> None:
    ok = respx.get(HC_URL).mock(return_value=httpx.Response(200, text="OK"))
    fail = respx.get(HC_URL + "/fail").mock(return_value=httpx.Response(200, text="OK"))
    (tmp_path / "killswitch.lock").write_text("{}")
    with httpx.Client() as http:
        result = heartbeat.run_heartbeat(
            FakeFT(), healthchecks_url=HC_URL, guard_dir=tmp_path, client=http, now=T0
        )
    assert not result.healthy and result.pinged and result.url == HC_URL + "/fail"
    assert "killswitch.lock" in result.reason
    assert ok.call_count == 0 and fail.call_count == 1


@respx.mock
def test_stale_last_process_and_unreachable_ft_ping_fail_url(tmp_path: Path) -> None:
    fail = respx.get(HC_URL + "/fail").mock(return_value=httpx.Response(200, text="OK"))
    with httpx.Client() as http:
        stale = heartbeat.run_heartbeat(
            FakeFT(last_process_age_s=120), healthchecks_url=HC_URL, guard_dir=tmp_path, client=http, now=T0
        )
        down = heartbeat.run_heartbeat(
            FakeFT(unreachable=True), healthchecks_url=HC_URL, guard_dir=tmp_path, client=http, now=T0
        )
    assert not stale.healthy and "120 s alt" in stale.reason
    assert not down.healthy and "nicht erreichbar" in down.reason
    assert fail.call_count == 2


@respx.mock
def test_ping_transport_error_is_reported_not_raised(tmp_path: Path) -> None:
    respx.get(HC_URL).mock(side_effect=httpx.ConnectError("boom"))
    with httpx.Client() as http:
        result = heartbeat.run_heartbeat(
            FakeFT(), healthchecks_url=HC_URL, guard_dir=tmp_path, client=http, now=T0
        )
    assert result.healthy and not result.pinged


@respx.mock
def test_without_url_only_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="btctrader.guard.heartbeat"):
        result = heartbeat.run_heartbeat(FakeFT(), healthchecks_url="", guard_dir=tmp_path, now=T0)
    assert result.url is None and not result.pinged and result.healthy
    assert "kein HEALTHCHECKS_URL" in caplog.text
    assert not respx.calls  # nothing was sent anywhere


@respx.mock
def test_cli_heartbeat_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FT_API_USER", "bot")
    monkeypatch.setenv("FT_API_PASS", "pw")
    monkeypatch.setenv("FT_API_URL", FT_URL)
    monkeypatch.setenv("GUARD_DIR", str(tmp_path / "guard"))
    monkeypatch.setenv("HEALTHCHECKS_URL", HC_URL)
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    respx.post(f"{FT_URL}/api/v1/token/login").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "refresh_token": "r"})
    )
    respx.get(f"{FT_URL}/api/v1/health").mock(
        return_value=httpx.Response(200, json={"last_process_ts": int(time.time()) - 3, "last_process": None})
    )
    ok = respx.get(HC_URL).mock(return_value=httpx.Response(200, text="OK"))
    fail = respx.get(HC_URL + "/fail").mock(return_value=httpx.Response(200, text="OK"))

    assert main(["heartbeat"]) == 0
    assert ok.call_count == 1 and fail.call_count == 0

    (tmp_path / "guard").mkdir(exist_ok=True)
    (tmp_path / "guard" / "killswitch.lock").write_text("{}")
    assert main(["heartbeat"]) == 0
    assert ok.call_count == 1 and fail.call_count == 1

    # Healthchecks unreachable: exit code 1 so the timer run shows up in journald
    respx.get(HC_URL + "/fail").mock(return_value=httpx.Response(503, text="down"))
    assert main(["heartbeat"]) == 1
