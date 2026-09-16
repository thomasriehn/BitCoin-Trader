"""CLI: bind parsing and uvicorn invocation (uvicorn itself is stubbed)."""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest

from btctrader.common.config import ConfigError
from btctrader.dashboard import cli


@pytest.mark.parametrize(
    ("bind", "expected"),
    [("127.0.0.1:8090", ("127.0.0.1", 8090)), ("0.0.0.0:80", ("0.0.0.0", 80)), ("[::1]:8090", ("::1", 8090))],
)
def test_parse_bind(bind: str, expected: tuple[str, int]) -> None:
    assert cli.parse_bind(bind) == expected


@pytest.mark.parametrize("bind", ["8090", "host:", ":8090", "host:abc", "host:70000"])
def test_parse_bind_rejects(bind: str) -> None:
    with pytest.raises(ConfigError):
        cli.parse_bind(bind)


def test_main_runs_uvicorn_on_dashboard_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **kw: calls.append({"app": app, **kw})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setenv("DASHBOARD_BIND", "127.0.0.1:9999")
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    assert cli.main([]) == 0
    assert calls == [
        {
            "app": "btctrader.dashboard.app:app",
            "host": "127.0.0.1",
            "port": 9999,
            "log_level": "info",
            "access_log": False,
            "proxy_headers": False,
        }
    ]
    assert cli.main(["--bind", "10.0.0.1:8091"]) == 0
    assert calls[-1]["host"] == "10.0.0.1" and calls[-1]["port"] == 8091


def test_main_reports_bad_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_BIND", "nonsense")
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    assert cli.main([]) == 2


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("nas", False),
    ],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert cli.is_loopback(host) is loopback


def test_main_warns_when_bound_to_a_non_loopback_address(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    caplog.set_level(logging.WARNING, logger="btctrader.dashboard")
    assert cli.main(["--bind", "0.0.0.0:8091"]) == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no login" in warnings[0].getMessage() and "tailscale" in warnings[0].getMessage()
    caplog.clear()
    assert cli.main(["--bind", "127.0.0.1:8091"]) == 0
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
