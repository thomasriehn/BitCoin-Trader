"""CLI: --dry-run prints the request, run exit codes, context and evaluate subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from btctrader.advisor import cli
from btctrader.common.bitvavo_public import BASE_URL
from btctrader.common.jsonl import read_json
from tests.advisor.conftest import (
    candles_to_api_rows,
    chat_response,
    good_answer,
    make_4h_candles,
    make_daily_candles,
)

URL = "http://vllm.test/v1/chat/completions"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key in list(__import__("os").environ):
        if key.startswith(("ADVISOR_", "FT_", "BTCTRADER_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ADVISOR_BASE_URL", "http://vllm.test/v1")
    monkeypatch.setenv("ADVISOR_MODEL", "Qwen/Qwen3-14B")
    monkeypatch.setenv("ADVISOR_API_KEY", "secret-token")
    monkeypatch.setenv("ADVISOR_DIR", str(tmp_path / "advisor"))
    monkeypatch.setenv("ADVISOR_TIMEOUT_S", "5")
    # keep /etc/freqtrade/btctrader.env (if present on the dev box) out of the test
    (tmp_path / "empty.env").write_text("")
    monkeypatch.setenv("BTCTRADER_ENV_FILE", str(tmp_path / "empty.env"))
    return tmp_path


@pytest.fixture
def context_file(tmp_path: Path, context: dict[str, Any]) -> Path:
    p = tmp_path / "ctx.json"
    p.write_text(json.dumps(context))
    return p


def test_dry_run_prints_request_without_calling_server(
    env: Path, context_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(URL)
        rc = cli.main(["run", "--dry-run", "--context-file", str(context_file)])
    assert rc == 0
    assert not route.called
    out = json.loads(capsys.readouterr().out)
    assert out["url"] == URL
    assert out["headers"] == {"Authorization": "Bearer ***"}
    assert "secret-token" not in json.dumps(out)
    body = out["body"]
    assert body["model"] == "Qwen/Qwen3-14B" and body["seed"] == 42 and body["temperature"] == 0
    assert body["response_format"]["json_schema"]["name"] == "regime_decision"
    assert body["messages"][0]["role"] == "system"
    assert "guided_json" in out["fallback_body_on_400"]
    assert not (env / "advisor" / "decision.json").exists()


def test_run_writes_decision_and_returns_zero(env: Path, context_file: Path) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer()))))
        rc = cli.main(["run", "--context-file", str(context_file), "--no-bot"])
    assert rc == 0
    doc = read_json(env / "advisor" / "decision.json")
    assert doc is not None and doc["regime"] == "neutral" and doc["mode"] == "shadow"


def test_run_returns_one_on_server_error(env: Path, context_file: Path) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(503, text="overloaded"))
        rc = cli.main(["run", "--context-file", str(context_file), "--no-bot"])
    assert rc == 1
    assert not (env / "advisor" / "decision.json").exists()


def test_run_requires_model(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADVISOR_MODEL", "")
    assert cli.main(["run", "--dry-run"]) == 2


def test_context_subcommand_prints_json(
    env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with respx.mock:
        respx.get(f"{BASE_URL}/BTC-EUR/candles", params__contains={"interval": "1d"}).mock(
            return_value=httpx.Response(200, json=candles_to_api_rows(make_daily_candles(400)))
        )
        respx.get(f"{BASE_URL}/BTC-EUR/candles", params__contains={"interval": "4h"}).mock(
            return_value=httpx.Response(200, json=candles_to_api_rows(make_4h_candles()))
        )
        rc = cli.main(["context", "--no-bot", "--out", str(tmp_path / "out" / "ctx.json")])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["market"]["market"] == "BTC-EUR" and len(printed["market"]["candles_1d"]) == 30
    assert read_json(tmp_path / "out" / "ctx.json") == printed


def test_evaluate_without_decisions(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["evaluate", "--decisions", str(env / "none.jsonl")])
    assert rc == 0
    assert "no decisions found" in capsys.readouterr().out


def test_run_with_invalid_answer_keeps_existing_decision(env: Path, context_file: Path) -> None:
    from btctrader.common.jsonl import atomic_write_json

    decision_path = env / "advisor" / "decision.json"
    old = {
        "schema_version": 1,
        "decision_id": "old",
        "regime": "risk_off",
        "created_at": "2026-09-14T00:00:00Z",
    }
    atomic_write_json(decision_path, old)
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer(confidence=2.0))))
        )
        rc = cli.main(["run", "--context-file", str(context_file), "--no-bot"])
    assert rc == 1
    assert read_json(decision_path) == old
    # atomic write leaves no temp files behind, neither on success nor on failure
    leftovers = [p.name for p in (env / "advisor").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_run_success_leaves_no_temp_files(env: Path, context_file: Path) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer()))))
        assert cli.main(["run", "--context-file", str(context_file), "--no-bot"]) == 0
    names = sorted(p.name for p in (env / "advisor").iterdir())
    assert names == ["context-latest.json", "decision.json", "decisions.jsonl"]


def _write_synthetic_decisions(path: Path) -> None:
    rows = [
        ("2026-09-01T12:00:00Z", "risk_on", 3),
        ("2026-09-02T12:00:00Z", "risk_on", 3),
        ("2026-09-05T12:00:00Z", "risk_off", 2),
        ("2026-09-08T12:00:00Z", "neutral", 5),
    ]
    with path.open("w", encoding="utf-8") as fh:
        for created, regime, horizon in rows:
            fh.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "decision_id": f"{created}-abc123",
                        "created_at": created,
                        "regime": regime,
                        "confidence": 0.6,
                        "horizon_days": horizon,
                        "error": None,
                    }
                )
                + "\n"
            )
        fh.write(json.dumps({"created_at": "2026-09-03T12:00:00Z", "error": "timeout"}) + "\n")


def _daily_rows_rising(first: str, days: int) -> list[list[str | int]]:
    """Bitvavo wire format (newest first): a series rising 1 % per day from ``first``."""
    from datetime import UTC, datetime, timedelta

    start = datetime.fromisoformat(first).replace(tzinfo=UTC)
    rows: list[list[str | int]] = []
    for i in range(days):
        day = start + timedelta(days=i)
        close = 100.0 * (1.01**i)
        c = f"{close:.2f}"
        rows.append([int(day.timestamp() * 1000), c, c, c, c, "1"])
    return list(reversed(rows))


def test_evaluate_on_synthetic_file(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_path = env / "decisions.jsonl"
    _write_synthetic_decisions(log_path)
    with respx.mock:
        route = respx.get(f"{BASE_URL}/BTC-EUR/candles", params__contains={"interval": "1d"}).mock(
            return_value=httpx.Response(200, json=_daily_rows_rising("2026-08-30", 20))
        )
        rc = cli.main(["evaluate", "--decisions", str(log_path)])
    assert rc == 0 and route.called
    out = capsys.readouterr().out
    assert "decisions: 4" in out and "risk_on" in out and "risk_off" in out and "hit rate" in out

    with respx.mock:
        respx.get(f"{BASE_URL}/BTC-EUR/candles", params__contains={"interval": "1d"}).mock(
            return_value=httpx.Response(200, json=_daily_rows_rising("2026-08-30", 20))
        )
        rc = cli.main(["evaluate", "--decisions", str(log_path), "--since", "2026-09-05", "--json"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decisions"] == 2  # since filter drops the two September 1/2 decisions
    rows = {(r["regime"], r["horizon"]): r for r in result["rows"]}
    # rising series: risk_off is always a miss, neutral (|ret| <= 3 %) hits on 1d
    assert rows[("risk_off", "1d")]["hit_rate"] == 0.0
    assert rows[("neutral", "1d")]["hit_rate"] == 1.0
    # prices are rounded to cents on the wire, so allow 1 % relative tolerance
    assert rows[("neutral", "1d")]["mean_fwd_return_pct"] == pytest.approx(1.0, rel=1e-2)


def test_advisor_package_has_no_exchange_keys() -> None:
    """Section 6: the advisor has no exchange keys and never calls order endpoints."""
    import btctrader.advisor as pkg

    root = Path(pkg.__file__).parent
    forbidden = (
        "bitvavo_api_key",
        "bitvavo_api_secret",
        "ccxt",
        "/order",
        "create_order",
        # Freqtrade control endpoints: the advisor only ever gets the status() method
        "forceexit",
        "stopentry",
        ".start(",
        ".stop(",
    )
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{source.name} mentions {needle!r}"
