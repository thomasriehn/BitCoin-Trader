"""RegimeDecision constraints and the exported JSON schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from btctrader.advisor.schema import REGIMES, RegimeDecision, response_format, response_json_schema
from tests.advisor.conftest import good_answer


def test_valid_answer_round_trips() -> None:
    d = RegimeDecision.model_validate(good_answer())
    assert d.regime == "neutral"
    assert d.confidence == 0.55
    assert d.horizon_days == 7


@pytest.mark.parametrize(
    "bad",
    [
        {"regime": "bullish"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"horizon_days": 0},
        {"horizon_days": 31},
        {"horizon_days": 2.5},
        {"rationale": "x" * 501},
        {"key_factors": ["a", "b", "c", "d", "e", "f"]},
        {"key_factors": "not a list"},
        {"extra_field": 1},
    ],
)
def test_out_of_range_values_are_rejected(bad: dict) -> None:
    with pytest.raises(ValidationError):
        RegimeDecision.model_validate(good_answer(**bad))


def test_missing_field_is_rejected() -> None:
    body = good_answer()
    del body["rationale"]
    with pytest.raises(ValidationError):
        RegimeDecision.model_validate(body)


def test_json_schema_export_is_strict_and_backend_friendly() -> None:
    schema = response_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"regime", "confidence", "horizon_days", "rationale", "key_factors"}
    assert schema["properties"]["regime"]["enum"] == list(REGIMES)
    assert schema["properties"]["key_factors"]["maxItems"] == 5
    # numeric bounds live in code, not in the schema (backend compatibility)
    assert "maximum" not in schema["properties"]["confidence"]
    assert "maxLength" not in schema["properties"]["rationale"]
    assert "title" not in schema


def test_response_format_shape() -> None:
    rf = response_format()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "regime_decision"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == response_json_schema()
