"""Call the local vLLM server, validate the answer and persist the decision.

Verified against vLLM v0.29.0 (OpenAI-compatible server):

* Primary request uses ``response_format`` of type ``json_schema`` with
  ``{"name", "schema", "strict"}``. vLLM maps it to constrained decoding
  (``structured_outputs.json``); ``strict`` is accepted and ignored.
* If the server answers HTTP 400 and the message mentions the
  ``response_format`` / guided / structured-output machinery, the request is
  retried once without ``response_format`` and with the legacy top-level
  ``guided_json`` field (pre-0.12 servers), as KOMPONENTEN.md prescribes.
  Servers >= 0.12 ignore unknown fields, so the retry still yields an
  answer that is then validated by Pydantic.
* ``chat_template_kwargs.enable_thinking=false`` is sent so hybrid
  thinking models (Qwen3, Qwen3.5, Gemma 4) answer directly with JSON.

Error policy (section 6): on any failure the error is logged, a line with
``error`` is appended to ``decisions.jsonl``, ``decision.json`` stays
untouched and the exit code is 1.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from btctrader.advisor import context as ctx_mod
from btctrader.advisor.schema import RegimeDecision, response_format, response_json_schema
from btctrader.common.config import Settings
from btctrader.common.db import iso_utc, utcnow
from btctrader.common.jsonl import append_jsonl, atomic_write_json

log = logging.getLogger(__name__)

DECISION_SCHEMA_VERSION = 1
DECISION_FILE = "decision.json"
DECISIONS_LOG = "decisions.jsonl"
CONTEXT_FILE = "context-latest.json"
TEMPERATURE = 0
SEED = 42
MAX_TOKENS = 400
RAW_RESPONSE_LIMIT = 4000
DISABLE_THINKING = True

_FALLBACK_TRIGGER = re.compile(r"response_format|guided|json_schema|structured", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class AdvisorError(Exception):
    """Any failure of the advisor call or validation (already formatted for the log)."""


def load_system_prompt() -> str:
    """The system prompt shipped with the package (``prompts/system.md``)."""
    return resources.files("btctrader.advisor").joinpath("prompts/system.md").read_text(encoding="utf-8")


def build_messages(system_prompt: str, context: dict[str, Any]) -> list[dict[str, str]]:
    """System prompt first, then the market context as compact JSON."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Market context (JSON):\n" + ctx_mod.stable_json(context)},
    ]


def build_request(model: str, system_prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """Body of the primary ``POST /chat/completions`` request."""
    body: dict[str, Any] = {
        "model": model,
        "messages": build_messages(system_prompt, context),
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_tokens": MAX_TOKENS,
        "response_format": response_format(),
    }
    if DISABLE_THINKING:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def build_fallback_request(request: dict[str, Any]) -> dict[str, Any]:
    """Same request without ``response_format`` but with legacy ``guided_json`` (older vLLM)."""
    body = {k: v for k, v in request.items() if k != "response_format"}
    body["guided_json"] = response_json_schema()
    return body


def should_retry_with_guided_json(status_code: int, body_text: str) -> bool:
    return status_code == 400 and bool(_FALLBACK_TRIGGER.search(body_text or ""))


@dataclass
class CallResult:
    """Outcome of one model call (possibly after the fallback retry)."""

    response: dict[str, Any]
    latency_ms: int
    used_fallback: bool
    raw_content: str


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _post(
    client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> httpx.Response:
    try:
        return client.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise AdvisorError(f"timeout after {timeout:g}s calling {url}: {exc.__class__.__name__}") from exc
    except httpx.HTTPError as exc:
        raise AdvisorError(f"request to {url} failed: {exc.__class__.__name__}: {exc}") from exc


def call_model(
    base_url: str,
    api_key: str,
    request: dict[str, Any],
    *,
    timeout: float,
    client: httpx.Client | None = None,
) -> CallResult:
    """POST the request, retry once with ``guided_json`` on a structured-output 400, return the JSON."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = _headers(api_key)
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    started = time.monotonic()
    used_fallback = False
    try:
        resp = _post(http, url, request, headers, timeout)
        if should_retry_with_guided_json(resp.status_code, resp.text):
            log.warning(
                "server rejected response_format (HTTP 400: %s); retrying with guided_json",
                _short(resp.text),
            )
            used_fallback = True
            resp = _post(http, url, build_fallback_request(request), headers, timeout)
    finally:
        if own:
            http.close()
    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        raise AdvisorError(f"HTTP {resp.status_code} from {url}: {_short(resp.text)}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise AdvisorError(f"response is not JSON: {_short(resp.text)}") from exc
    if not isinstance(data, dict):
        raise AdvisorError(f"unexpected response payload: {_short(resp.text)}")
    return CallResult(
        response=data, latency_ms=latency_ms, used_fallback=used_fallback, raw_content=extract_content(data)
    )


def extract_content(data: dict[str, Any]) -> str:
    """Assistant text of the first choice ('' if absent)."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def parse_decision(content: str) -> RegimeDecision:
    """Strip think blocks and code fences, parse JSON, validate with Pydantic."""
    text = _THINK.sub("", content or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    if not text:
        raise AdvisorError("empty answer from model")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise AdvisorError(f"answer is not valid JSON: {exc}: {_short(text)}") from exc
    try:
        return RegimeDecision.model_validate(obj)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise AdvisorError(f"answer violates schema: {problems}") from exc


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _usage(data: dict[str, Any]) -> dict[str, Any] | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def make_decision_id(created_at: datetime, context_hash: str) -> str:
    """``<created_at ISO>-<6 hex of the context hash>`` as in KOMPONENTEN.md."""
    digest = context_hash.split(":", 1)[-1]
    return f"{iso_utc(created_at)}-{digest[:6]}"


@dataclass
class RunOutcome:
    """What ``run_advisor`` did; ``exit_code`` 0 on success, 1 on any error."""

    exit_code: int
    decision: dict[str, Any] | None = None
    error: str | None = None
    log_record: dict[str, Any] = field(default_factory=dict)


def run_advisor(
    settings: Settings,
    *,
    context: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    client: httpx.Client | None = None,
    market_client: httpx.Client | None = None,
    ft_client: Any | None = None,
    now: datetime | None = None,
) -> RunOutcome:
    """One advisor cycle: context, model call, validation, files. Never raises on expected failures."""
    created_at = now or utcnow()
    prompt = system_prompt if system_prompt is not None else load_system_prompt()
    prompt_hash = ctx_mod.sha256_prefixed(prompt)
    advisor_dir = Path(settings.advisor_dir)
    record: dict[str, Any] = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "created_at": iso_utc(created_at),
        "mode": settings.advisor_mode,
        "model": settings.advisor_model,
        "prompt_hash": prompt_hash,
    }

    try:
        if context is None:
            context = ctx_mod.build_context(client=market_client, ft=ft_client, now=created_at)
        atomic_write_json(advisor_dir / CONTEXT_FILE, context)
    except Exception as exc:  # noqa: BLE001 - any context failure is reported the same way
        return _fail(advisor_dir, record, f"context: {exc.__class__.__name__}: {exc}")

    chash = ctx_mod.context_hash(context)
    record["context_hash"] = chash
    record["decision_id"] = make_decision_id(created_at, chash)
    request = build_request(settings.advisor_model, prompt, context)

    try:
        call = call_model(
            settings.advisor_base_url,
            settings.advisor_api_key,
            request,
            timeout=settings.advisor_timeout_s,
            client=client,
        )
    except AdvisorError as exc:
        return _fail(advisor_dir, record, str(exc))

    record["latency_ms"] = call.latency_ms
    record["usage"] = _usage(call.response)
    record["raw_response"] = call.raw_content[:RAW_RESPONSE_LIMIT]
    record["used_guided_json_fallback"] = call.used_fallback

    try:
        decision = parse_decision(call.raw_content)
    except AdvisorError as exc:
        return _fail(advisor_dir, record, str(exc))

    valid_until = created_at + timedelta(hours=2 * settings.advisor_interval_hours)
    decision_doc: dict[str, Any] = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_id": record["decision_id"],
        "created_at": record["created_at"],
        "valid_until": iso_utc(valid_until),
        "mode": settings.advisor_mode,
        "model": settings.advisor_model,
        "regime": decision.regime,
        "confidence": decision.confidence,
        "horizon_days": decision.horizon_days,
        "rationale": decision.rationale,
        "key_factors": decision.key_factors,
        "context_hash": chash,
        "prompt_hash": prompt_hash,
    }
    extra_keys = ("latency_ms", "usage", "raw_response", "used_guided_json_fallback")
    log_line = {**decision_doc, **{k: record[k] for k in extra_keys}}
    log_line["error"] = None
    try:
        append_jsonl(advisor_dir / DECISIONS_LOG, log_line)
        atomic_write_json(advisor_dir / DECISION_FILE, decision_doc)
    except OSError as exc:
        log.error("error=%s", f"cannot write advisor files in {advisor_dir}: {exc}")
        return RunOutcome(exit_code=1, decision=decision_doc, error=str(exc), log_record=log_line)
    log.info(
        "decision %s regime=%s confidence=%.2f horizon=%dd mode=%s latency=%dms",
        decision_doc["decision_id"],
        decision.regime,
        decision.confidence,
        decision.horizon_days,
        settings.advisor_mode,
        call.latency_ms,
    )
    return RunOutcome(exit_code=0, decision=decision_doc, error=None, log_record=log_line)


def _fail(advisor_dir: Path, record: dict[str, Any], error: str) -> RunOutcome:
    """Log the error, append the error line to decisions.jsonl, leave decision.json alone."""
    log.error("error=%s", error)
    line = {**record, "error": error}
    line.setdefault("decision_id", None)
    line.setdefault("context_hash", None)
    try:
        append_jsonl(advisor_dir / DECISIONS_LOG, line)
    except OSError as exc:
        log.error("error=%s", f"cannot append to {advisor_dir / DECISIONS_LOG}: {exc}")
    return RunOutcome(exit_code=1, decision=None, error=error, log_record=line)
