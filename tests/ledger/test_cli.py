"""CLI subcommands against a temporary ledger and fixture Freqtrade DB."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from btctrader.common.db import connect
from btctrader.ledger.cli import _parse_since, main
from tests.ledger.conftest import ACCOUNT, make_fill, store
from tests.ledger.ft_fixture import create_ft_db

DAY_MS = 86_400_000
FT = "http://127.0.0.1:8080/api/v1"
BITVAVO = "https://api.bitvavo.com/v2"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ft_db = create_ft_db(tmp_path / "tradesv3.dryrun.sqlite")
    monkeypatch.setenv("LEDGER_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("LEDGER_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("LEDGER_SOURCE", "freqtrade-db")
    monkeypatch.setenv("LEDGER_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("FT_DB_PATH", str(ft_db))
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    return tmp_path


def test_sync_rebuild_export_report_show(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "5 Fills gelesen, 5 neu" in out and "FIFO: 3 Lots, 2 Veräußerungen" in out

    assert main(["rebuild"]) == 0
    assert "FIFO neu aufgebaut: 3 Lots" in capsys.readouterr().out

    assert main(["export", "--month", "2026-03"]) == 0
    out = capsys.readouterr().out
    assert "2026-03-bitvavo-fills.csv (2 Fills)" in out
    assert (env / "exports" / "2026-03-bitvavo-fills.csv.sha256").exists()

    assert main(["export", "--all"]) == 0
    assert (env / "exports" / "2026-05-bitvavo-fills.csv").exists()

    assert main(["report", "--year", "2026"]) == 0
    out = capsys.readouterr().out
    assert "steuer-2026.csv" in out and "Gesamtgewinn § 23 EStG: 29.92 EUR" in out
    assert (env / "reports" / "steuer-2026.csv").exists()

    assert main(["show", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "Offene Lots: 2, Bestand 0.014 BTC" in out and "Hash-Kette: intakt" in out


def test_rebuild_reports_broken_chain(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync"]) == 0
    conn = connect(env / "ledger.sqlite")
    with conn:
        conn.execute("UPDATE fills SET amount_btc = '9' WHERE exchange_trade_id = 'ft-dry_run_buy_1'")
    conn.close()
    capsys.readouterr()
    assert main(["rebuild"]) == 1
    assert "HASH-KETTE DEFEKT" in capsys.readouterr().out
    assert main(["sync"]) == 1


def test_snapshot_requires_benchmark_start(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["snapshot"]) == 1
    assert "BENCHMARK_START" in capsys.readouterr().err


def test_export_month_argument_validation(env: Path) -> None:
    with pytest.raises(SystemExit):
        main(["export", "--month", "2026-13"])


def test_since_is_parsed_as_utc_and_validated(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        assert _parse_since("2026-01-01") == 1_767_225_600_000  # midnight UTC, not local time
        assert _parse_since("2026-01-01T00:00:00Z") == 1_767_225_600_000
        assert _parse_since("2026-01-01T01:00:00+01:00") == 1_767_225_600_000
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    with pytest.raises(SystemExit):  # argparse error, not a ValueError traceback
        main(["sync", "--since", "not-a-date"])


def test_sqlite_errors_are_reported_not_raised(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LEDGER_DB_PATH", str(env))  # a directory cannot be opened as a database
    assert main(["show"]) == 1
    assert "Fehler:" in capsys.readouterr().err


# -- daily (the command the timer runs) --------------------------------------------------


def _candles(request: httpx.Request) -> httpx.Response:
    """Daily candles for the requested window, newest first like the live API."""
    params = request.url.params
    start, end = int(params["start"]), int(params["end"])
    rows: list[list[Any]] = []
    t = start - start % DAY_MS
    while t <= end:
        rows.append([t, "50000", "51000", "49000", "50000", "10"])
        t += DAY_MS
    return httpx.Response(200, json=list(reversed(rows))[:1440])


def _balance_payload() -> dict[str, Any]:
    return {
        "currencies": [
            {"currency": "EUR", "free": 500.5, "balance": 600.25, "used": 99.75, "est_stake": 600.25},
            {"currency": "BTC", "free": 0.01, "balance": 0.012, "used": 0.002, "est_stake": 600.0},
        ],
        "total": 1200.25,
        "stake": "EUR",
    }


@pytest.fixture
def daily_env(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    today = datetime.now(tz=UTC).date()
    monkeypatch.setenv("FT_API_USER", "u")
    monkeypatch.setenv("FT_API_PASS", "p")
    monkeypatch.setenv("BENCHMARK_START", (today - timedelta(days=10)).isoformat())
    return env


def _mock_daily(router: respx.MockRouter, balance_status: int = 200) -> None:
    router.post(f"{FT}/token/login").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    router.get(f"{FT}/balance").mock(return_value=httpx.Response(balance_status, json=_balance_payload()))
    router.get(f"{FT}/show_config").mock(return_value=httpx.Response(200, json={"dry_run": True}))
    router.get(f"{BITVAVO}/BTC-EUR/candles").mock(side_effect=_candles)
    router.get(f"{BITVAVO}/ticker/price").mock(
        return_value=httpx.Response(200, json={"market": "BTC-EUR", "price": "50000"})
    )


@pytest.mark.respx(assert_all_called=False)
def test_daily_runs_sync_snapshot_export(
    daily_env: Path, respx_mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_daily(respx_mock)
    assert main(["daily"]) == 0
    out = capsys.readouterr().out
    assert "5 Fills gelesen, 5 neu" in out and "snapshot" in out
    conn = connect(daily_env / "ledger.sqlite")
    rows = conn.execute("SELECT * FROM equity_daily").fetchall()
    conn.close()
    assert len(rows) == 1
    today = datetime.now(tz=UTC).date()
    assert rows[0]["date_utc"] == today.isoformat()
    assert rows[0]["equity_eur"] == "1200.25"  # 600.25 + 0.012 x 50000
    assert rows[0]["btc_balance"] == "0.012" and rows[0]["dry_run"] == 1
    assert rows[0]["fees_cum_eur"] == "3.03"  # 1 + 0.42 + 0.66 + 0.6750075 + 0.276 (dry-run fee estimates)
    assert rows[0]["realized_gain_ytd_eur"] == ("29.92" if today.year == 2026 else "0")
    exports = daily_env / "exports"
    assert (exports / f"{today.year:04d}-{today.month:02d}-bitvavo-fills.csv.sha256").exists()
    assert len(list(exports.glob("*.csv"))) == 2  # previous and current month


@pytest.mark.respx(assert_all_called=False)
def test_daily_snapshot_failure_keeps_going_and_exits_1(
    daily_env: Path, respx_mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_daily(respx_mock, balance_status=503)
    assert main(["daily"]) == 1
    captured = capsys.readouterr()
    assert "5 Fills gelesen" in captured.out
    assert "Fehler (snapshot)" in captured.err and "503" in captured.err
    conn = connect(daily_env / "ledger.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM equity_daily").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 5  # sync still happened
    conn.close()
    assert len(list((daily_env / "exports").glob("*.csv"))) == 2  # export still ran


@pytest.mark.respx(assert_all_called=False)
def test_daily_fifo_error_still_writes_snapshot_and_export(
    daily_env: Path, respx_mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sell without lots must not stop the equity history; it is reported with exit code 1."""
    _mock_daily(respx_mock)
    conn = connect(daily_env / "ledger.sqlite")
    store(conn, [make_fill("stray-sell", "2026-01-05", "sell", "0.001", "40000")])
    conn.close()
    assert main(["daily"]) == 1
    captured = capsys.readouterr()
    assert "FEHLER FIFO" in captured.err and "exceeds lots held" in captured.err
    conn = connect(daily_env / "ledger.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM equity_daily").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM sync_state WHERE key = 'fifo_error'").fetchone() is not None
    conn.close()
    assert len(list((daily_env / "exports").glob("*.csv"))) == 2
