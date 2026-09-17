"""Buy-and-hold and virtual weekly DCA benchmarks on daily closes.

* ``bh_equity(date) = START_CAPITAL x (1 - fee_taker) / close(BENCHMARK_START) x close(date)``
  (one fictitious taker buy on day 0).
* ``dca_equity(date)``: every 7 days from ``BENCHMARK_START`` for ``DCA_WEEKS`` weeks a
  fictitious taker buy of ``START_CAPITAL / DCA_WEEKS`` at that day's close; the
  not yet invested remainder stays as EUR.

Closes come from ``btctrader.common.bitvavo_public`` daily candles or from a
candle list / mapping passed in. A missing day uses the last close before it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from btctrader.common.bitvavo_public import MAX_CANDLE_LIMIT, Candle, fetch_candles
from btctrader.ledger.errors import BenchmarkError
from btctrader.ledger.fills import q8

ONE = Decimal(1)
ZERO = Decimal(0)
DAY_MS = 86_400_000


def candle_date(candle: Candle) -> date:
    return datetime.fromtimestamp(candle.ts_ms / 1000, tz=UTC).date()


class PriceSeries:
    """Daily closes keyed by UTC date with 'last known close' lookup."""

    def __init__(self, closes: Mapping[date, Decimal]) -> None:
        self._dates: list[date] = sorted(closes)
        self._closes: dict[date, Decimal] = {d: Decimal(closes[d]) for d in self._dates}

    @classmethod
    def from_candles(cls, candles: Iterable[Candle]) -> PriceSeries:
        return cls({candle_date(c): c.close for c in candles})

    def __len__(self) -> int:
        return len(self._dates)

    @property
    def first_date(self) -> date | None:
        return self._dates[0] if self._dates else None

    @property
    def last_date(self) -> date | None:
        return self._dates[-1] if self._dates else None

    def close_on(self, day: date) -> Decimal:
        """Close of ``day`` or, when missing, of the latest earlier day. Raises if none exists."""
        exact = self._closes.get(day)
        if exact is not None:
            return exact
        # Binary search for the latest date <= day.
        lo, hi = 0, len(self._dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._dates[mid] <= day:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            raise BenchmarkError(f"no close on or before {day.isoformat()} (series starts {self.first_date})")
        return self._closes[self._dates[lo - 1]]


@dataclass(frozen=True, slots=True)
class DcaState:
    equity_eur: Decimal
    btc: Decimal
    eur_uninvested: Decimal
    buys: int


@dataclass(frozen=True, slots=True)
class Benchmarks:
    bh_equity_eur: Decimal
    dca_equity_eur: Decimal
    bh_btc: Decimal
    dca: DcaState


def buy_and_hold(series: PriceSeries, start: date, on: date, capital: Decimal, fee_taker: Decimal) -> Decimal:
    """Equity of one fictitious taker buy of ``capital`` at the close of ``start``, valued at ``on``."""
    if on < start:
        return capital
    btc = buy_and_hold_btc(series, start, capital, fee_taker)
    return btc * series.close_on(on)


def buy_and_hold_btc(series: PriceSeries, start: date, capital: Decimal, fee_taker: Decimal) -> Decimal:
    return q8(capital * (ONE - fee_taker) / series.close_on(start))


def weekly_dca(
    series: PriceSeries, start: date, on: date, capital: Decimal, weeks: int, fee_taker: Decimal
) -> DcaState:
    """Virtual weekly DCA of ``capital / weeks`` per week (taker fee), valued at the close of ``on``."""
    if weeks < 1:
        raise BenchmarkError("DCA_WEEKS must be >= 1")
    per_week = q8(capital / Decimal(weeks))
    btc = ZERO
    invested = ZERO
    buys = 0
    for week in range(weeks):
        day = start + timedelta(days=7 * week)
        if day > on:
            break
        # The last instalment absorbs the rounding so that all weeks together invest exactly ``capital``.
        amount = per_week if week < weeks - 1 else capital - per_week * (weeks - 1)
        btc += q8(amount * (ONE - fee_taker) / series.close_on(day))
        invested += amount
        buys += 1
    uninvested = capital - invested
    equity = uninvested if on < start else btc * series.close_on(on) + uninvested
    return DcaState(equity_eur=equity, btc=btc, eur_uninvested=uninvested, buys=buys)


def compute_benchmarks(
    series: PriceSeries,
    start: date,
    on: date,
    capital: Decimal,
    weeks: int,
    fee_taker: Decimal,
) -> Benchmarks:
    dca = weekly_dca(series, start, on, capital, weeks, fee_taker)
    bh_btc = buy_and_hold_btc(series, start, capital, fee_taker) if on >= start else ZERO
    return Benchmarks(
        bh_equity_eur=buy_and_hold(series, start, on, capital, fee_taker),
        dca_equity_eur=dca.equity_eur,
        bh_btc=bh_btc,
        dca=dca,
    )


def load_price_series(
    start: date,
    end: date,
    *,
    market: str = "BTC-EUR",
    client: httpx.Client | None = None,
    max_pages: int = 20,
) -> PriceSeries:
    """Fetch daily closes from ``start - 7 days`` to ``end`` from the public Bitvavo API.

    Checked live on 15.09.2026: a window holding more than ``limit`` candles returns the
    *newest* ones, so pages walk backwards by moving ``end`` below the oldest candle
    received. ``end`` excludes the candle that closes after it, hence one day of slack.
    A window shorter than one interval is rejected by the API (error 205) and skipped.
    """
    first = start - timedelta(days=7)
    start_ms = _midnight_ms(first)
    end_ms = _midnight_ms(end + timedelta(days=1)) + DAY_MS - 1
    candles: list[Candle] = []
    cursor_end = end_ms
    for _ in range(max_pages):
        if cursor_end - start_ms < DAY_MS:
            break
        page = fetch_candles(
            market, "1d", MAX_CANDLE_LIMIT, start_ms=start_ms, end_ms=cursor_end, client=client
        )
        if not page:
            break
        candles.extend(page)
        oldest = page[0].ts_ms
        if len(page) < MAX_CANDLE_LIMIT or oldest <= start_ms:
            break
        cursor_end = oldest - 1
    series = PriceSeries.from_candles(candles)
    if len(series) == 0:
        raise BenchmarkError(f"no daily candles between {first} and {end}")
    return series


def _midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
