"""Model call, guided_json fallback, validation failures, timeouts, file writes."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from btctrader.advisor import advisor
from btctrader.common.config import Settings
from btctrader.common.jsonl import read_json, read_jsonl_tail
from tests.advisor.conftest import NOW, chat_response, good_answer

URL = "http://vllm.test/v1/chat/completions"


def _files(settings: Settings) -> tuple[Path, Path, Path]:
    d = Path(settings.advisor_dir)
    return d / advisor.DECISION_FILE, d / advisor.DECISIONS_LOG, d / advisor.CONTEXT_FILE


def test_build_request_matches_contract(context: dict[str, Any]) -> None:
    req = advisor.build_request("Qwen/Qwen3-14B", "SYSTEM", context)
    assert req["model"] == "Qwen/Qwen3-14B"
    assert req["temperature"] == 0 and req["seed"] == 42 and req["max_tokens"] == 400
    assert req["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert req["messages"][1]["role"] == "user"
    assert json.loads(req["messages"][1]["content"].split("\n", 1)[1]) == context
    rf = req["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "regime_decision"
    assert rf["json_schema"]["strict"] is True
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    fb = advisor.build_fallback_request(req)
    assert "response_format" not in fb and fb["guided_json"] == rf["json_schema"]["schema"]
    assert fb["messages"] == req["messages"]


def test_system_prompt_ships_with_package() -> None:
    text = advisor.load_system_prompt()
    assert "risk_on" in text and "risk_off" in text and "neutral" in text
    assert "leverage" in text


@respx.mock
def test_successful_call_writes_both_files(settings: Settings, context: dict[str, Any]) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer())))
    )
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 0 and outcome.error is None
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["type"] == "json_schema"
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret-token"

    decision_file, log_file, context_file = _files(settings)
    doc = read_json(decision_file)
    assert doc is not None
    assert doc["schema_version"] == 1
    assert doc["mode"] == "shadow"
    assert doc["model"] == "Qwen/Qwen3-14B"
    assert doc["created_at"] == "2026-09-15T13:07:02Z"
    assert doc["valid_until"] == "2026-09-15T15:07:02Z"  # now + 2 x 1 h
    assert doc["regime"] == "neutral" and doc["confidence"] == 0.55 and doc["horizon_days"] == 7
    assert doc["key_factors"] == ["close 1.2% above SMA200", "30d vol 48%"]
    assert doc["context_hash"].startswith("sha256:") and doc["prompt_hash"].startswith("sha256:")
    assert doc["decision_id"] == "2026-09-15T13:07:02Z-" + doc["context_hash"][7:13]
    assert set(doc) == {
        "schema_version", "decision_id", "created_at", "valid_until", "mode", "model", "regime",
        "confidence", "horizon_days", "rationale", "key_factors", "context_hash", "prompt_hash",
    }  # fmt: skip

    lines = read_jsonl_tail(log_file)
    assert len(lines) == 1
    line = lines[0]
    for key in doc:
        assert line[key] == doc[key]
    assert line["error"] is None
    assert line["usage"] == {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280}
    assert isinstance(line["latency_ms"], int)
    assert json.loads(line["raw_response"]) == good_answer()
    assert line["used_guided_json_fallback"] is False
    assert read_json(context_file) == context


def test_valid_until_uses_interval_and_mode_is_copied(settings: Settings, context: dict[str, Any]) -> None:
    s = Settings(**{**settings.__dict__, "advisor_interval_hours": 2.5, "advisor_mode": "gate"})
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer()))))
        outcome = advisor.run_advisor(s, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 0
    doc = read_json(_files(s)[0])
    assert doc is not None and doc["mode"] == "gate"
    assert doc["valid_until"] == advisor.iso_utc(NOW + timedelta(hours=5))


@respx.mock
def test_400_on_response_format_triggers_guided_json_retry(
    settings: Settings, context: dict[str, Any]
) -> None:
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(
            400,
            json={"error": {"message": "[{'type': 'extra_forbidden', 'loc': ('body', 'response_format')}]"}},
        ),
        httpx.Response(200, json=chat_response(json.dumps(good_answer(regime="risk_on")))),
    ]
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 0
    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert "response_format" in first and "guided_json" not in first
    assert "response_format" not in second and second["guided_json"]["type"] == "object"
    assert second["messages"] == first["messages"] and second["seed"] == 42
    doc = read_json(_files(settings)[0])
    assert doc is not None and doc["regime"] == "risk_on"
    assert read_jsonl_tail(_files(settings)[1])[0]["used_guided_json_fallback"] is True


@respx.mock
def test_400_unrelated_to_response_format_is_not_retried(settings: Settings, context: dict[str, Any]) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(400, json={"error": "model not found"}))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1 and route.call_count == 1
    assert "HTTP 400" in (outcome.error or "")


@respx.mock
def test_fallback_that_fails_again_is_an_error(settings: Settings, context: dict[str, Any]) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(400, json={"error": "guided_json unsupported"}))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1 and route.call_count == 2
    assert not _files(settings)[0].exists()


def _seed_existing_decision(settings: Settings) -> dict[str, Any]:
    from btctrader.common.jsonl import atomic_write_json

    old = {
        "schema_version": 1,
        "decision_id": "old",
        "regime": "risk_off",
        "created_at": "2026-09-14T00:00:00Z",
    }
    atomic_write_json(_files(settings)[0], old)
    return old


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("this is not json {", "not valid JSON"),
        (json.dumps(good_answer(confidence=1.7)), "confidence"),
        (json.dumps(good_answer(confidence=-0.2)), "confidence"),
        (json.dumps(good_answer(regime="moon")), "regime"),
        (json.dumps(good_answer(horizon_days=45)), "horizon_days"),
        (json.dumps({"regime": "neutral"}), "confidence"),
        ("", "empty answer"),
    ],
)
def test_invalid_answer_leaves_decision_json_unchanged_and_logs_error(
    settings: Settings,
    context: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    content: str,
    fragment: str,
) -> None:
    old = _seed_existing_decision(settings)
    with respx.mock, caplog.at_level(logging.ERROR, logger="btctrader.advisor.advisor"):
        respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(content)))
        outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1
    assert fragment in (outcome.error or "")
    assert read_json(_files(settings)[0]) == old
    assert any("error=" in r.getMessage() and fragment in r.getMessage() for r in caplog.records)
    lines = read_jsonl_tail(_files(settings)[1])
    assert len(lines) == 1
    assert fragment in lines[0]["error"]
    assert lines[0]["raw_response"] == content[:4000]
    assert lines[0]["context_hash"].startswith("sha256:")
    assert "regime" not in lines[0]


@respx.mock
def test_timeout_is_handled(
    settings: Settings, context: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    old = _seed_existing_decision(settings)
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("read timed out"))
    with caplog.at_level(logging.ERROR):
        outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1
    assert "timeout after 5s" in (outcome.error or "")
    assert read_json(_files(settings)[0]) == old
    line = read_jsonl_tail(_files(settings)[1])[0]
    assert "timeout" in line["error"] and "raw_response" not in line
    assert any("timeout" in r.getMessage() for r in caplog.records)


@respx.mock
def test_server_unreachable(settings: Settings, context: dict[str, Any]) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("connection refused"))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1 and "ConnectError" in (outcome.error or "")
    assert not _files(settings)[0].exists()


@respx.mock
def test_context_failure_is_logged_and_no_decision_written(settings: Settings) -> None:
    from btctrader.common.bitvavo_public import BASE_URL

    respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(
        return_value=httpx.Response(500, json={"errorCode": 500, "error": "boom"})
    )
    outcome = advisor.run_advisor(settings, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1 and "context:" in (outcome.error or "")
    assert not _files(settings)[0].exists()
    line = read_jsonl_tail(_files(settings)[1])[0]
    assert line["error"].startswith("context:") and line["decision_id"] is None


@respx.mock
def test_answer_with_code_fence_and_think_block_is_accepted(
    settings: Settings, context: dict[str, Any]
) -> None:
    content = "<think>\nhmm\n</think>\n```json\n" + json.dumps(good_answer(regime="risk_off")) + "\n```"
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(content)))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 0
    assert outcome.decision is not None and outcome.decision["regime"] == "risk_off"


@respx.mock
def test_raw_response_is_truncated_to_4000_chars(settings: Settings, context: dict[str, Any]) -> None:
    content = "x" * 10_000
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(content)))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1
    assert len(read_jsonl_tail(_files(settings)[1])[0]["raw_response"]) == 4000


def test_no_authorization_header_without_api_key(settings: Settings, context: dict[str, Any]) -> None:
    s = Settings(**{**settings.__dict__, "advisor_api_key": ""})
    with respx.mock:
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer())))
        )
        assert advisor.run_advisor(s, context=context, system_prompt="SYS", now=NOW).exit_code == 0
    assert "Authorization" not in route.calls[0].request.headers


def test_production_timestamps_have_whole_seconds(settings: Settings, context: dict[str, Any]) -> None:
    """Section 6 shows ``2026-09-15T13:07:02Z``; a run without ``now`` must not leak microseconds."""
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer()))))
        outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS")
    assert outcome.exit_code == 0 and outcome.decision is not None
    stamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    assert re.fullmatch(stamp, outcome.decision["created_at"])
    assert re.fullmatch(stamp, outcome.decision["valid_until"])
    assert re.fullmatch(stamp + r"-[0-9a-f]{6}", outcome.decision["decision_id"])


def _failing_replace_for(name: str) -> Any:
    """``os.replace`` stand-in that fails only for destination ``name`` (after the tmp file exists)."""
    real_replace = os.replace

    def fake(src: Any, dst: Any) -> None:
        if Path(dst).name == name:
            assert Path(src).exists() and Path(src).name.startswith(f".{name}.")
            raise OSError(28, "No space left on device")
        real_replace(src, dst)

    return fake


@respx.mock
def test_failed_decision_replace_keeps_old_file_and_logs_error(
    settings: Settings, context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 11 'atomares Schreiben': the rename fails -> old decision.json intact, log line says so."""
    old = _seed_existing_decision(settings)
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer()))))
    monkeypatch.setattr("btctrader.common.jsonl.os.replace", _failing_replace_for(advisor.DECISION_FILE))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1 and outcome.decision is None
    assert "cannot write decision.json" in (outcome.error or "")
    decision_file, log_file, _ = _files(settings)
    assert read_json(decision_file) == old
    assert [p.name for p in decision_file.parent.iterdir() if p.name.endswith(".tmp")] == []
    line = read_jsonl_tail(log_file)[0]
    assert line["error"] and "No space left" in line["error"]
    assert line["regime"] == "neutral"  # what the model said is kept, but the line is not a success


@respx.mock
def test_failed_log_append_after_decision_written_is_reported(
    settings: Settings, context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response(json.dumps(good_answer()))))

    def boom(path: Any, obj: Any) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(advisor, "append_jsonl", boom)
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1
    assert "decision.json written" in (outcome.error or "")
    doc = read_json(_files(settings)[0])
    assert doc is not None and doc["regime"] == "neutral"
    assert not _files(settings)[1].exists()


@respx.mock
def test_truncated_answer_names_max_tokens(settings: Settings, context: dict[str, Any]) -> None:
    body = chat_response('{"regime": "neutral", "confidence": 0.5, "rationale": "cut off he')
    body["choices"][0]["finish_reason"] = "length"
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    outcome = advisor.run_advisor(settings, context=context, system_prompt="SYS", now=NOW)
    assert outcome.exit_code == 1
    error = outcome.error or ""
    assert "truncated at max_tokens=400" in error and "not valid JSON" in error
    assert not _files(settings)[0].exists()
