"""Buy-and-hold and weekly DCA benchmarks on a synthetic price series."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from btctrader.common.bitvavo_public import Candle
from btctrader.ledger.benchmarks import (
    PriceSeries,
    buy_and_hold,
    compute_benchmarks,
    load_price_series,
    weekly_dca,
)
from btctrader.ledger.errors import BenchmarkError
from btctrader.ledger.fills import q8

START = date(2026, 1, 1)
FEE = Decimal("0.0025")
CAPITAL = Decimal("1000")


def _series(days: int = 60) -> PriceSeries:
    # Linear price: 10000 + 100 x day index.
    return PriceSeries({START + timedelta(days=i): Decimal(10000 + 100 * i) for i in range(days)})


def test_buy_and_hold_with_taker_fee() -> None:
    series = _series()
    on = START + timedelta(days=30)
    btc = q8(CAPITAL * (1 - FEE) / Decimal(10000))
    assert buy_and_hold(series, START, on, CAPITAL, FEE) == btc * Decimal(13000)
    assert buy_and_hold(series, START, START, CAPITAL, FEE) == btc * Decimal(10000)
    assert buy_and_hold(series, START, START - timedelta(days=1), CAPITAL, FEE) == CAPITAL


def test_weekly_dca_with_fee_and_uninvested_remainder() -> None:
    series = _series()
    weeks = 4
    on = START + timedelta(days=15)  # buys on day 0, 7 and 14 -> 3 of 4 weeks invested
    per_week = CAPITAL / weeks
    expected_btc = sum(q8(per_week * (1 - FEE) / Decimal(10000 + 100 * d)) for d in (0, 7, 14))
    state = weekly_dca(series, START, on, CAPITAL, weeks, FEE)
    assert state.buys == 3
    assert state.btc == expected_btc
    assert state.eur_uninvested == per_week
    assert state.equity_eur == expected_btc * Decimal(11500) + per_week
    # After the last week all capital is invested and no remainder is left.
    later = weekly_dca(series, START, START + timedelta(days=40), CAPITAL, weeks, FEE)
    assert later.buys == 4 and later.eur_uninvested == 0
    # Before the start date everything is still EUR.
    before = weekly_dca(series, START, START - timedelta(days=1), CAPITAL, weeks, FEE)
    assert before.buys == 0 and before.equity_eur == CAPITAL


def test_dca_uses_last_known_close_for_missing_days() -> None:
    closes = {START: Decimal(10000), START + timedelta(days=10): Decimal(20000)}
    series = PriceSeries(closes)
    assert series.close_on(START + timedelta(days=7)) == Decimal(10000)
    state = weekly_dca(series, START, START + timedelta(days=10), CAPITAL, 2, FEE)
    assert state.buys == 2
    assert state.btc == 2 * q8(Decimal(500) * (1 - FEE) / Decimal(10000))
    with pytest.raises(BenchmarkError):
        series.close_on(START - timedelta(days=1))


def test_compute_benchmarks_and_series_from_candles() -> None:
    candles = [
        Candle(
            ts_ms=int(datetime(2026, 1, 1 + i, tzinfo=UTC).timestamp() * 1000),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(10000 + i * 1000),
            volume=Decimal(1),
        )
        for i in range(10)
    ]
    series = PriceSeries.from_candles(candles)
    assert series.first_date == START and series.last_date == date(2026, 1, 10)
    bench = compute_benchmarks(series, START, date(2026, 1, 8), CAPITAL, 52, FEE)
    assert bench.bh_equity_eur == q8(CAPITAL * (1 - FEE) / 10000) * Decimal(17000)
    assert bench.dca.buys == 2
    assert bench.dca_equity_eur == bench.dca.btc * Decimal(17000) + bench.dca.eur_uninvested


def test_load_price_series_pages_bitvavo_candles(respx_mock) -> None:  # type: ignore[no-untyped-def]
    import httpx

    day_ms = 86_400_000

    def responder(request: httpx.Request) -> httpx.Response:
        # Like Bitvavo: newest candles of [start, end] first, at most ``limit`` of them.
        start = int(request.url.params["start"])
        end = int(request.url.params["end"])
        limit = int(request.url.params["limit"])
        assert end - start >= day_ms
        first_day = -(-start // day_ms) * day_ms
        rows = []
        ts = first_day
        while ts + day_ms <= end + 1:
            rows.append([ts, "1", "1", "1", str(ts // day_ms), "1"])
            ts += day_ms
        return httpx.Response(200, json=list(reversed(rows))[:limit])

    route = respx_mock.get("https://api.bitvavo.com/v2/BTC-EUR/candles").mock(side_effect=responder)
    start, end = date(2022, 1, 1), date(2026, 9, 14)
    series = load_price_series(start, end)
    assert route.call_count == 2  # ~1720 days need two pages of 1440
    assert series.first_date == start - timedelta(days=7)
    assert series.last_date == end + timedelta(days=1)
    assert len(series) == (series.last_date - series.first_date).days + 1
    expected_ts = datetime(2024, 3, 3, tzinfo=UTC).timestamp() * 1000
    assert series.close_on(date(2024, 3, 3)) == Decimal(int(expected_ts // day_ms))
