"""Tax panel numbers must equal the ledger's own yearly report."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from freezegun import freeze_time

from btctrader.common.config import Settings
from btctrader.common.db import connect
from btctrader.dashboard.app import create_app
from btctrader.ledger.export import build_tax_report
from tests.dashboard.conftest import ACCOUNT, NOW, make_fill, seed_ledger


@freeze_time(NOW)
def test_tax_current_year_matches_ledger_report(client: TestClient, env: Settings) -> None:
    body = client.get("/api/tax").json()
    assert body["year"] == 2026
    # Taxable: s1 only (296.75). s2 is past the one-year holding period.
    assert body["gain_before_expenses_eur"] == "296.75"
    assert body["expenses_eur"] == "9.99"
    assert body["sell_fees_eur"] == "2.00"
    assert body["werbungskosten_eur"] == "11.99"
    assert body["gain_section_23_eur"] == "286.76"
    assert body["gain_not_taxable_eur"] == "396.50"
    assert body["freigrenze_eur"] == "1000.00"
    assert body["distance_to_freigrenze_eur"] == "713.24"
    assert body["freigrenze_exceeded"] is False
    assert body["fees_ytd_eur"] == "5.45"
    c = body["counts"]
    assert c == {
        "disposals": 2,
        "taxable": 1,
        "boundary": 0,
        "fills_year": 4,
        "fills_total": 6,
        "open_lots": 3,
    }

    lots = body["open_lots"]
    assert [lot["remaining_qty_btc"] for lot in lots] == ["0.01", "0.01", "0.001"]
    # Lot 2 bought 2025-09-01: free from 2026-09-02, so already free on 2026-09-15.
    assert lots[0]["acquired_at"] == "2025-09-01T12:00:00.000000Z"  # iso_utc: fixed precision
    assert lots[0]["frei_ab"] == "2026-09-02" and lots[0]["tax_free_now"] is True
    assert lots[0]["days_until_free"] == 0 and lots[0]["pre_2027"] is True
    # Lot 3 bought 2026-06-01: free from 2027-06-02 (acquired + 1 year + 1 day).
    assert lots[1]["frei_ab"] == "2027-06-02" and lots[1]["tax_free_now"] is False
    assert lots[1]["days_until_free"] == 260
    assert lots[2]["frei_ab"] == "2027-09-11"

    # Same numbers as the report the ledger CLI writes.
    conn = connect(env.ledger_db_path)
    try:
        report = build_tax_report(conn, ACCOUNT, 2026)
    finally:
        conn.close()
    s = report.summary
    assert Decimal(body["gain_section_23_eur"]) == s.gain_section_23
    assert Decimal(body["distance_to_freigrenze_eur"]) == s.distance_to_freigrenze
    assert [lot["frei_ab"] for lot in lots] == [lot.free_from.isoformat() for lot in report.open_lots]


@freeze_time(NOW)
def test_tax_explicit_year_and_validation(client: TestClient) -> None:
    body = client.get("/api/tax?year=2025").json()
    assert body["year"] == 2025
    assert body["gain_section_23_eur"] == "0.00" and body["counts"]["disposals"] == 0
    assert body["counts"]["fills_year"] == 2 and body["counts"]["fills_total"] == 6
    assert body["fees_ytd_eur"] == "3.40"  # 2.5 + 0.9
    assert client.get("/api/tax?year=1999").status_code == 422
    assert client.get("/api/tax?year=abc").status_code == 422


def _cliff_settings(tmp_path: Path, sell_fee: str) -> Settings:
    # Buy 0.02 @ 50000 (1000 EUR, no fee), sell within the year @ 100000 (2000 EUR): gain = 1000 - fee.
    seed_ledger(
        tmp_path / "ledger.sqlite",
        [
            make_fill("b", "2025-01-15", "buy", "0.02", "50000", "0"),
            make_fill("s", "2025-06-01", "sell", "0.02", "100000", sell_fee),
        ],
        equity=False,
    )
    return Settings(
        ledger_db_path=tmp_path / "ledger.sqlite",
        ledger_account_id=ACCOUNT,
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
    )


def test_freigrenze_cliff_exactly_1000_is_exceeded(tmp_path: Path) -> None:
    with TestClient(create_app(_cliff_settings(tmp_path, "0"))) as c:
        body = c.get("/api/tax?year=2025").json()
    assert body["gain_section_23_eur"] == "1000.00"
    assert body["distance_to_freigrenze_eur"] == "0.00"
    assert body["freigrenze_exceeded"] is True
    assert body["open_lots"] == []
    page_settings = _cliff_settings(tmp_path / "page", "0")
    with freeze_time("2025-12-31T12:00:00Z"), TestClient(create_app(page_settings)) as c:
        html = c.get("/").text
    assert "Freigrenze überschritten" in html


def test_freigrenze_cliff_one_cent_below_is_not_exceeded(tmp_path: Path) -> None:
    with TestClient(create_app(_cliff_settings(tmp_path, "0.01"))) as c:
        body = c.get("/api/tax?year=2025").json()
    assert body["gain_section_23_eur"] == "999.99"
    assert body["distance_to_freigrenze_eur"] == "0.01"
    assert body["freigrenze_exceeded"] is False
