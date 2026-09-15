"""Tests for btctrader.common.alerts."""

from __future__ import annotations

import json

import httpx
import respx

from btctrader.common.alerts import Alerter, alerter_from_settings
from btctrader.common.config import load_settings

NTFY = "https://ntfy.example.com/btc-bot"
TG = "https://api.telegram.org/bot123:ABC/sendMessage"


def test_no_channels_returns_empty() -> None:
    assert Alerter().send("t", "m") == []
    assert Alerter().channels == []


@respx.mock
def test_both_channels_succeed() -> None:
    tg = respx.post(TG).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    nt = respx.post(NTFY).mock(return_value=httpx.Response(200, json={"id": "x"}))
    a = Alerter("123:ABC", "42", NTFY, client=httpx.Client())
    assert a.send("Kill-Switch", "Drawdown 21 % über Peak", priority="high") == ["telegram", "ntfy"]

    body = json.loads(tg.calls[0].request.content)
    assert body["chat_id"] == "42"
    assert body["text"].startswith("Kill-Switch\n")
    req = nt.calls[0].request
    assert req.headers["Title"] == "Kill-Switch"
    assert req.headers["Priority"] == "4"
    assert req.content.decode("utf-8") == "Drawdown 21 % über Peak"


@respx.mock
def test_partial_failure_reports_only_success() -> None:
    respx.post(TG).mock(return_value=httpx.Response(500, text="boom"))
    respx.post(NTFY).mock(return_value=httpx.Response(200, json={}))
    a = Alerter("123:ABC", "42", NTFY)
    assert a.send("t", "m") == ["ntfy"]


@respx.mock
def test_never_raises_on_transport_error() -> None:
    respx.post(TG).mock(side_effect=httpx.ConnectError("down"))
    respx.post(NTFY).mock(side_effect=RuntimeError("weird"))
    a = Alerter("123:ABC", "42", NTFY)
    assert a.send("t", "m") == []


@respx.mock
def test_telegram_ok_false_counts_as_failure() -> None:
    respx.post(TG).mock(return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"}))
    assert Alerter("123:ABC", "42").send("t", "m") == []


@respx.mock
def test_non_latin1_title_is_encoded_for_ntfy() -> None:
    route = respx.post(NTFY).mock(return_value=httpx.Response(200, json={}))
    assert Alerter(ntfy_url=NTFY).send("Achtung €", "m") == ["ntfy"]
    assert route.calls[0].request.headers["Title"].startswith("=?UTF-8?B?")


def test_alerter_from_settings() -> None:
    s = load_settings({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "NTFY_URL": NTFY})
    a = alerter_from_settings(s)
    assert a.channels == ["telegram", "ntfy"]
    assert alerter_from_settings(load_settings({"TELEGRAM_BOT_TOKEN": "t"})).channels == []
