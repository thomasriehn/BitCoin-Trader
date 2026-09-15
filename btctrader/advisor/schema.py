"""Pydantic schema of the advisor answer and its JSON-schema export for ``response_format``.

The Pydantic model carries the full constraints of KOMPONENTEN.md section 6
(regime enum, confidence 0..1, horizon 1..30 days, rationale <= 500 chars,
at most 5 key factors). The exported JSON schema is deliberately simpler:
numeric bounds are moved into ``description`` so that every vLLM
structured-output backend (xgrammar, guidance, outlines) accepts it. The
ranges are always enforced in code by ``RegimeDecision.model_validate``.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Regime = Literal["risk_on", "neutral", "risk_off"]

REGIMES: tuple[str, ...] = ("risk_on", "neutral", "risk_off")
SCHEMA_NAME = "regime_decision"
MAX_RATIONALE_CHARS = 500
MAX_KEY_FACTORS = 5


class RegimeDecision(BaseModel):
    """Validated answer of the regime classifier."""

    model_config = ConfigDict(extra="forbid", strict=False)

    regime: Regime = Field(description="Market regime for the next horizon: risk_on, neutral or risk_off.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Subjective confidence in the regime call, a number between 0 and 1.",
    )
    horizon_days: int = Field(
        ge=1,
        le=30,
        description="Number of days the regime call is meant to hold, an integer between 1 and 30.",
    )
    rationale: str = Field(
        max_length=MAX_RATIONALE_CHARS,
        description="Short reasoning in plain English, at most 500 characters.",
    )
    key_factors: list[str] = Field(
        max_length=MAX_KEY_FACTORS,
        description="Up to 5 short bullet points naming the decisive inputs.",
    )


# Keywords that some constrained-decoding backends reject or ignore.
_STRIPPED_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "maxLength", "title")


def response_json_schema() -> dict[str, Any]:
    """JSON schema for constrained decoding: enum, types, required, no extra properties.

    Numeric bounds and string lengths are dropped from the schema (kept in the
    descriptions and enforced by Pydantic) to stay compatible with every
    vLLM backend. ``maxItems`` on ``key_factors`` is kept.
    """
    schema = copy.deepcopy(RegimeDecision.model_json_schema())
    schema.pop("title", None)
    schema["additionalProperties"] = False
    props: dict[str, Any] = schema["properties"]
    for name, prop in props.items():
        for key in _STRIPPED_KEYWORDS:
            prop.pop(key, None)
        if name == "key_factors":
            prop["maxItems"] = MAX_KEY_FACTORS
            prop["minItems"] = 1
    schema["required"] = list(props.keys())
    return schema


def response_format() -> dict[str, Any]:
    """OpenAI-style ``response_format`` object for the vLLM chat completions endpoint."""
    return {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": response_json_schema(), "strict": True},
    }
