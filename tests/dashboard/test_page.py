"""HTML page, partials, static files and formatting filters."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from btctrader.common.config import Settings
from btctrader.dashboard.app import (
    CHARTJS_CDN,
    HTMX_CDN,
    STATIC_DIR,
    create_app,
    de_date,
    fmt_btc,
    fmt_eur,
    fmt_pct,
    make_local_ts,
    resolve_tz,
)
from tests.dashboard.conftest import ACCOUNT, NOW, make_fill, seed_ledger

TILE_IDS = ("equity", "outperformance", "drawdown", "fees", "tax", "bot", "advisor")


@freeze_time(NOW)
def test_index_renders_tiles_charts_fills_and_events(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    for tile in TILE_IDS:
        assert f'id="{tile}"' in html, tile
    assert 'id="equity-chart"' in html and 'id="drawdown-chart"' in html
    assert 'hx-get="/partials/tiles"' in html and 'hx-trigger="every 30s"' in html
    assert html.count('hx-sync="this:drop"') == 3  # a slow poll is never stacked by the next one
    assert 'hx-get="/partials/fills"' in html and 'hx-get="/partials/events"' in html
    assert 'src="/static/vendor/chart.umd.js"' in html and 'src="/static/vendor/htmx.min.js"' in html
    assert CHARTJS_CDN in html and HTMX_CDN in html and "sha384-" in html
    assert 'lang="de"' in html and "Überrendite" in html and "§ 23 Gewinn 2026" in html
    # Tile values from the fixture ledger.
    assert "1.200,00 €" in html  # equity
    assert "286,76 €" in html  # section 23 gain
    assert "713,24 €" in html  # distance to Freigrenze
    assert "-10,00 %" in html  # max drawdown bot
    # Fills table (newest first, Berlin time 14:00 for 12:00Z in September) and events list.
    assert "Verkauf" in html and "Kauf" in html and "10.09.2026 14:00" in html
    assert "daily_loss" in html and "ft_unreachable" in html
    # Open lots with the tax-free date.
    assert "02.06.2027" in html


def test_partials_return_fragments(client: TestClient) -> None:
    tiles = client.get("/partials/tiles")
    assert tiles.status_code == 200 and "<html" not in tiles.text
    for tile in TILE_IDS:
        assert f'id="{tile}"' in tiles.text
    fills = client.get("/partials/fills")
    assert fills.status_code == 200 and "<table" in fills.text and "exit_signal" in fills.text
    events = client.get("/partials/events")
    assert events.status_code == 200 and "<ul" in events.text and "day_start" in events.text


def test_static_files_are_served(client: TestClient) -> None:
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "prefers-color-scheme: dark" in css.text
    assert "max-width: 700px" in css.text and "background: var(--surface-page)" in css.text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "/api/equity" in js.text
    sh = client.get("/static/vendor/fetch-vendor.sh")
    assert sh.status_code == 200 and "chart.js@4.5.1" in sh.text and "htmx.org@2.0.10" in sh.text


def test_index_hides_openapi(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1234.5"), "1.234,50 €"),
        ("-0.005", "-0,00 €"),  # half-even like the ledger's q2
        ("2.675", "2,68 €"),
        (None, "–"),
        ("", "–"),
        ("abc", "–"),
    ],
)
def test_fmt_eur(value: object, expected: str) -> None:
    assert fmt_eur(value) == expected


def test_other_filters() -> None:
    assert fmt_pct(7.5) == "+7,50 %" and fmt_pct(-3, 2, False) == "-3,00 %" and fmt_pct(None) == "–"
    assert fmt_btc("0.01000000") == "0,01 BTC" and fmt_btc("1") == "1 BTC"
    assert de_date("2027-06-02") == "02.06.2027" and de_date(None) == "–"
    berlin = make_local_ts("Europe/Berlin")
    assert berlin("2026-01-10T12:00:00Z") == "10.01.2026 13:00"
    assert berlin("2026-07-10T12:00:00Z", "%H:%M") == "14:00"
    assert berlin("not a date") == "not a date" and berlin(None) == "–"
    assert make_local_ts("No/Such_Zone")("2026-01-10T12:00:00Z") == "10.01.2026 12:00"


@freeze_time(NOW)
def test_tiles_partial_refreshes_header_time_out_of_band(client: TestClient) -> None:
    # The header's "Stand" is server time in TZ_DISPLAY; the tiles partial carries it as an
    # htmx out-of-band swap, so the browser clock and zone are never used.
    index = client.get("/").text
    assert index.count('id="generated-at"') == 1 and "hx-swap-oob" not in index
    assert 'id="generated-at">15.09.2026 14:00:00<' in index
    tiles = client.get("/partials/tiles").text
    assert '<span id="generated-at" hx-swap-oob="true">15.09.2026 14:00:00</span>' in tiles
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "toLocaleString" not in js and "generated-at" not in js


@freeze_time(NOW)
def test_lot_purchase_date_is_the_utc_calendar_day(tmp_path: Path) -> None:
    # Bought 23:30 UTC on 1 June (already 2 June in Berlin). frei_ab is computed on the UTC
    # calendar day (2027-06-02), so the table must show 01.06.2026, not 02.06.2026.
    seed_ledger(
        tmp_path / "ledger.sqlite",
        [make_fill("late", "2026-06-01T23:30:00Z", "buy", "0.01", "70000", "1.05")],
        equity=False,
    )
    settings = Settings(
        ledger_db_path=tmp_path / "ledger.sqlite",
        ledger_account_id=ACCOUNT,
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
        tz_display="Europe/Berlin",
    )
    with TestClient(create_app(settings)) as c:
        html = c.get("/partials/tiles").text
    assert "<td>01.06.2026</td>" in html and "02.06.2027" in html
    assert "<td>02.06.2026</td>" not in html


def test_invalid_env_is_shown_as_badge_not_hidden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Running app:app directly (uvicorn CLI) loads settings lazily; a broken btctrader.env must be
    # visible on the page instead of silently rendering defaults.
    monkeypatch.setenv("BTCTRADER_ENV_FILE", "")
    monkeypatch.setenv("DASHBOARD_BIND", "nonsense")
    monkeypatch.setenv("LEDGER_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("ADVISOR_DIR", str(tmp_path / "advisor"))
    monkeypatch.setenv("GUARD_DIR", str(tmp_path / "guard"))
    with TestClient(create_app(None)) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "Konfiguration ungültig" in r.text and "DASHBOARD_BIND" in r.text
        assert c.app.state.settings_error  # type: ignore[attr-defined]
    with TestClient(create_app(Settings(ledger_db_path=tmp_path / "none.sqlite"))) as c:
        assert "Konfiguration ungültig" not in c.get("/").text


def test_header_names_the_effective_time_zone(tmp_path: Path) -> None:
    assert resolve_tz("Europe/Berlin").key == "Europe/Berlin" and resolve_tz("No/Such_Zone").key == "UTC"
    settings = Settings(
        ledger_db_path=tmp_path / "ledger.sqlite",
        advisor_dir=tmp_path / "advisor",
        guard_dir=tmp_path / "guard",
        tz_display="No/Such_Zone",
    )
    with TestClient(create_app(settings)) as c:
        html = c.get("/").text
    assert "Zeiten in UTC" in html and "No/Such_Zone" not in html


def test_downloaded_vendor_files_are_git_ignored() -> None:
    ignore = (STATIC_DIR / "vendor" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.js" in ignore
