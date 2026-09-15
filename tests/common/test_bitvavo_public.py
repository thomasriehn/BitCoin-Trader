"""Tests for btctrader.common.bitvavo_public (respx-mocked)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from btctrader.common.bitvavo_public import (
    BASE_URL,
    BitvavoError,
    Candle,
    fetch_book_top,
    fetch_candles,
    fetch_market_info,
    fetch_ticker_price,
    parse_candles,
)

# Real shape from the API (newest first, numbers as strings)
RAW_CANDLES = [
    [1789430400000, "67665", "67746", "64910", "65826", "1112.42837282"],
    [1789344000000, "66290", "68923", "65915", "67682", "684.38663782"],
    [1789257600000, "66664", "66796", "65942", "66268", "214.0493407"],
]


@respx.mock
def test_fetch_candles_parses_decimal_ascending() -> None:
    route = respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(return_value=httpx.Response(200, json=RAW_CANDLES))
    candles = fetch_candles("BTC-EUR", "1d", 3, client=httpx.Client())
    assert route.calls[0].request.url.params["interval"] == "1d"
    assert route.calls[0].request.url.params["limit"] == "3"
    assert [c.ts_ms for c in candles] == [1789257600000, 1789344000000, 1789430400000]
    first = candles[0]
    assert isinstance(first, Candle)
    assert first.open == Decimal("66664")
    assert first.volume == Decimal("214.0493407")
    assert isinstance(first.close, Decimal) and not isinstance(first.close, float)
    assert candles[-1].close == Decimal("65826")


@respx.mock
def test_fetch_candles_passes_start_end_and_caps_limit() -> None:
    route = respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(return_value=httpx.Response(200, json=[]))
    assert fetch_candles(limit=5000, start_ms=1700000000000, end_ms=1700200000000) == []
    params = route.calls[0].request.url.params
    assert params["limit"] == "1440"
    assert params["start"] == "1700000000000"
    assert params["end"] == "1700200000000"


def test_fetch_candles_rejects_bad_interval() -> None:
    with pytest.raises(ValueError):
        fetch_candles(interval="3d")


def test_parse_candles_rejects_short_rows() -> None:
    with pytest.raises(BitvavoError):
        parse_candles([[1, "2", "3"]])


@respx.mock
def test_fetch_ticker_price() -> None:
    respx.get(f"{BASE_URL}/ticker/price").mock(
        return_value=httpx.Response(200, json={"market": "BTC-EUR", "price": "65827"})
    )
    assert fetch_ticker_price(client=httpx.Client()) == Decimal("65827")


@respx.mock
def test_fetch_book_top() -> None:
    respx.get(f"{BASE_URL}/ticker/book").mock(
        return_value=httpx.Response(
            200,
            json={"market": "BTC-EUR", "bid": "65814", "bidSize": "0.15", "ask": "65815", "askSize": "0.19"},
        )
    )
    assert fetch_book_top() == (Decimal("65814"), Decimal("65815"))


@respx.mock
def test_fetch_market_info() -> None:
    route = respx.get(f"{BASE_URL}/markets").mock(
        return_value=httpx.Response(
            200, json={"market": "BTC-EUR", "status": "trading", "minOrderInQuoteAsset": "5.00"}
        )
    )
    info = fetch_market_info()
    assert route.calls[0].request.url.params["market"] == "BTC-EUR"
    assert info["minOrderInQuoteAsset"] == "5.00"


@respx.mock
def test_fetch_market_info_accepts_list_payload() -> None:
    respx.get(f"{BASE_URL}/markets").mock(
        return_value=httpx.Response(200, json=[{"market": "ETH-EUR"}, {"market": "BTC-EUR", "status": "x"}])
    )
    assert fetch_market_info()["status"] == "x"


@respx.mock
def test_api_error_payload_raises() -> None:
    respx.get(f"{BASE_URL}/ticker/price").mock(
        return_value=httpx.Response(400, json={"errorCode": 205, "error": "Market does not exist."})
    )
    with pytest.raises(BitvavoError) as exc:
        fetch_ticker_price("XXX-EUR")
    assert exc.value.status_code == 400
    assert "Market does not exist" in str(exc.value)


@respx.mock
def test_transport_error_raises() -> None:
    respx.get(f"{BASE_URL}/ticker/book").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BitvavoError):
        fetch_book_top()
