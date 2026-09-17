"""Monthly CSV export with SHA-256 sidecar and the yearly tax report."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

from btctrader.ledger.export import (
    FILL_EXPORT_COLUMNS,
    REPORT_COLUMNS,
    build_tax_report,
    export_month,
    months_with_fills,
    summary_lines,
    verify_export,
    write_tax_report,
)
from btctrader.ledger.fifo import rebuild
from tests.ledger.conftest import ACCOUNT, make_fill, store


def _seed(conn: sqlite3.Connection) -> None:
    store(
        conn,
        [
            make_fill("b1", "2025-01-15", "buy", "0.02", "50000", "2.5"),  # cost 1002.5
            make_fill("b2", "2025-03-01", "buy", "0.01", "60000", "0.9"),  # cost 600.9
            make_fill("s1", "2026-01-10", "sell", "0.01", "80000", "2"),  # lot 1, 360 days -> taxable
            make_fill("s2", "2026-02-01", "sell", "0.01", "90000", "2.25"),  # lot 1, > 1 year -> not taxable
            make_fill(
                "s3", "2026-03-01", "sell", "0.005", "100000", "1.25"
            ),  # lot 2, anniversary -> boundary
        ],
    )
    rebuild(conn, ACCOUNT)


def test_export_month_writes_csv_and_sidecar(ledger_conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed(ledger_conn)
    out = tmp_path / "exports"
    result = export_month(ledger_conn, ACCOUNT, 2026, 1, out)
    assert result.csv_path == out / "2026-01-bitvavo-fills.csv"
    assert result.sha_path == out / "2026-01-bitvavo-fills.csv.sha256"
    assert result.rows == 1
    digest = hashlib.sha256(result.csv_path.read_bytes()).hexdigest()
    assert result.sha256 == digest
    assert result.sha_path.read_text() == f"{digest}  2026-01-bitvavo-fills.csv\n"
    with result.csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(FILL_EXPORT_COLUMNS)
    assert rows[0]["exchange_trade_id"] == "s1" and rows[0]["side"] == "sell"
    assert rows[0]["amount_btc"] == "0.01" and rows[0]["gross_eur"] == "800"
    assert rows[0]["row_hash"] and rows[0]["prev_hash"]
    assert verify_export(result.csv_path)
    result.csv_path.write_text(result.csv_path.read_text().replace("800", "801"))
    assert not verify_export(result.csv_path)
    # Months without fills produce an empty CSV (header only), still with a sidecar.
    empty = export_month(ledger_conn, ACCOUNT, 2026, 6, out)
    assert empty.rows == 0 and empty.sha_path.exists()
    assert months_with_fills(ledger_conn, ACCOUNT) == [(2025, 1), (2025, 3), (2026, 1), (2026, 2), (2026, 3)]


def test_tax_report_rows_and_summary(ledger_conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed(ledger_conn)
    with ledger_conn:
        ledger_conn.execute(
            "INSERT INTO expenses (date_utc, amount_eur, description) VALUES ('2026-02-15', '9.99', 'VPS')"
        )
    report = build_tax_report(ledger_conn, ACCOUNT, 2026)
    s = report.summary
    assert s.count_total == 3 and s.count_taxable == 2 and s.count_boundary == 1
    # s1: 800 - 501.25 - 2 = 296.75 ; s3: 500 - 300.45 - 1.25 = 198.30 ; s2 not taxable: 900 - 501.25 - 2.25
    assert s.proceeds_taxable == Decimal("1300.00")
    assert s.cost_taxable == Decimal("801.70")
    assert s.sell_fees_taxable == Decimal("3.25")
    assert s.expenses == Decimal("9.99")
    assert s.gain_taxable_before_expenses == Decimal("495.05")
    assert s.gain_section_23 == Decimal("485.06")
    assert s.distance_to_freigrenze == Decimal("514.94")
    assert not s.freigrenze_exceeded
    assert s.gain_not_taxable == Decimal("396.50")
    assert len(report.open_lots) == 1 and report.open_lots[0].remaining_qty == Decimal("0.005")

    path, _ = write_tax_report(ledger_conn, ACCOUNT, 2026, tmp_path / "reports")
    assert path == tmp_path / "reports" / "steuer-2026.csv"
    assert path.with_name("steuer-2026.csv.sha256").exists()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == ";".join(REPORT_COLUMNS)
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    first = dict(zip(REPORT_COLUMNS, rows[1], strict=True))
    assert first["Kürzel"] == "BTC" and first["Menge"] == "0,01000000"
    assert first["Anschaffungszeitpunkt"] == "2025-01-15T12:00:00.000000Z"
    assert first["Anschaffungskosten inkl. Gebühren"] == "501,25"
    assert first["Plattform"] == "Bitvavo"
    assert first["Erlös"] == "800,00" and first["Verkaufsgebühr"] == "2,00"
    assert first["Haltedauer"] == "360 Tage" and first["Gewinn"] == "296,75"
    assert first["steuerbar"] == "ja" and first["Grenzfall"] == "nein"
    third = dict(zip(REPORT_COLUMNS, rows[3], strict=True))
    assert third["steuerbar"] == "ja" and third["Grenzfall"] == "ja"
    second = dict(zip(REPORT_COLUMNS, rows[2], strict=True))
    assert second["steuerbar"] == "nein"
    summary = {r[0]: r[1] for r in rows[4:] if len(r) >= 2}
    assert summary["Gesamtgewinn § 23 EStG (EUR)"] == "485,06"
    assert summary["Werbungskosten gesamt (EUR)"] == "13,24"
    assert summary["Abstand zur Freigrenze (EUR)"] == "514,94"
    assert summary["Anzahl Vorgänge"] == "3"
    assert summary["Freigrenze (EUR)"] == "1000,00"
    assert "steuerfrei ab" in text and "2026-03-02" in text
    printed = summary_lines(s)
    assert any("485.06 EUR" in line for line in printed)


def test_freigrenze_exceeded(ledger_conn: sqlite3.Connection) -> None:
    store(
        ledger_conn,
        [
            make_fill("b1", "2026-01-01", "buy", "0.1", "10000"),
            make_fill("s1", "2026-06-01", "sell", "0.1", "30000"),
        ],
    )
    rebuild(ledger_conn, ACCOUNT)
    s = build_tax_report(ledger_conn, ACCOUNT, 2026).summary
    assert s.gain_section_23 == Decimal("2000.00") and s.freigrenze_exceeded
    assert s.distance_to_freigrenze == Decimal("-1000.00")
