"""CLI subcommands against a temporary ledger and fixture Freqtrade DB."""

from __future__ import annotations

from pathlib import Path

import pytest

from btctrader.common.db import connect
from btctrader.ledger.cli import main
from tests.ledger.conftest import ACCOUNT
from tests.ledger.ft_fixture import create_ft_db


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
    assert "4 Fills gelesen, 4 neu" in out and "FIFO: 2 Lots, 2 Veräußerungen" in out

    assert main(["rebuild"]) == 0
    assert "FIFO neu aufgebaut: 2 Lots" in capsys.readouterr().out

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
    assert "Offene Lots: 1, Bestand 0.01 BTC" in out and "Hash-Kette: intakt" in out


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
