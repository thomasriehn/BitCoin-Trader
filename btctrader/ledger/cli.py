"""``btctrader-ledger`` command line: sync, snapshot, export, report, rebuild, show, daily."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from btctrader.common import bitvavo_public
from btctrader.common.config import ConfigError, Settings, load_settings
from btctrader.common.db import connect, parse_iso
from btctrader.common.ftapi import FreqtradeError, client_from_settings
from btctrader.common.log import setup_logging
from btctrader.ledger.benchmarks import load_price_series
from btctrader.ledger.errors import LedgerError
from btctrader.ledger.export import export_month, months_with_fills, summary_lines, write_tax_report
from btctrader.ledger.fifo import load_lots, rebuild
from btctrader.ledger.fills import fmt, verify_chain
from btctrader.ledger.snapshot import (
    Balances,
    balances_from_exchange,
    balances_from_freqtrade,
    latest_snapshot,
    write_snapshot,
)
from btctrader.ledger.sync import make_exchange, run_sync

log = logging.getLogger("btctrader.ledger")


def _today() -> date:
    return datetime.now(tz=UTC).date()


def _parse_month(text: str) -> tuple[int, int]:
    try:
        year, month = text.split("-")
        y, m = int(year), int(month)
        date(y, m, 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {text!r}") from exc
    return y, m


def _parse_since(text: str) -> int:
    """ISO-8601 timestamp (naive values are UTC) to epoch milliseconds."""
    try:
        return int(parse_iso(text).timestamp() * 1000)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an ISO-8601 timestamp, got {text!r}") from exc


def _previous_month(today: date) -> tuple[int, int]:
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btctrader-ledger", description="Ledger: Fills, FIFO, Benchmarks, Exporte"
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--db", type=Path, default=None, help="Pfad zur ledger.sqlite (Default: LEDGER_DB_PATH)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Fills holen, speichern, FIFO neu aufbauen")
    p_sync.add_argument("--source", choices=("exchange", "freqtrade-db"), default=None)
    p_sync.add_argument("--ft-db", type=Path, default=None, help="Freqtrade-DB (Default: FT_DB_PATH)")
    p_sync.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="ISO-Zeitpunkt (UTC, wenn ohne Zeitzone), ab dem Börsen-Fills geholt werden",
    )

    p_snap = sub.add_parser("snapshot", help="Tageszeile in equity_daily schreiben")
    p_snap.add_argument("--date", type=date.fromisoformat, default=None, help="UTC-Datum (Default: heute)")

    p_exp = sub.add_parser("export", help="Monats-CSV der Fills mit SHA-256")
    p_exp.add_argument(
        "--month", type=_parse_month, default=None, help="YYYY-MM (Default: Vormonat und aktueller Monat)"
    )
    p_exp.add_argument("--all", action="store_true", help="alle Monate mit Fills exportieren")

    p_rep = sub.add_parser("report", help="Jahresreport steuer-YYYY.csv")
    p_rep.add_argument("--year", type=int, default=None)
    p_rep.add_argument("--out-dir", type=Path, default=None, help="Default: <LEDGER_EXPORT_DIR>/../reports")

    sub.add_parser("rebuild", help="Hash-Kette prüfen und FIFO neu aufbauen")

    p_show = sub.add_parser("show", help="letzte Fills, offene Lots und letzte Equity-Zeile anzeigen")
    p_show.add_argument("--limit", type=int, default=20)

    sub.add_parser("daily", help="sync, snapshot und export nacheinander (für den Timer)")
    return parser


def _settings(args: argparse.Namespace, require: Sequence[str]) -> Settings:
    settings = load_settings(require=require)
    return settings


def _open_db(args: argparse.Namespace, settings: Settings) -> sqlite3.Connection:
    return connect(args.db or settings.ledger_db_path)


# -- commands -------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    result = run_sync(conn, settings, source=args.source, ft_db_path=args.ft_db, since_ms=args.since)
    print(
        f"sync {result.source}: {result.fetched} Fills gelesen, {result.upsert.inserted} neu, "
        f"{result.upsert.unchanged} unverändert, {result.upsert.updated_meta} Metadaten aktualisiert, "
        f"{result.upsert.conflict_count} Konflikte"
    )
    if result.fifo is not None:
        print(
            f"FIFO: {len(result.fifo.lots)} Lots, {len(result.fifo.disposals)} Veräußerungen, "
            f"Bestand {fmt(result.fifo.btc_held)} BTC"
        )
    for warning in result.warnings:
        print(f"WARNUNG: {warning}")
    if result.fifo_error:
        print(f"FEHLER FIFO (Lots und Veräußerungen veraltet): {result.fifo_error}", file=sys.stderr)
        return 1
    return 0 if not result.warnings else 2


def cmd_snapshot(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if settings.benchmark_start is None:
        raise ConfigError("BENCHMARK_START is required for snapshots")
    on = args.date or _today()
    if settings.ledger_source == "exchange":
        balances = balances_from_exchange(make_exchange(settings))
        dry_run = False
    else:
        if not (settings.ft_api_user and settings.ft_api_pass):
            raise ConfigError("FT_API_USER and FT_API_PASS are required for freqtrade-db snapshots")
        with client_from_settings(settings) as client:
            balances = balances_from_freqtrade(client)
            try:
                dry_run = bool(client.show_config().get("dry_run", True))
            except FreqtradeError:
                dry_run = True
    series = load_price_series(settings.benchmark_start, on)
    price = bitvavo_public.fetch_ticker_price() if on >= _today() else series.close_on(on)
    row = write_snapshot(
        conn,
        account_id=settings.ledger_account_id,
        on=on,
        balances=Balances(eur=balances.eur, btc=balances.btc),
        price=price,
        series=series,
        benchmark_start=settings.benchmark_start,
        start_capital=settings.start_capital_eur,
        dca_weeks=settings.dca_weeks,
        fee_taker=settings.fee_taker,
        dry_run=dry_run,
    )
    print(
        f"snapshot {row['date_utc']}: Equity {row['equity_eur']} EUR, B&H {row['bh_equity_eur']} EUR, "
        f"DCA {row['dca_equity_eur']} EUR, Gebühren {row['fees_cum_eur']} EUR, "
        f"§ 23 YTD {row['realized_gain_ytd_eur']} EUR"
    )
    return 0


def cmd_export(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    account = settings.ledger_account_id
    if args.all:
        months = months_with_fills(conn, account)
    elif args.month:
        months = [args.month]
    else:
        today = _today()
        months = [_previous_month(today), (today.year, today.month)]
    for year, month in months:
        result = export_month(conn, account, year, month, settings.ledger_export_dir)
        print(f"{result.csv_path} ({result.rows} Fills) sha256 {result.sha256}")
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    year = args.year or _today().year
    out_dir = args.out_dir or settings.ledger_export_dir.parent / "reports"
    path, report = write_tax_report(conn, settings.ledger_account_id, year, out_dir)
    print(f"Report: {path}")
    for line in summary_lines(report.summary):
        print(line)
    return 0


def cmd_rebuild(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    problems = verify_chain(conn, settings.ledger_account_id)
    if problems:
        for p in problems:
            print(f"HASH-KETTE DEFEKT: {p}")
        return 1
    result = rebuild(conn, settings.ledger_account_id)
    print(
        f"FIFO neu aufgebaut: {len(result.lots)} Lots, {len(result.disposals)} Veräußerungen, "
        f"Bestand {fmt(result.btc_held)} BTC"
    )
    return 0


def cmd_show(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    account = settings.ledger_account_id
    rows = conn.execute(
        "SELECT id, ts_utc, side, amount_btc, price_eur, fee_eur, fee_currency, source, signal_reason "
        "FROM fills WHERE account_id = ? ORDER BY ts_utc DESC, exchange_trade_id DESC LIMIT ?",
        (account, args.limit),
    ).fetchall()
    print(f"Letzte Fills ({account}):")
    for r in reversed(rows):
        print(
            f"  #{r['id']} {r['ts_utc']} {r['side']:4} {r['amount_btc']} BTC @ {r['price_eur']} EUR "
            f"fee {r['fee_eur']} EUR ({r['fee_currency']}) [{r['source']}] {r['signal_reason'] or ''}"
        )
    lots = load_lots(conn, account, open_only=True)
    total = sum((lot.remaining_qty for lot in lots), Decimal(0))
    print(f"Offene Lots: {len(lots)}, Bestand {fmt(total)} BTC")
    for lot in lots:
        print(
            f"  Lot {lot.lot_id} seit {lot.acquired_at}: {fmt(lot.remaining_qty)} BTC, "
            f"steuerfrei ab {lot.free_from.isoformat()}"
        )
    snap = latest_snapshot(conn)
    if snap:
        print(
            f"Letzter Snapshot {snap['date_utc']}: Equity {snap['equity_eur']} EUR, "
            f"B&H {snap['bh_equity_eur']} EUR, DCA {snap['dca_equity_eur']} EUR"
        )
    problems = verify_chain(conn, account)
    print("Hash-Kette: " + ("intakt" if not problems else f"DEFEKT ({len(problems)} Probleme)"))
    return 0 if not problems else 1


def cmd_daily(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """sync, snapshot, export in sequence; a failing step is reported but does not skip the others."""
    steps = (
        ("sync", cmd_sync, argparse.Namespace(source=None, ft_db=None, since=None)),
        ("snapshot", cmd_snapshot, argparse.Namespace(date=None)),
        ("export", cmd_export, argparse.Namespace(all=False, month=None)),
    )
    rc = 0
    for name, func, step_args in steps:
        try:
            rc = max(rc, func(step_args, settings, conn))
        except HANDLED_ERRORS as exc:
            log.error("daily %s failed: %s", name, exc)
            print(f"Fehler ({name}): {exc}", file=sys.stderr)
            rc = max(rc, 1)
    return rc


COMMANDS = {
    "sync": cmd_sync,
    "snapshot": cmd_snapshot,
    "export": cmd_export,
    "report": cmd_report,
    "rebuild": cmd_rebuild,
    "show": cmd_show,
    "daily": cmd_daily,
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "snapshot": ("benchmark_start",),
    "daily": ("benchmark_start",),
}

# Errors that end a command with a one-line message instead of a traceback. ccxt errors are
# wrapped into LedgerError in sync/snapshot; sqlite3.Error covers a locked or corrupt DB.
HANDLED_ERRORS: tuple[type[Exception], ...] = (
    ConfigError,
    LedgerError,
    FreqtradeError,
    bitvavo_public.BitvavoError,
    sqlite3.Error,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging("btctrader.ledger", args.log_level)
    try:
        settings = _settings(args, REQUIRED.get(args.command, ()))
        conn = _open_db(args, settings)
        try:
            return COMMANDS[args.command](args, settings, conn)
        finally:
            conn.close()
    except HANDLED_ERRORS as exc:
        log.error("%s", exc)
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
