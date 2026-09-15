"""Shared fixtures: synthetic candles, a ready context, settings pointing at a temp dir."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from btctrader.advisor import context as ctx_mod
from btctrader.common.bitvavo_public import Candle
from btctrader.common.config import Settings

DAY_MS = 86_400_000
NOW = datetime(2026, 9, 15, 13, 7, 2, tzinfo=UTC)


def make_daily_candles(n: int = 400, *, end: datetime = NOW, start_price: float = 50_000.0) -> list[Candle]:
    """Deterministic, gently trending daily candles ending on ``end``'s date (ascending)."""
    end_day = datetime(end.year, end.month, end.day, tzinfo=UTC)
    out: list[Candle] = []
    for i in range(n):
        day = end_day - timedelta(days=n - 1 - i)
        price = start_price * (1 + 0.0008 * i) * (1 + 0.02 * math.sin(i / 9.0))
        close = Decimal(f"{price:.2f}")
        out.append(
            Candle(
                ts_ms=int(day.timestamp() * 1000),
                open=close - Decimal("50"),
                high=close + Decimal("300"),
                low=close - Decimal("300"),
                close=close,
                volume=Decimal(f"{100 + 30 * math.sin(i / 5.0):.4f}"),
            )
        )
    return out


def make_4h_candles(n: int = 24, *, end: datetime = NOW) -> list[Candle]:
    base = end.replace(minute=0, second=0, microsecond=0)
    out: list[Candle] = []
    for i in range(n):
        ts = base - timedelta(hours=4 * (n - 1 - i))
        close = Decimal("60000") + Decimal(i * 10)
        out.append(
            Candle(int(ts.timestamp() * 1000), close - 5, close + 20, close - 20, close, Decimal("3.5"))
        )
    return out


def candles_to_api_rows(candles: list[Candle]) -> list[list[str | int]]:
    """Bitvavo wire format: newest first, numbers as strings."""
    rows = [[c.ts_ms, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume)] for c in candles]
    return list(reversed(rows))


@pytest.fixture
def daily() -> list[Candle]:
    return make_daily_candles()


@pytest.fixture
def fourhour() -> list[Candle]:
    return make_4h_candles()


@pytest.fixture
def context(daily: list[Candle], fourhour: list[Candle]) -> dict[str, Any]:
    return ctx_mod.assemble_context(daily, fourhour, now=NOW)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        advisor_base_url="http://vllm.test/v1",
        advisor_model="Qwen/Qwen3-14B",
        advisor_api_key="secret-token",
        advisor_mode="shadow",
        advisor_interval_hours=1.0,
        advisor_dir=tmp_path / "advisor",
        advisor_timeout_s=5.0,
    )


def good_answer(**overrides: Any) -> dict[str, Any]:
    body = {
        "regime": "neutral",
        "confidence": 0.55,
        "horizon_days": 7,
        "rationale": "Price sits 1.2% above SMA200 with flat volume; mixed signals.",
        "key_factors": ["close 1.2% above SMA200", "30d vol 48%"],
    }
    body.update(overrides)
    return body


def chat_response(content: str, *, prompt_tokens: int = 1200, completion_tokens: int = 80) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "Qwen/Qwen3-14B",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
