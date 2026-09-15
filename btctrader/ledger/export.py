"""Monthly fill exports (CSV + SHA-256 sidecar) and the yearly tax report.

Monthly export: ``<export_dir>/YYYY-MM-bitvavo-fills.csv`` with all fills of the
account in that month (UTC), comma separated, dot decimals, plus
``<name>.csv.sha256`` in ``sha256sum`` format.

Yearly report: ``<reports_dir>/steuer-YYYY.csv`` with one row per disposal in
the columns BMF letter of 06.03.2025 Rn. 102/103 asks for, followed by a
summary block (Gesamtgewinn section 23, Werbungskosten, distance to the
1.000 EUR Freigrenze, number of transactions). Semicolon separated with
German decimal commas so it opens cleanly in a German spreadsheet.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from btctrader.ledger.fifo import Disposal, Lot, load_disposals, load_lots
from btctrader.ledger.fills import ALL_FIELDS, Fill, fmt

FREIGRENZE_EUR = Decimal("1000")  # section 23 (3) sentence 5 EStG, from assessment period 2024
FREIGRENZE_BEFORE_2024_EUR = Decimal("600")
Q2 = Decimal("0.01")
ZERO = Decimal(0)

FILL_EXPORT_COLUMNS: tuple[str, ...] = ("id", *ALL_FIELDS, "prev_hash", "row_hash")

REPORT_COLUMNS: tuple[str, ...] = (
    "Nr",
    "Art",
    "Kürzel",
    "Menge",
    "Anschaffungszeitpunkt",
    "Anschaffungskosten inkl. Gebühren",
    "Plattform",
    "Veräußerungszeitpunkt",
    "Erlös",
    "Verkaufsgebühr",
    "Haltedauer",
    "Gewinn",
    "steuerbar",
    "Grenzfall",
    "Lot",
    "Fill-ID",
)


def q2(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_EVEN)


def de_number(value: Decimal, places: int | None = 2) -> str:
    """German number formatting: decimal comma, no thousands separator."""
    if places is not None:
        value = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    return format(value, "f").replace(".", ",")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# -- monthly fills export ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportResult:
    csv_path: Path
    sha_path: Path
    rows: int
    sha256: str


def fills_in_month(conn: sqlite3.Connection, account_id: str, year: int, month: int) -> list[Fill]:
    start = date(year, month, 1).isoformat()
    end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)).isoformat()
    rows = conn.execute(
        "SELECT * FROM fills WHERE account_id = ? AND ts_utc >= ? AND ts_utc < ? "
        "ORDER BY ts_utc, exchange_trade_id",
        (account_id, start, end),
    ).fetchall()
    return [Fill.from_row(r) for r in rows]


def fills_csv(fills: list[Fill]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(FILL_EXPORT_COLUMNS)
    for fill in fills:
        row = fill.to_row()
        row["id"] = fill.id
        writer.writerow(["" if row[c] is None else row[c] for c in FILL_EXPORT_COLUMNS])
    return buf.getvalue()


def export_month(
    conn: sqlite3.Connection,
    account_id: str,
    year: int,
    month: int,
    export_dir: str | Path,
    *,
    exchange: str = "bitvavo",
) -> ExportResult:
    """Write ``YYYY-MM-<exchange>-fills.csv`` and its ``.sha256`` sidecar; returns paths and digest."""
    fills = fills_in_month(conn, account_id, year, month)
    directory = Path(export_dir)
    csv_path = directory / f"{year:04d}-{month:02d}-{exchange}-fills.csv"
    _atomic_write_text(csv_path, fills_csv(fills))
    digest = sha256_file(csv_path)
    sha_path = csv_path.with_name(csv_path.name + ".sha256")
    _atomic_write_text(sha_path, f"{digest}  {csv_path.name}\n")
    return ExportResult(csv_path=csv_path, sha_path=sha_path, rows=len(fills), sha256=digest)


def verify_export(csv_path: str | Path) -> bool:
    """Check a CSV against its sidecar. Returns False when the sidecar is missing or differs."""
    path = Path(csv_path)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        return False
    expected = sidecar.read_text(encoding="utf-8").split()[0] if sidecar.stat().st_size else ""
    return expected == sha256_file(path)


def months_with_fills(conn: sqlite3.Connection, account_id: str) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT DISTINCT substr(ts_utc, 1, 7) AS ym FROM fills WHERE account_id = ? ORDER BY ym",
        (account_id,),
    ).fetchall()
    result: list[tuple[int, int]] = []
    for r in rows:
        y, m = str(r["ym"]).split("-")
        result.append((int(y), int(m)))
    return result


# -- yearly tax report ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaxSummary:
    year: int
    count_total: int
    count_taxable: int
    count_boundary: int
    proceeds_taxable: Decimal
    cost_taxable: Decimal
    sell_fees_taxable: Decimal  # Werbungskosten aus Verkaufsgebühren
    expenses: Decimal  # weitere Werbungskosten (Tabelle expenses)
    gain_taxable_before_expenses: Decimal
    gain_section_23: Decimal  # nach allen Werbungskosten
    gain_not_taxable: Decimal
    freigrenze: Decimal
    distance_to_freigrenze: Decimal

    @property
    def freigrenze_exceeded(self) -> bool:
        return self.gain_section_23 >= self.freigrenze


@dataclass(frozen=True, slots=True)
class TaxReport:
    disposals: list[Disposal]
    open_lots: list[Lot]
    summary: TaxSummary


def expenses_in_year(conn: sqlite3.Connection, year: int) -> Decimal:
    rows = conn.execute(
        "SELECT amount_eur FROM expenses WHERE date_utc >= ? AND date_utc < ?",
        (f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
    ).fetchall()
    return sum((Decimal(r["amount_eur"]) for r in rows if r["amount_eur"] is not None), ZERO)


def build_tax_report(conn: sqlite3.Connection, account_id: str, year: int) -> TaxReport:
    disposals = load_disposals(conn, account_id, year=year)
    taxable = [d for d in disposals if d.taxable]
    proceeds = sum((d.proceeds_eur for d in taxable), ZERO)
    cost = sum((d.cost_eur for d in taxable), ZERO)
    sell_fees = sum((d.sell_fee_eur for d in taxable), ZERO)
    gain_before = sum((d.gain_eur for d in taxable), ZERO)
    expenses = expenses_in_year(conn, year)
    gain = gain_before - expenses
    freigrenze = FREIGRENZE_EUR if year >= 2024 else FREIGRENZE_BEFORE_2024_EUR
    summary = TaxSummary(
        year=year,
        count_total=len(disposals),
        count_taxable=len(taxable),
        count_boundary=sum(1 for d in disposals if d.boundary_case),
        proceeds_taxable=q2(proceeds),
        cost_taxable=q2(cost),
        sell_fees_taxable=q2(sell_fees),
        expenses=q2(expenses),
        gain_taxable_before_expenses=q2(gain_before),
        gain_section_23=q2(gain),
        gain_not_taxable=q2(sum((d.gain_eur for d in disposals if not d.taxable), ZERO)),
        freigrenze=freigrenze,
        distance_to_freigrenze=q2(freigrenze - gain),
    )
    return TaxReport(
        disposals=disposals, open_lots=load_lots(conn, account_id, open_only=True), summary=summary
    )


def tax_report_csv(report: TaxReport, *, platform: str = "Bitvavo") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(REPORT_COLUMNS)
    for n, d in enumerate(report.disposals, start=1):
        writer.writerow(
            [
                n,
                "Verkauf" if d.kind == "sell" else "Gebühr in BTC",
                "BTC",
                de_number(d.qty_btc, 8),
                d.acquired_at,
                de_number(d.cost_eur),
                platform,
                d.disposed_at,
                de_number(d.proceeds_eur),
                de_number(d.sell_fee_eur),
                f"{d.holding_days} Tage",
                de_number(d.gain_eur),
                "ja" if d.taxable else "nein",
                "ja" if d.boundary_case else "nein",
                d.lot_id,
                d.sell_fill_id,
            ]
        )
    s = report.summary
    writer.writerow([])
    writer.writerow(["Zusammenfassung", f"Steuerjahr {s.year}"])
    writer.writerow(["Anzahl Vorgänge", s.count_total])
    writer.writerow(["Davon steuerbar (Haltefrist <= 1 Jahr)", s.count_taxable])
    writer.writerow(["Davon Grenzfälle (Veräußerung am Jahrestag)", s.count_boundary])
    writer.writerow(["Erlöse steuerbar (EUR)", de_number(s.proceeds_taxable)])
    writer.writerow(["Anschaffungskosten inkl. Gebühren steuerbar (EUR)", de_number(s.cost_taxable)])
    writer.writerow(["Werbungskosten Verkaufsgebühren (EUR)", de_number(s.sell_fees_taxable)])
    writer.writerow(["Werbungskosten sonstige Ausgaben (EUR)", de_number(s.expenses)])
    writer.writerow(["Werbungskosten gesamt (EUR)", de_number(s.sell_fees_taxable + s.expenses)])
    writer.writerow(["Gesamtgewinn § 23 EStG (EUR)", de_number(s.gain_section_23)])
    writer.writerow(["Freigrenze (EUR)", de_number(s.freigrenze)])
    writer.writerow(["Abstand zur Freigrenze (EUR)", de_number(s.distance_to_freigrenze)])
    writer.writerow(["Freigrenze erreicht oder überschritten", "ja" if s.freigrenze_exceeded else "nein"])
    writer.writerow(["Nicht steuerbare Gewinne, Haltefrist > 1 Jahr (EUR)", de_number(s.gain_not_taxable)])
    writer.writerow([])
    writer.writerow(["Offene Lots", "Lot", "Anschaffungszeitpunkt", "Restmenge BTC", "steuerfrei ab"])
    for lot in report.open_lots:
        writer.writerow(
            ["", lot.lot_id, lot.acquired_at, de_number(lot.remaining_qty, 8), lot.free_from.isoformat()]
        )
    return buf.getvalue()


def write_tax_report(
    conn: sqlite3.Connection,
    account_id: str,
    year: int,
    reports_dir: str | Path,
    *,
    platform: str = "Bitvavo",
) -> tuple[Path, TaxReport]:
    """Write ``steuer-YYYY.csv`` (plus ``.sha256``) and return its path and the report data."""
    report = build_tax_report(conn, account_id, year)
    path = Path(reports_dir) / f"steuer-{year:04d}.csv"
    _atomic_write_text(path, tax_report_csv(report, platform=platform))
    digest = sha256_file(path)
    _atomic_write_text(path.with_name(path.name + ".sha256"), f"{digest}  {path.name}\n")
    return path, report


def summary_lines(summary: TaxSummary) -> list[str]:
    """Human readable summary for the CLI (German, EUR with two decimals)."""
    return [
        f"Steuerjahr {summary.year}: {summary.count_total} Vorgänge, "
        f"davon {summary.count_taxable} steuerbar, {summary.count_boundary} Grenzfälle",
        f"Gesamtgewinn § 23 EStG: {fmt(summary.gain_section_23)} EUR",
        f"Werbungskosten: {fmt(summary.sell_fees_taxable + summary.expenses)} EUR "
        f"(Verkaufsgebühren {fmt(summary.sell_fees_taxable)}, sonstige {fmt(summary.expenses)})",
        f"Abstand zur Freigrenze {fmt(summary.freigrenze)} EUR: {fmt(summary.distance_to_freigrenze)} EUR",
        f"Nicht steuerbar (Haltefrist > 1 Jahr): {fmt(summary.gain_not_taxable)} EUR",
    ]
