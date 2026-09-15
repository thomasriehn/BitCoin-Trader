"""Market context: indicator maths, Bitvavo fetch via respx, bot status skip, stable hashing."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from btctrader.advisor import context as ctx_mod
from btctrader.common.bitvavo_public import BASE_URL, Candle
from btctrader.common.ftapi import FreqtradeClient
from tests.advisor.conftest import NOW, candles_to_api_rows, make_4h_candles, make_daily_candles


def test_market_features_shape(context: dict[str, Any]) -> None:
    m = context["market"]
    assert m["market"] == "BTC-EUR"
    assert len(m["candles_1d"]) == 30
    assert len(m["candles_4h"]) == 24
    assert m["candles_1d"][-1]["t"] == "2026-09-15"
    assert m["candles_4h"][-1]["t"].endswith("Z")
    assert m["sma50_eur"] is not None and m["sma200_eur"] is not None
    assert set(m["returns_pct"]) == {"1d", "7d", "30d", "90d"}
    assert m["drawdown_from_365d_high_pct"] <= 0
    assert m["volume_trend"] in {"rising", "falling", "flat"}
    assert m["realized_vol_30d_annualised_pct"] > 0
    assert context["as_of"] == "2026-09-15T13:07:02Z"
    assert context["bot"] == {"available": False, "reason": "not configured"}


def test_indicator_maths_on_known_series() -> None:
    closes = [100.0, 110.0, 121.0]
    assert ctx_mod.sma(closes, 3) == pytest.approx(110.333333)
    assert ctx_mod.sma(closes, 4) is None
    assert ctx_mod.trailing_return_pct(closes, 1) == pytest.approx(10.0)
    assert ctx_mod.trailing_return_pct(closes, 2) == pytest.approx(21.0)
    assert ctx_mod.trailing_return_pct(closes, 5) is None
    assert ctx_mod.pct_distance(121.0, 110.0) == pytest.approx(10.0)
    assert ctx_mod.pct_distance(121.0, None) is None
    # constant returns -> zero vol
    flat = [100.0 * 1.01**i for i in range(40)]
    assert ctx_mod.realized_vol_annualised_pct(flat, 30) == pytest.approx(0.0, abs=1e-9)


def test_drawdown_and_volume_trend() -> None:
    def c(close: str, high: str, vol: str, i: int) -> Candle:
        return Candle(i, Decimal(close), Decimal(high), Decimal(close), Decimal(close), Decimal(vol))

    candles = [c("100", "100", "10", i) for i in range(30)] + [c("50", "200", "20", 30 + i) for i in range(7)]
    high, dd = ctx_mod.drawdown_from_high(candles, 365)
    assert high == 200.0 and dd == pytest.approx(-75.0)
    ratio, label = ctx_mod.volume_trend(candles, 7, 30)
    assert ratio == pytest.approx(2.0) and label == "rising"
    assert ctx_mod.volume_trend(candles[:10], 7, 30) == (None, "unknown")


def test_insufficient_history_yields_nulls() -> None:
    daily = make_daily_candles(10)
    m = ctx_mod.market_features(daily, make_4h_candles(3))
    assert m["sma200_eur"] is None and m["dist_sma200_pct"] is None
    assert m["returns_pct"]["90d"] is None
    assert m["realized_vol_30d_annualised_pct"] is None
    assert len(m["candles_1d"]) == 10 and len(m["candles_4h"]) == 3


def test_context_hash_is_stable_across_key_order_and_runs(context: dict[str, Any]) -> None:
    h1 = ctx_mod.context_hash(context)
    reordered = json.loads(json.dumps(context, sort_keys=False))
    shuffled = {k: reordered[k] for k in reversed(list(reordered))}
    shuffled["market"] = {k: shuffled["market"][k] for k in reversed(list(shuffled["market"]))}
    assert ctx_mod.context_hash(shuffled) == h1
    assert h1.startswith("sha256:") and len(h1) == 7 + 64
    # same inputs -> same context -> same hash
    again = ctx_mod.assemble_context(make_daily_candles(), make_4h_candles(), now=NOW)
    assert ctx_mod.context_hash(again) == h1
    # any change -> different hash
    changed = json.loads(json.dumps(context))
    changed["market"]["price_eur"] = "1"
    assert ctx_mod.context_hash(changed) != h1


def test_stable_json_handles_decimal_and_is_compact() -> None:
    text = ctx_mod.stable_json({"b": Decimal("1.50"), "a": [1, {"z": "ü"}]})
    assert text == '{"a":[1,{"z":"ü"}],"b":"1.50"}'


@respx.mock
def test_build_context_fetches_bitvavo_and_skips_unreachable_bot() -> None:
    daily = make_daily_candles(400)
    fourhour = make_4h_candles(24)
    route_1d = respx.get(f"{BASE_URL}/BTC-EUR/candles", params__contains={"interval": "1d"}).mock(
        return_value=httpx.Response(200, json=candles_to_api_rows(daily))
    )
    route_4h = respx.get(f"{BASE_URL}/BTC-EUR/candles", params__contains={"interval": "4h"}).mock(
        return_value=httpx.Response(200, json=candles_to_api_rows(fourhour))
    )
    respx.post("http://ft.test/api/v1/token/login").mock(side_effect=httpx.ConnectError("down"))
    ft = FreqtradeClient("http://ft.test", "u", "p")
    ctx = ctx_mod.build_context(ft=ft, now=NOW)
    assert route_1d.called and route_4h.called
    assert route_1d.calls[0].request.url.params["limit"] == "400"
    assert ctx["market"]["candles_1d"][-1]["c"] == str(daily[-1].close)
    assert ctx["bot"]["available"] is False
    assert "down" in ctx["bot"]["reason"] or "failed" in ctx["bot"]["reason"]


@respx.mock
def test_bot_status_with_open_trade() -> None:
    respx.post("http://ft.test/api/v1/token/login").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "refresh_token": "r"})
    )
    respx.get("http://ft.test/api/v1/status").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "trade_id": 1,
                    "is_open": True,
                    "profit_pct": 2.5,
                    "profit_abs": 12.34,
                    "stake_amount": 500.0,
                }
            ],
        )
    )
    bot = ctx_mod.bot_status(FreqtradeClient("http://ft.test", "u", "p"))
    assert bot == {
        "available": True,
        "in_position": True,
        "open_trades": 1,
        "unrealized_profit_pct": 2.5,
        "unrealized_profit_eur": 12.34,
        "stake_eur": 500.0,
    }


@respx.mock
def test_bot_status_without_position() -> None:
    respx.post("http://ft.test/api/v1/token/login").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "refresh_token": "r"})
    )
    respx.get("http://ft.test/api/v1/status").mock(return_value=httpx.Response(200, json=[]))
    bot = ctx_mod.bot_status(FreqtradeClient("http://ft.test", "u", "p"))
    assert bot["available"] is True and bot["in_position"] is False and bot["open_trades"] == 0
