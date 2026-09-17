"""JSON endpoints against the fixture ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from btctrader.common.config import Settings
from btctrader.common.db import connect
from btctrader.dashboard import data
from btctrader.dashboard.app import create_app
from tests.dashboard.conftest import NOW


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["ledger"] is True
    assert body["time"].endswith("Z")


def test_equity_series_and_summary(client: TestClient) -> None:
    body = client.get("/api/equity").json()
    assert body["days"] == 365 and body["ledger_available"] is True
    s = body["series"]
    assert s["dates"] == ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"]
    assert s["equity"] == [1000.0, 1100.0, 990.0, 1050.0, 1200.0]
    assert s["bh"][-1] == 1300.0 and s["dca"][-1] == 1050.0
    assert s["drawdown"] == [0.0, 0.0, -10.0, -4.55, 0.0]
    assert s["drawdown_bh"] == [0.0, 0.0, -25.0, -16.67, 0.0]
    sm = body["summary"]
    assert sm["rows"] == 5 and sm["from"] == "2026-09-10" and sm["to"] == "2026-09-14"
    assert (
        sm["equity_eur"] == "1200.00"
        and sm["bh_equity_eur"] == "1300.00"
        and sm["dca_equity_eur"] == "1050.00"
    )
    assert sm["vs_bh_eur"] == "-100.00" and sm["vs_bh_pct"] == -7.69
    assert sm["vs_dca_eur"] == "150.00" and sm["vs_dca_pct"] == 14.29
    assert sm["return_pct"] == 20.0
    assert sm["max_drawdown_bot_pct"] == -10.0 and sm["max_drawdown_bh_pct"] == -25.0
    assert sm["btc_price_eur"] == 65000.0 and sm["dry_run"] is True


def test_equity_window_keeps_true_peak(client: TestClient) -> None:
    body = client.get("/api/equity?days=2").json()
    s = body["series"]
    assert s["dates"] == ["2026-09-13", "2026-09-14"]
    # Drawdown is computed on the whole history before cutting the window.
    assert s["drawdown"] == [-4.55, 0.0]
    assert body["summary"]["max_drawdown_bot_pct"] == -4.55
    assert body["summary"]["return_pct"] == 14.29


@pytest.mark.parametrize("query", ["days=0", "days=abc", "days=99999"])
def test_equity_rejects_bad_days(client: TestClient, query: str) -> None:
    assert client.get(f"/api/equity?{query}").status_code == 422


@freeze_time(NOW)
def test_fees(client: TestClient) -> None:
    body = client.get("/api/fees").json()
    assert body["year"] == 2026
    assert body["cum_eur"] == "8.85"  # 2.5 + 0.9 + 2 + 2.25 + 1.05 + 0.15
    assert body["ytd_eur"] == "5.45"  # 2 + 2.25 + 1.05 + 0.15
    assert body["last_30d_eur"] == "0.15"  # only b4 (2026-09-10)
    assert body["fills_total"] == 6
    ms = body["maker_share"]
    assert ms["maker_fills"] == 2 and ms["taker_fills"] == 1 and ms["unknown_fills"] == 3
    assert ms["by_count_pct"] == 66.67
    assert ms["by_fee_eur_pct"] == 48.86  # (2 + 0.15) / (2 + 2.25 + 0.15)


def test_decisions_newest_first_and_limit(client: TestClient) -> None:
    body = client.get("/api/decisions").json()
    assert body["limit"] == 50 and len(body["decisions"]) == 2
    assert body["decisions"][0]["error"] == "timeout after 120s"
    assert body["decisions"][1]["decision_id"] == "2026-09-15T11:07:02Z-a1b2c3"
    assert body["decisions"][1]["latency_ms"] == 1234
    one = client.get("/api/decisions?limit=1").json()
    assert len(one["decisions"]) == 1 and one["decisions"][0]["error"] == "timeout after 120s"
    assert client.get("/api/decisions?limit=0").status_code == 422
    assert client.get("/api/decisions?limit=9999").status_code == 422


def test_missing_ledger_degrades_without_creating_it(tmp_path: Path) -> None:
    settings = Settings(
        ledger_db_path=tmp_path / "missing" / "ledger.sqlite",
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
    )
    with TestClient(create_app(settings)) as c:
        assert c.get("/healthz").json()["ledger"] is False
        eq = c.get("/api/equity").json()
        assert eq["ledger_available"] is False and eq["series"]["dates"] == [] and eq["summary"]["rows"] == 0
        fees = c.get("/api/fees").json()
        assert fees["ledger_available"] is False and fees["cum_eur"] == "0.00" and fees["fills_total"] == 0
        tax = c.get("/api/tax").json()
        assert tax["ledger_available"] is False and tax["gain_section_23_eur"] == "0.00"
        assert tax["open_lots"] == [] and tax["freigrenze_exceeded"] is False
        status = c.get("/api/status").json()
        assert status["bot"]["reachable"] is False and "FT_API_USER" in status["bot"]["error"]
        assert status["guard"]["state"] is None and status["guard"]["killswitch"]["active"] is False
        assert status["advisor"]["decision"] is None
        assert c.get("/api/decisions").json()["decisions"] == []
        page = c.get("/")
        assert page.status_code == 200 and "Ledger noch leer" in page.text
    assert not (tmp_path / "missing").exists()


# -- ledger access: read-only, one snapshot per request, calendar-day windows ----------------


def test_open_ledger_refuses_writes(env: Settings) -> None:
    conn = data.open_ledger(env.ledger_db_path)
    assert conn is not None
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"] == 6
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO sync_state (key, value) VALUES ('x', '1')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE IF NOT EXISTS scratch (id INTEGER)")
    finally:
        conn.rollback()
        conn.close()
    # Still six fills and no scratch table; no -wal/-shm left behind by the reader alone.
    writer = connect(env.ledger_db_path)
    try:
        assert writer.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"] == 6
        assert writer.execute("SELECT COUNT(*) AS n FROM sync_state WHERE key = 'x'").fetchone()["n"] == 0
    finally:
        writer.close()


def test_open_ledger_reads_one_consistent_snapshot(env: Settings) -> None:
    # A ledger sync committing between two SELECTs of one request must not be visible half-way.
    reader = data.open_ledger(env.ledger_db_path)
    assert reader is not None
    try:
        before = reader.execute("SELECT COUNT(*) AS n FROM equity_daily").fetchone()["n"]
        writer = connect(env.ledger_db_path)
        with writer:
            writer.execute(
                "INSERT INTO equity_daily VALUES ('2026-09-15', '0', '0', '1', '1', '1', '1', '0', '0', 1)"
            )
        writer.close()
        assert reader.execute("SELECT COUNT(*) AS n FROM equity_daily").fetchone()["n"] == before
        reader.rollback()
        assert reader.execute("SELECT COUNT(*) AS n FROM equity_daily").fetchone()["n"] == before + 1
    finally:
        reader.close()


def test_equity_window_counts_calendar_days_not_rows(tmp_path: Path) -> None:
    # Snapshots with gaps (timer missed days): "days=5" must cover 5 calendar days, not 5 rows.
    path = tmp_path / "ledger.sqlite"
    conn = connect(path)
    with conn:
        conn.executemany(
            "INSERT INTO equity_daily VALUES (?, '0', '0', '1', ?, '1', '1', '0', '0', 1)",
            [("2026-09-01", "1000"), ("2026-09-05", "1200"), ("2026-09-10", "900"), ("2026-09-14", "1100")],
        )
    conn.close()
    settings = Settings(ledger_db_path=path, advisor_dir=tmp_path / "a", guard_dir=tmp_path / "g")
    with TestClient(create_app(settings)) as c:
        five = c.get("/api/equity?days=5").json()
        ten = c.get("/api/equity?days=10").json()
        one = c.get("/api/equity?days=1").json()
        everything = c.get("/api/equity?days=3660").json()
    assert five["series"]["dates"] == ["2026-09-10", "2026-09-14"]
    assert five["summary"]["from"] == "2026-09-10" and five["summary"]["rows"] == 2
    # Drawdown stays measured from the all-time peak (1200 on 09-05): 900 is -25 %.
    assert five["series"]["drawdown"] == [-25.0, -8.33] and five["summary"]["max_drawdown_bot_pct"] == -25.0
    assert ten["series"]["dates"] == ["2026-09-05", "2026-09-10", "2026-09-14"]
    assert one["series"]["dates"] == ["2026-09-14"]
    assert everything["summary"]["rows"] == 4
