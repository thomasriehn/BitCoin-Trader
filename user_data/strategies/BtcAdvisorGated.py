"""BtcAdvisorGated: BtcTrend plus an entry gate fed by the local vLLM advisor.

``bot_loop_start`` reads ``$ADVISOR_DIR/decision.json`` (default
``/srv/trading/advisor``) once per bot loop. Only when the document is valid
(``schema_version`` 1, ``valid_until`` in the future) and ``mode == "gate"``
does ``regime == "risk_off"`` block new entries in ``confirm_trade_entry``.
``risk_on`` and ``neutral`` change nothing; ``confidence`` is logged only.
A missing, stale or malformed file fails open (behaviour of plain BtcTrend)
with one warning per hour. Exits are never blocked by the advisor. The gate is
inactive in backtesting and hyperopt because a single current decision file
has no meaning for historical candles.

No network calls: the advisor service writes the file, this strategy only reads it.
Order settings and fee reasoning: see the header of BtcTrend.py.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import btctrend_lib as lib
from BtcTrend import BtcTrend

logger = logging.getLogger(__name__)

DEFAULT_ADVISOR_DIR = "/srv/trading/advisor"
DECISION_FILENAME = "decision.json"
WARN_INTERVAL = timedelta(hours=1)


class BtcAdvisorGated(BtcTrend):
    """BtcTrend with a risk_off entry gate read from decision.json."""

    INTERFACE_VERSION = 3

    def bot_start(self, **kwargs: Any) -> None:
        super().bot_start(**kwargs)
        self._gate_block = False
        self._gate_reason = "not read"
        self._gate_decision_id: str | None = None
        self._last_warn: dict[str, datetime] = {}

    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        if not self._is_live_or_dry():
            self._gate_block = False
            self._gate_reason = "inactive (not live/dry-run)"
            return
        decision = self.read_decision()
        block, reason = lib.evaluate_decision(decision, current_time)
        self._gate_block = block
        self._gate_reason = reason
        if decision is None or reason in {"missing", "expired", "schema_version", "valid_until unparsable"}:
            self._warn_rate_limited(
                reason,
                current_time,
                f"advisor gate inactive ({reason}) at {self.decision_path()}; "
                "falling back to BtcTrend behaviour",
            )
            return
        decision_id = decision.get("decision_id")
        if decision_id != self._gate_decision_id:
            self._gate_decision_id = decision_id
            logger.info(
                "advisor decision %s: regime=%s confidence=%s mode=%s -> block_entries=%s",
                decision_id,
                decision.get("regime"),
                decision.get("confidence"),
                decision.get("mode"),
                block,
            )

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        if not super().confirm_trade_entry(
            pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, **kwargs
        ):
            return False
        if getattr(self, "_gate_block", False):
            logger.warning(
                "%s: entry blocked by advisor gate (%s, decision %s)",
                pair,
                self._gate_reason,
                self._gate_decision_id,
            )
            return False
        return True

    # ------------------------------------------------------------------ helpers

    def decision_path(self) -> Path:
        return Path(os.environ.get("ADVISOR_DIR", DEFAULT_ADVISOR_DIR)) / DECISION_FILENAME

    def read_decision(self) -> dict[str, Any] | None:
        """Read decision.json; None when missing or unreadable (never raises)."""
        path = self.decision_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _warn_rate_limited(self, key: str, now: datetime, message: str) -> None:
        last = self._last_warn.get(key)
        if last is not None and now - last < WARN_INTERVAL:
            return
        self._last_warn[key] = now
        logger.warning(message)
