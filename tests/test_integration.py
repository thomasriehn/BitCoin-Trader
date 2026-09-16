"""Cross-component integration test.

One temp environment is driven through the real entry points in the order the
timers run them in production:

1. ``btctrader-ledger daily`` syncs fills from a fixture Freqtrade dry-run DB
   (``tests/ledger/ft_fixture.py``), writes the ``equity_daily`` snapshot against
   a respx-mocked Freqtrade ``/balance`` and public Bitvavo candles/ticker, and
   exports the monthly CSVs.
2. ``btctrader.advisor.advisor.run_advisor`` writes ``decision.json`` and
   ``decisions.jsonl`` against a respx-mocked vLLM ``/chat/completions``.
3. ``btctrader-guard check`` runs against the same mocked Freqtrade API in gate
   mode and writes ``state.json`` and ``events.jsonl``.
4. The dashboard (``create_app`` + ``TestClient``) reads all of it; every endpoint
   must answer 200 and the numbers it serves must be exactly the ones the ledger's
   own snapshot/report functions and the guard/advisor files contain.

No real network: HTTP is mocked with respx, the Freqtrade DB is a fixture file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from btctrader.advisor import advisor
from btctrader.advisor import context as ctx_mod
from btctrader.common.config import Settings, load_settings
from btctrader.common.db import connect_readonly, parse_iso
from btctrader.common.jsonl import read_json, read_jsonl_tail
from btctrader.dashboard.app import create_app
from btctrader.guard.cli import main as guard_main
from btctrader.ledger.cli import main as ledger_main
from btctrader.ledger.export import build_tax_report, verify_export
from btctrader.ledger.fifo import load_lots
from btctrader.ledger.snapshot import fees_cum, latest_snapshot, realized_gain_ytd
from tests.advisor.conftest import chat_response, good_answer, make_4h_candles, make_daily_candles
from tests.ledger.ft_fixture import create_ft_db

ACCOUNT = "bitvavo-main"
FT_URL = "http://127.0.0.1:8080"
FT_API = f"{FT_URL}/api/v1"
BITVAVO = "https://api.bitvavo.com/v2"
VLLM = "http://vllm.test/v1/chat/completions"
DAY_MS = 86_400_000
TICKER_PRICE = "50000"

# The fixture Freqtrade DB yields 5 fills (2 trades), 3 lots, 2 disposals and 0.014 BTC held; the
# mocked /balance reports 600.25 EUR + 0.012 BTC, valued at the mocked ticker (50000) = 1200.25.
EXPECTED_EQUITY = "1200.25"
EXPECTED_FEES_CUM = "3.03"
EXPECTED_FILLS = 5
EXPECTED_GAIN_2026 = "29.92"


def _balance_payload(eur: float = 600.25, btc: float = 0.012, price: float = 50000.0) -> dict[str, Any]:
    est_btc = btc * price
    return {
        "currencies": [
            {
                "currency": "EUR",
                "free": eur,
                "balance": eur,
                "used": 0.0,
                "est_stake": eur,
                "stake": "EUR",
                "side": "long",
                "is_position": False,
            },
            {
                "currency": "BTC",
                "free": btc,
                "balance": btc,
                "used": 0.0,
                "est_stake": est_btc,
                "stake": "EUR",
                "side": "long",
                "is_position": False,
            },
        ],
        "total": eur + est_btc,
        "stake": "EUR",
        "starting_capital": 1000.0,
        "value": 0.0,
    }


def _candles(request: httpx.Request) -> httpx.Response:
    """Daily candles for the requested window, newest first like the live API."""
    params = request.url.params
    start, end = int(params["start"]), int(params["end"])
    rows: list[list[Any]] = []
    t = start - start % DAY_MS
    while t <= end:
        rows.append([t, TICKER_PRICE, "51000", "49000", TICKER_PRICE, "10"])
        t += DAY_MS
    return httpx.Response(200, json=list(reversed(rows))[:1440])


class MockFreqtrade:
    """respx routes for everything ledger, guard and dashboard ask the Freqtrade API for."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.eur = 600.25
        self.btc = 0.012
        self.price = 50000.0
        self.open_trades: list[dict[str, Any]] = [
            {
                "trade_id": 2,
                "pair": "BTC/EUR",
                "amount": 0.012,
                "stake_amount": 450.0,
                "open_date": "2026-05-01 00:05:00",
                "open_rate": 45000.5,
                "current_rate": 50000.0,
                "profit_ratio": 0.1,
                "profit_abs": 45.0,
                "enter_tag": "trend_up",
                "strategy": "BtcTrend",
            }
        ]
        router.post(f"{FT_API}/token/login").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "refresh_token": "r"})
        )
        now_ts = int(datetime.now(UTC).timestamp())
        router.get(f"{FT_API}/health").mock(
            return_value=httpx.Response(
                200, json={"last_process_ts": now_ts, "last_process": None, "bot_start_ts": now_ts - 3600}
            )
        )
        router.get(f"{FT_API}/show_config").mock(
            return_value=httpx.Response(
                200,
                json={
                    "dry_run": True,
                    "version": "2026.8",
                    "state": "running",
                    "strategy": "BtcTrend",
                    "bot_name": "btc-dryrun",
                    "runmode": "dry_run",
                    "stake_currency": "EUR",
                    "max_open_trades": 1,
                    "timeframe": "1d",
                    "exchange": "bitvavo",
                },
            )
        )
        router.get(f"{FT_API}/profit").mock(
            return_value=httpx.Response(
                200,
                json={
                    "profit_closed_coin": 29.92,
                    "profit_closed_ratio": 0.0299,
                    "profit_all_coin": 74.92,
                    "profit_all_ratio": 0.0749,
                    "trade_count": 2,
                    "closed_trade_count": 1,
                    "winning_trades": 1,
                    "losing_trades": 0,
                },
            )
        )
        router.get(f"{FT_API}/balance").mock(side_effect=self._balance)
        router.get(f"{FT_API}/status").mock(side_effect=self._status)
        self.stopentry = router.post(f"{FT_API}/stopentry").mock(
            return_value=httpx.Response(200, json={"status": "No more entries will occur from now."})
        )
        self.forceexit = router.post(f"{FT_API}/forceexit").mock(
            return_value=httpx.Response(200, json={"result": "Created exit order for trade all."})
        )

    def _balance(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_balance_payload(self.eur, self.btc, self.price))

    def _status(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.open_trades)


@dataclass
class World:
    root: Path
    settings: Settings
    ft: MockFreqtrade
    vllm: respx.Route
    client: TestClient
    today: datetime

    @property
    def ledger_db(self) -> Path:
        return self.settings.ledger_db_path

    @property
    def guard_dir(self) -> Path:
        return self.settings.guard_dir

    @property
    def advisor_dir(self) -> Path:
        return self.settings.advisor_dir


def _run_advisor(settings: Settings, *, now: datetime) -> advisor.RunOutcome:
    """The advisor code path with a precomputed market context (the model call is what matters here)."""
    context = ctx_mod.assemble_context(make_daily_candles(end=now), make_4h_candles(end=now), now=now)
    return advisor.run_advisor(settings, context=context, system_prompt="SYS", now=now)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Run ledger daily, advisor and guard check once, then hand over a dashboard TestClient."""
    today = datetime.now(UTC).replace(microsecond=0)
    ft_db = create_ft_db(tmp_path / "tradesv3.dryrun.sqlite")
    env = {
        "BTCTRADER_ENV_FILE": "",
        "FT_API_URL": FT_URL,
        "FT_API_USER": "bot",
        "FT_API_PASS": "pw",
        "FT_DB_PATH": str(ft_db),
        "LEDGER_DB_PATH": str(tmp_path / "ledger" / "ledger.sqlite"),
        "LEDGER_ACCOUNT_ID": ACCOUNT,
        "LEDGER_SOURCE": "freqtrade-db",
        "LEDGER_EXPORT_DIR": str(tmp_path / "ledger" / "exports"),
        "BENCHMARK_START": (today.date() - timedelta(days=10)).isoformat(),
        "ADVISOR_BASE_URL": "http://vllm.test/v1",
        "ADVISOR_MODEL": "Qwen/Qwen3-14B",
        "ADVISOR_MODE": "gate",
        "ADVISOR_INTERVAL_HOURS": "1",
        "ADVISOR_DIR": str(tmp_path / "advisor"),
        "GUARD_DIR": str(tmp_path / "guard"),
        "GUARD_TRADE_ENV_FILE": str(tmp_path / "missing-secrets-guard.env"),
        "TZ_DISPLAY": "Europe/Berlin",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("GUARD_TRADE_API_KEY", "GUARD_TRADE_API_SECRET", "TELEGRAM_BOT_TOKEN", "NTFY_URL"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings()

    with respx.mock(assert_all_called=False) as router:
        ft = MockFreqtrade(router)
        router.get(f"{BITVAVO}/BTC-EUR/candles").mock(side_effect=_candles)
        router.get(f"{BITVAVO}/ticker/price").mock(
            return_value=httpx.Response(200, json={"market": "BTC-EUR", "price": TICKER_PRICE})
        )
        vllm = router.post(VLLM).mock(
            return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer())))
        )

        # 1. ledger: sync from the Freqtrade DB, snapshot, export (what the 00:20 UTC timer runs)
        assert ledger_main(["daily"]) == 0
        # 2. advisor: a fresh decision (10 minutes old) so the gate-mode guard sees it as valid
        outcome = _run_advisor(settings, now=today - timedelta(minutes=10))
        assert outcome.exit_code == 0 and outcome.error is None
        # 3. guard: one check against the mocked bot
        assert guard_main(["check"]) == 0

        with TestClient(create_app(settings)) as client:
            yield World(tmp_path, settings, ft, vllm, client, today)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _ledger_numbers(world: World) -> dict[str, Any]:
    """Reference values straight from the ledger's own functions (read-only connection)."""
    conn = connect_readonly(world.ledger_db)
    try:
        snap = latest_snapshot(conn)
        assert snap is not None
        year = world.today.year
        report = build_tax_report(conn, ACCOUNT, year)
        return {
            "snapshot": snap,
            # the ledger snapshot and the API both store/serve cents; the raw sums keep all digits
            "fees_cum": _cents(fees_cum(conn, ACCOUNT, world.today.date())),
            "gain_ytd": _cents(realized_gain_ytd(conn, ACCOUNT, world.today.date())),
            "report": report,
            "report_2026": build_tax_report(conn, ACCOUNT, 2026),
            "open_lots": load_lots(conn, ACCOUNT, open_only=True),
            "fills": conn.execute("SELECT COUNT(*) FROM fills WHERE account_id = ?", (ACCOUNT,)).fetchone()[
                0
            ],
        }
    finally:
        conn.close()


# -- the pipeline itself ----------------------------------------------------------------


def test_pipeline_produced_every_runtime_file(world: World) -> None:
    """Section 2 of KOMPONENTEN.md: the files each component owns exist after one cycle."""
    assert world.ledger_db.is_file()
    exports = world.settings.ledger_export_dir
    csvs = sorted(exports.glob("*-bitvavo-fills.csv"))
    assert len(csvs) == 2  # previous and current month
    for csv_path in csvs:
        assert csv_path.with_suffix(".csv.sha256").is_file()
        assert verify_export(csv_path)

    assert (world.advisor_dir / "decision.json").is_file()
    assert (world.advisor_dir / "decisions.jsonl").is_file()
    assert (world.advisor_dir / "context-latest.json").is_file()
    assert world.vllm.call_count == 1

    assert (world.guard_dir / "state.json").is_file()
    assert (world.guard_dir / "events.jsonl").is_file()
    assert not (world.guard_dir / "killswitch.lock").exists()
    assert world.ft.stopentry.call_count == 0 and world.ft.forceexit.call_count == 0

    numbers = _ledger_numbers(world)
    assert numbers["fills"] == EXPECTED_FILLS
    assert numbers["snapshot"]["equity_eur"] == EXPECTED_EQUITY
    assert numbers["snapshot"]["fees_cum_eur"] == EXPECTED_FEES_CUM
    assert numbers["snapshot"]["dry_run"] == 1
    assert numbers["report_2026"].summary.gain_section_23 == Decimal(EXPECTED_GAIN_2026)


def test_guard_in_gate_mode_accepts_the_advisors_decision(world: World) -> None:
    """The guard parses created_at exactly as the advisor writes it: fresh file, no advisor_stale event."""
    events = read_jsonl_tail(world.guard_dir / "events.jsonl", 50)
    assert [e["event"] for e in events] == ["day_start"]
    state = read_json(world.guard_dir / "state.json")
    assert state is not None
    assert Decimal(state["day_start_equity"]) == Decimal(EXPECTED_EQUITY)
    assert Decimal(state["peak_equity"]) == Decimal(EXPECTED_EQUITY)

    # A decision older than 3 x ADVISOR_INTERVAL_HOURS (written through the same code path)
    # must be flagged on the next run: the guard reads what the advisor writes.
    stale = _run_advisor(world.settings, now=world.today - timedelta(hours=4))
    assert stale.exit_code == 0
    assert guard_main(["check"]) == 0
    events = read_jsonl_tail(world.guard_dir / "events.jsonl", 50)
    assert [e["event"] for e in events] == ["day_start", "advisor_stale"]
    decision = read_json(world.advisor_dir / "decision.json")
    assert decision is not None
    # the guard re-serialises the parsed timestamp (fixed microsecond precision), so compare instants
    assert parse_iso(events[-1]["details"]["created_at"]) == parse_iso(decision["created_at"])
    assert events[-1]["details"]["limit_hours"] == 3.0


# -- dashboard endpoints --------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/partials/tiles",
        "/partials/fills",
        "/partials/events",
        "/api/equity",
        "/api/equity?days=30",
        "/api/fees",
        "/api/tax",
        "/api/tax?year=2026",
        "/api/status",
        "/api/decisions",
        "/api/decisions?limit=5",
        "/healthz",
    ],
)
def test_every_dashboard_endpoint_answers_200(world: World, path: str) -> None:
    response = world.client.get(path)
    assert response.status_code == 200, response.text[:500]
    if path.startswith("/api") or path == "/healthz":
        assert response.headers["content-type"].startswith("application/json")
        payload = response.json()
        assert payload.get("ledger_available", True) is True
    else:
        assert response.headers["content-type"].startswith("text/html")


def test_page_shows_the_ledger_equity_and_the_open_position(world: World) -> None:
    html = world.client.get("/").text
    for tile in ("equity", "outperformance", "drawdown", "fees", "tax", "bot", "advisor"):
        assert f'id="{tile}"' in html
    assert "1.200,25" in html  # German formatting of the snapshot equity
    assert ACCOUNT in html
    fills_html = world.client.get("/partials/fills").text
    assert "ft-dry_run_buy_2" in fills_html or "45.000,50" in fills_html


def test_api_equity_matches_the_ledger_snapshot(world: World) -> None:
    snap = _ledger_numbers(world)["snapshot"]
    payload = world.client.get("/api/equity").json()
    summary = payload["summary"]
    assert payload["series"]["dates"] == [snap["date_utc"]]
    # equity_daily stores normalised Decimal strings ("997.5"), the API serves cents ("997.50")
    assert Decimal(summary["equity_eur"]) == Decimal(snap["equity_eur"]) == Decimal(EXPECTED_EQUITY)
    assert Decimal(summary["bh_equity_eur"]) == Decimal(snap["bh_equity_eur"])
    assert Decimal(summary["dca_equity_eur"]) == Decimal(snap["dca_equity_eur"])
    assert Decimal(str(payload["series"]["equity"][0])) == Decimal(snap["equity_eur"])
    assert Decimal(str(payload["series"]["bh"][0])) == Decimal(snap["bh_equity_eur"])
    assert Decimal(str(payload["series"]["dca"][0])) == Decimal(snap["dca_equity_eur"])
    assert Decimal(summary["vs_bh_eur"]) == Decimal(snap["equity_eur"]) - Decimal(snap["bh_equity_eur"])
    assert Decimal(summary["vs_dca_eur"]) == Decimal(snap["equity_eur"]) - Decimal(snap["dca_equity_eur"])
    assert summary["dry_run"] is True
    assert summary["max_drawdown_bot_pct"] == 0.0  # a single snapshot cannot be below its own peak
    assert Decimal(str(summary["btc_price_eur"])) == Decimal(snap["btc_price_eur"]) == Decimal(TICKER_PRICE)
    # the ledger valued the bot's BTC at the same price the mocked Freqtrade balance used
    assert Decimal(snap["btc_balance"]) == Decimal("0.012") and Decimal(snap["eur_balance"]) == Decimal(
        "600.25"
    )


def test_api_fees_matches_the_ledger(world: World) -> None:
    numbers = _ledger_numbers(world)
    payload = world.client.get("/api/fees").json()
    assert Decimal(payload["cum_eur"]) == numbers["fees_cum"] == Decimal(EXPECTED_FEES_CUM)
    assert payload["cum_eur"] == numbers["snapshot"]["fees_cum_eur"]
    assert payload["fills_total"] == numbers["fills"] == EXPECTED_FILLS
    # dry-run fills carry no maker/taker flag (section 9): all unknown, no maker share
    share = payload["maker_share"]
    assert share["unknown_fills"] == EXPECTED_FILLS
    assert share["maker_fills"] == 0 and share["taker_fills"] == 0
    assert share["by_count_pct"] is None and share["by_fee_eur_pct"] is None
    # fixture fills are all in 2026, none in the last 30 days
    if world.today.year == 2026:
        assert payload["ytd_eur"] == payload["cum_eur"]
    assert payload["last_30d_eur"] == "0.00"


def test_api_tax_matches_the_ledger_report(world: World) -> None:
    numbers = _ledger_numbers(world)
    report = numbers["report_2026"]
    s = report.summary
    payload = world.client.get("/api/tax?year=2026").json()
    assert payload["year"] == 2026
    assert Decimal(payload["gain_section_23_eur"]) == s.gain_section_23 == Decimal(EXPECTED_GAIN_2026)
    assert Decimal(payload["gain_before_expenses_eur"]) == s.gain_taxable_before_expenses
    assert Decimal(payload["sell_fees_eur"]) == s.sell_fees_taxable
    assert Decimal(payload["expenses_eur"]) == s.expenses
    assert Decimal(payload["werbungskosten_eur"]) == s.sell_fees_taxable + s.expenses
    assert Decimal(payload["gain_not_taxable_eur"]) == s.gain_not_taxable
    assert Decimal(payload["proceeds_taxable_eur"]) == s.proceeds_taxable
    assert Decimal(payload["cost_taxable_eur"]) == s.cost_taxable
    assert Decimal(payload["freigrenze_eur"]) == s.freigrenze == Decimal(1000)
    assert Decimal(payload["distance_to_freigrenze_eur"]) == s.distance_to_freigrenze
    assert payload["freigrenze_exceeded"] is s.freigrenze_exceeded is False
    assert payload["counts"]["disposals"] == s.count_total == 2
    assert payload["counts"]["taxable"] == s.count_taxable == 2
    assert payload["counts"]["boundary"] == s.count_boundary
    assert payload["counts"]["fills_total"] == numbers["fills"]
    assert payload["counts"]["fills_year"] == EXPECTED_FILLS
    assert Decimal(payload["fees_ytd_eur"]) == numbers["fees_cum"]  # all fills are 2026 fills

    # open lots: same lots, same frei_ab as Lot.free_from, oldest first
    api_lots = payload["open_lots"]
    assert [lot["lot_id"] for lot in api_lots] == [lot.lot_id for lot in report.open_lots]
    assert len(api_lots) == len(numbers["open_lots"]) == 2
    for api_lot, lot in zip(api_lots, report.open_lots, strict=True):
        assert api_lot["acquired_at"] == lot.acquired_at
        assert Decimal(api_lot["remaining_qty_btc"]) == lot.remaining_qty
        assert Decimal(api_lot["cost_eur_incl_fees"]) == lot.cost_eur_incl_fees.quantize(Decimal("0.01"))
        assert api_lot["frei_ab"] == lot.free_from.isoformat()
        assert api_lot["pre_2027"] is bool(lot.pre_2027)
        assert api_lot["tax_free_now"] is (world.today.date() >= lot.free_from)
    assert sum(Decimal(lot["remaining_qty_btc"]) for lot in api_lots) == Decimal("0.014")

    # the snapshot's realized_gain_ytd_eur is the same section 23 figure for the current year
    if world.today.year == 2026:
        assert Decimal(numbers["snapshot"]["realized_gain_ytd_eur"]) == s.gain_section_23
    default_year = world.client.get("/api/tax").json()
    assert default_year["year"] == world.today.year
    assert Decimal(default_year["gain_section_23_eur"]) == numbers["report"].summary.gain_section_23
    assert Decimal(numbers["snapshot"]["realized_gain_ytd_eur"]) == numbers["gain_ytd"]


def test_api_status_reflects_guard_advisor_and_bot(world: World) -> None:
    payload = world.client.get("/api/status").json()

    # guard: state.json and events.jsonl verbatim, no kill switch
    assert payload["guard"]["state"] == read_json(world.guard_dir / "state.json")
    assert payload["guard"]["killswitch"] == {"active": False, "content": None}
    events_file = read_jsonl_tail(world.guard_dir / "events.jsonl", 20)
    assert payload["guard"]["events"] == list(reversed(events_file))

    # advisor: decision.json verbatim, valid, confidence as percent, mode from settings
    decision = read_json(world.advisor_dir / "decision.json")
    assert decision is not None
    assert payload["advisor"]["decision"] == decision
    assert payload["advisor"]["valid"] is True
    assert payload["advisor"]["confidence_pct"] == 55.0
    assert 0 <= payload["advisor"]["age_s"] <= 15 * 60
    assert payload["advisor"]["last_log"]["decision_id"] == decision["decision_id"]
    assert "raw_response" not in payload["advisor"]["last_log"]
    assert payload["advisor_mode"] == "gate" == decision["mode"]
    assert parse_iso(decision["valid_until"]) - parse_iso(decision["created_at"]) == timedelta(hours=2)

    # bot: the same balance the ledger snapshot and the guard equity were built from
    bot = payload["bot"]
    assert bot["reachable"] is True and bot["error"] is None
    assert bot["config"]["dry_run"] is True and bot["config"]["state"] == "running"
    assert Decimal(str(bot["balance"]["total"])) == Decimal(EXPECTED_EQUITY)
    assert {c["currency"] for c in bot["balance"]["currencies"]} == {"EUR", "BTC"}
    assert [t["trade_id"] for t in bot["open_trades"]] == [2]
    assert bot["profit"]["closed_trade_count"] == 1


def test_api_decisions_returns_the_advisor_log(world: World) -> None:
    payload = world.client.get("/api/decisions?limit=5").json()
    decision = read_json(world.advisor_dir / "decision.json")
    assert decision is not None
    assert payload["limit"] == 5
    assert len(payload["decisions"]) == 1
    line = payload["decisions"][0]
    for key, value in decision.items():
        assert line[key] == value
    assert line["error"] is None
    assert line["usage"]["prompt_tokens"] == 1200
    assert json.loads(line["raw_response"]) == good_answer()


def test_healthz_sees_the_ledger(world: World) -> None:
    payload = world.client.get("/healthz").json()
    assert payload["status"] == "ok" and payload["ledger"] is True


# -- kill switch through the whole stack ------------------------------------------------------


def test_kill_switch_reaches_the_dashboard(world: World) -> None:
    """Equity 25 % below the peak: guard force-exits, locks, and the dashboard shows the lock."""
    world.ft.eur = 450.0
    world.ft.btc = 0.0
    assert guard_main(["check"]) == 0
    assert world.ft.forceexit.call_count == 1
    assert json.loads(world.ft.forceexit.calls[0].request.content) == {
        "tradeid": "all",
        "ordertype": "market",
    }
    assert world.ft.stopentry.call_count == 1
    lock = read_json(world.guard_dir / "killswitch.lock")
    assert lock is not None
    assert lock["peak_equity_eur"] == EXPECTED_EQUITY and lock["equity_eur"] == "450.00"

    status = world.client.get("/api/status").json()
    assert status["guard"]["killswitch"] == {"active": True, "content": lock}
    names = [e["event"] for e in status["guard"]["events"]]
    assert names[0] == "killswitch" and "daily_loss" in names and names[-1] == "day_start"
    assert status["guard"]["state"]["killswitch_at"] == lock["ts"]
    page = world.client.get("/").text
    assert "Kill-Switch" in page
    events_html = world.client.get("/partials/events").text
    assert "killswitch" in events_html

    # the ledger's numbers are untouched by a guard event
    assert world.client.get("/api/fees").json()["cum_eur"] == EXPECTED_FEES_CUM
    assert world.client.get("/api/equity").json()["summary"]["equity_eur"] == EXPECTED_EQUITY
