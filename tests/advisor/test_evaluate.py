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


def _daily_rows_ending_today(days: int) -> list[list[str | int]]:
    """Bitvavo wire rows (newest first) for ``days`` daily candles, the newest being today's open one."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[list[str | int]] = []
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        c = f"{100 + i * 0.01:.2f}"
        rows.append([int(day.timestamp() * 1000), c, c, c, c, "1"])
    return list(reversed(rows))


def _bitvavo_like(rows: list[list[str | int]]) -> object:
    """Mimic the live API: newest ``limit`` rows with start <= ts <= end (checked live 16.09.2026)."""
    import httpx

    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        limit = int(params["limit"])
        start = int(params.get("start", 0))
        end = int(params.get("end", 2**62))
        selected = [r for r in rows if start <= int(r[0]) <= end]  # rows are newest first
        return httpx.Response(200, json=selected[:limit])

    return respond


def test_fetch_candles_pages_backwards_and_drops_open_candle(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import respx

    from btctrader.common.bitvavo_public import BASE_URL

    rows = _daily_rows_ending_today(3000)
    first_day = datetime.fromtimestamp(int(rows[-1][0]) / 1000, tz=UTC).date()
    first_decision = first_day + timedelta(days=100)
    decisions = [_decision(first_decision, "risk_on"), _decision(first_day + timedelta(days=2900), "neutral")]
    with respx.mock, caplog.at_level(logging.WARNING):
        route = respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(side_effect=_bitvavo_like(rows))
        candles = ev.fetch_candles_for(decisions)
    # about 2900 days are needed: three pages of at most 1440, each one ending before the previous one
    assert route.call_count == 3
    params = [c.request.url.params for c in route.calls]
    assert "end" not in params[0] and all(p["limit"] == "1440" for p in params)
    assert int(params[2]["end"]) < int(params[1]["end"])
    days = [datetime.fromtimestamp(c.ts_ms / 1000, tz=UTC).date() for c in candles]
    assert days == sorted(set(days)), "ascending, no duplicates"
    assert days[0] == first_decision - timedelta(days=1)
    assert days[-1] == datetime.now(UTC).date() - timedelta(days=1), "today's open candle is dropped"
    assert not [r for r in caplog.records if "stay pending" in r.getMessage()]
    result = ev.evaluate_decisions(decisions, candles)
    assert result.pending == 0


def test_fetch_candles_warns_when_history_is_too_short(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import respx

    from btctrader.common.bitvavo_public import BASE_URL

    rows = _daily_rows_ending_today(30)
    decisions = [_decision(date(2019, 1, 1), "risk_on")]
    with respx.mock, caplog.at_level(logging.WARNING):
        respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(side_effect=_bitvavo_like(rows))
        candles = ev.fetch_candles_for(decisions)
    assert len(candles) == 29
    assert any("2019-01-01" in r.getMessage() and "stay pending" in r.getMessage() for r in caplog.records)
    assert ev.evaluate_decisions(decisions, candles).pending == 1


def test_read_decisions_drops_raw_response(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text(json.dumps(_decision(date(2026, 9, 1), "risk_on", raw_response="x" * 4000)) + "\n")
    rows = ev.read_decisions(log_path)
    assert len(rows) == 1 and "raw_response" not in rows[0] and rows[0]["regime"] == "risk_on"
