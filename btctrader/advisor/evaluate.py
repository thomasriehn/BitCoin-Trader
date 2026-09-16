"""Evaluate logged regime decisions against the subsequent BTC/EUR daily returns.

For every successful line of ``decisions.jsonl`` the daily close on the
decision date is joined with the close ``h`` days later for several
horizons (1 d, 7 d and the model's own ``horizon_days``). Forward returns
are measured from the close of the decision day (UTC), not from the moment
of the call: a decision made at 00:07 UTC is compared against the close
that ends that same day. Only closed daily candles are used; the still
open candle of today is dropped. A call counts as a hit when the forward
return has the sign the regime implies:

* ``risk_on``: forward return > 0
* ``risk_off``: forward return < 0
* ``neutral``: absolute forward return <= ``NEUTRAL_BAND_PCT``

Decisions whose forward window is not yet complete are skipped for that
horizon. This is a sanity check, not a backtest: shadow-mode decisions do
not influence the strategy, and a hit rate near 50 % is the expected
outcome for anything that tries to predict short-term price direction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from btctrader.advisor.schema import REGIMES
from btctrader.common import bitvavo_public
from btctrader.common.bitvavo_public import Candle
from btctrader.common.db import parse_iso

log = logging.getLogger(__name__)

NEUTRAL_BAND_PCT = 3.0
FIXED_HORIZONS = (1, 7)
MARKET = "BTC-EUR"
DAY_MS = 86_400_000
MAX_CANDLE_PAGES = 20  # 20 x 1440 days, far beyond the 10-year retention of decisions.jsonl


def read_decisions(path: str | Path, *, since: date | None = None) -> list[dict[str, Any]]:
    """All valid, error-free decision lines (oldest first), optionally from ``since`` on.

    ``raw_response`` (up to 4000 characters per line) is dropped while reading
    so a ten-year file does not sit in memory just for three fields.
    """
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("error") or obj.get("regime") not in REGIMES:
                continue
            try:
                created = parse_iso(str(obj["created_at"]))
            except (KeyError, ValueError, TypeError):
                continue
            if since is not None and created.date() < since:
                continue
            obj.pop("raw_response", None)
            rows.append(obj)
    return rows


def closes_by_date(candles: Iterable[Candle]) -> dict[date, float]:
    """Map candle date (UTC) to close price."""
    return {datetime.fromtimestamp(c.ts_ms / 1000.0, tz=UTC).date(): float(c.close) for c in candles}


def forward_return_pct(closes: dict[date, float], start: date, days: int) -> float | None:
    """Close-to-close return from ``start`` to ``start + days`` in percent, None if data is missing."""
    end = date.fromordinal(start.toordinal() + days)
    c0 = closes.get(start)
    c1 = closes.get(end)
    if c0 is None or c1 is None or c0 <= 0:
        return None
    return (c1 / c0 - 1.0) * 100.0


def is_hit(regime: str, fwd_ret_pct: float, neutral_band_pct: float = NEUTRAL_BAND_PCT) -> bool:
    if regime == "risk_on":
        return fwd_ret_pct > 0
    if regime == "risk_off":
        return fwd_ret_pct < 0
    return abs(fwd_ret_pct) <= neutral_band_pct


@dataclass
class RegimeStats:
    regime: str
    horizon: str
    n: int = 0
    hits: int = 0
    returns: list[float] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def mean_return(self) -> float | None:
        return sum(self.returns) / len(self.returns) if self.returns else None


@dataclass
class Evaluation:
    """Result table plus a baseline: share of positive forward returns over all decision dates."""

    rows: list[RegimeStats]
    decisions: int
    pending: int
    baseline_positive_share: dict[str, float | None]


def _horizons_for(decision: dict[str, Any]) -> list[tuple[str, int]]:
    out = [(f"{d}d", d) for d in FIXED_HORIZONS]
    own = decision.get("horizon_days")
    if isinstance(own, int) and 1 <= own <= 30:
        out.append(("own", own))
    return out


def evaluate_decisions(
    decisions: Sequence[dict[str, Any]],
    candles: Sequence[Candle],
    *,
    neutral_band_pct: float = NEUTRAL_BAND_PCT,
) -> Evaluation:
    """Join decisions with forward returns and aggregate hit rates per regime and horizon."""
    closes = closes_by_date(candles)
    table: dict[tuple[str, str], RegimeStats] = {}
    baseline: dict[str, list[float]] = {}
    pending = 0
    for d in decisions:
        regime = str(d["regime"])
        start = parse_iso(str(d["created_at"])).date()
        complete = False
        for label, days in _horizons_for(d):
            ret = forward_return_pct(closes, start, days)
            if ret is None:
                continue
            complete = True
            key = (regime, label)
            stats = table.setdefault(key, RegimeStats(regime=regime, horizon=label))
            stats.n += 1
            stats.returns.append(ret)
            if is_hit(regime, ret, neutral_band_pct):
                stats.hits += 1
            baseline.setdefault(label, []).append(ret)
        if not complete:
            pending += 1
    horizon_order = {"1d": 0, "7d": 1, "own": 2}
    rows = sorted(table.values(), key=lambda s: (REGIMES.index(s.regime), horizon_order.get(s.horizon, 9)))
    base = {
        label: (sum(1 for r in rets if r > 0) / len(rets) if rets else None)
        for label, rets in baseline.items()
    }
    return Evaluation(rows=rows, decisions=len(decisions), pending=pending, baseline_positive_share=base)


def format_table(ev: Evaluation) -> str:
    """Plain-text table for the terminal."""
    lines = [
        f"decisions: {ev.decisions}  (without complete forward window: {ev.pending})",
        f"{'regime':<10}{'horizon':<9}{'n':>5}{'hit rate':>10}{'mean fwd return':>18}",
    ]
    for s in ev.rows:
        hr = f"{s.hit_rate * 100:5.1f}%" if s.hit_rate is not None else "   n/a"
        mr = f"{s.mean_return:+7.2f}%" if s.mean_return is not None else "    n/a"
        lines.append(f"{s.regime:<10}{s.horizon:<9}{s.n:>5}{hr:>10}{mr:>18}")
    if ev.baseline_positive_share:
        parts = [
            f"{label}: {share * 100:.1f}%" if share is not None else f"{label}: n/a"
            for label, share in sorted(ev.baseline_positive_share.items())
        ]
        lines.append("baseline share of positive forward returns: " + ", ".join(parts))
    lines.append(f"neutral counts as hit if |return| <= {NEUTRAL_BAND_PCT:g}%")
    return "\n".join(lines)


def fetch_candles_for(
    decisions: Sequence[dict[str, Any]], *, client: httpx.Client | None = None
) -> list[Candle]:
    """Closed daily candles from two days before the first decision until now.

    Bitvavo answers ``start`` + ``limit`` with the newest ``limit`` candles at or
    after ``start`` (checked live 16.09.2026), so one call covers at most 1440
    days. The download therefore pages backwards with ``end`` until the first
    decision date is covered. If the exchange has no candle that early, a
    warning is logged and those decisions stay pending.
    """
    if not decisions:
        return []
    first = min(parse_iso(str(d["created_at"])) for d in decisions)
    start_ms = int(first.timestamp() * 1000) - 2 * DAY_MS
    pages: list[list[Candle]] = []
    end_ms: int | None = None
    for _ in range(MAX_CANDLE_PAGES):
        page = bitvavo_public.fetch_candles(
            MARKET, "1d", bitvavo_public.MAX_CANDLE_LIMIT, start_ms=start_ms, end_ms=end_ms, client=client
        )
        if not page:
            break
        pages.append(page)
        oldest = page[0].ts_ms
        if oldest <= start_ms or len(page) < bitvavo_public.MAX_CANDLE_LIMIT:
            break
        end_ms = oldest - 1
    merged = {c.ts_ms: c for page in pages for c in page}
    candles = bitvavo_public.closed_only([merged[k] for k in sorted(merged)], "1d")
    if candles and candles[0].ts_ms > int(first.timestamp() * 1000):
        log.warning(
            "oldest candle %s is later than the first decision %s; older decisions stay pending",
            datetime.fromtimestamp(candles[0].ts_ms / 1000.0, tz=UTC).date().isoformat(),
            first.date().isoformat(),
        )
    return candles
