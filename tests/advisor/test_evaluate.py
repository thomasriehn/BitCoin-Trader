"""Evaluate: join synthetic decisions with synthetic daily closes, hit rates per regime."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from btctrader.advisor import evaluate as ev
from btctrader.common.bitvavo_public import Candle


def _candles(closes: dict[date, float]) -> list[Candle]:
    out = []
    for day, close in sorted(closes.items()):
        ts = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        c = Decimal(str(close))
        out.append(Candle(ts, c, c, c, c, Decimal("1")))
    return out


def _decision(day: date, regime: str, horizon: int = 7, **extra: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "decision_id": f"{day.isoformat()}T12:00:00Z-abc123",
        "created_at": f"{day.isoformat()}T12:00:00Z",
        "regime": regime,
        "confidence": 0.6,
        "horizon_days": horizon,
        "error": None,
        **extra,
    }


def test_forward_return_and_hits() -> None:
    d0 = date(2026, 9, 1)
    closes = {d0 + timedelta(days=i): 100.0 * (1.01**i) for i in range(20)}
    assert ev.forward_return_pct(closes, d0, 1) == pytest.approx(1.0)
    assert ev.forward_return_pct(closes, d0, 30) is None
    assert ev.is_hit("risk_on", 0.5) and not ev.is_hit("risk_on", -0.5)
    assert ev.is_hit("risk_off", -0.5) and not ev.is_hit("risk_off", 0.5)
    assert ev.is_hit("neutral", 2.9) and not ev.is_hit("neutral", -3.1)


def test_evaluate_synthetic_decisions(tmp_path: Path) -> None:
    d0 = date(2026, 9, 1)
    # up for 10 days, then down for 10 days, then flat
    closes: dict[date, float] = {}
    price = 100.0
    for i in range(30):
        if i < 10:
            price *= 1.02
        elif i < 20:
            price *= 0.98
        closes[d0 + timedelta(days=i)] = round(price, 4)
    candles = _candles(closes)

    decisions = [
        _decision(d0, "risk_on", 7),  # up phase -> hit
        _decision(d0 + timedelta(days=2), "risk_off", 3),  # up phase -> miss
        _decision(d0 + timedelta(days=10), "risk_off", 5),  # down phase -> hit
        _decision(d0 + timedelta(days=12), "risk_on", 2),  # down phase -> miss on 1d/7d/own
        _decision(d0 + timedelta(days=21), "neutral", 5),  # flat -> hit
        _decision(d0 + timedelta(days=29), "neutral", 5),  # no forward data -> pending
    ]
    log_path = tmp_path / "decisions.jsonl"
    with log_path.open("w") as fh:
        for d in decisions:
            fh.write(json.dumps(d) + "\n")
        fh.write(
            json.dumps({"created_at": "2026-09-03T00:00:00Z", "error": "timeout", "regime": None}) + "\n"
        )
        fh.write("{truncated\n")

    rows = ev.read_decisions(log_path)
    assert len(rows) == 6  # error and broken lines skipped
    assert len(ev.read_decisions(log_path, since=date(2026, 9, 11))) == 4

    result = ev.evaluate_decisions(rows, candles)
    assert result.decisions == 6 and result.pending == 1
    table = {(r.regime, r.horizon): r for r in result.rows}
    assert table[("risk_on", "1d")].n == 2 and table[("risk_on", "1d")].hits == 1
    assert table[("risk_on", "7d")].hit_rate == 0.5
    assert table[("risk_on", "own")].returns[0] == pytest.approx((1.02**7 - 1) * 100, rel=1e-4)
    assert table[("risk_off", "1d")].hits == 1 and table[("risk_off", "1d")].n == 2
    assert table[("neutral", "7d")].n == 1 and table[("neutral", "7d")].hits == 1
    assert table[("neutral", "own")].mean_return == pytest.approx(0.0)
    assert [r.regime for r in result.rows][:3] == ["risk_on", "risk_on", "risk_on"]
    assert result.baseline_positive_share["1d"] == pytest.approx(2 / 5)

    text = ev.format_table(result)
    assert "decisions: 6" in text and "risk_on" in text and "hit rate" in text
    assert "without complete forward window: 1" in text
    assert "50.0%" in text


def test_evaluate_empty() -> None:
    result = ev.evaluate_decisions([], [])
    assert result.rows == [] and result.decisions == 0
    assert "decisions: 0" in ev.format_table(result)
