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
