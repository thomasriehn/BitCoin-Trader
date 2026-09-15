"""``btctrader-advisor`` command line: run, context, evaluate."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from btctrader.advisor import advisor
from btctrader.advisor import context as ctx_mod
from btctrader.advisor import evaluate as ev_mod
from btctrader.common.config import ConfigError, Settings, load_settings
from btctrader.common.ftapi import FreqtradeClient, client_from_settings
from btctrader.common.jsonl import read_json
from btctrader.common.log import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btctrader-advisor",
        description="Advisor: Marktregime per lokalem vLLM (Schatten- oder Gate-Modus)",
    )
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Kontext holen, Modell fragen, decision.json schreiben")
    run.add_argument(
        "--dry-run", action="store_true", help="Request nur ausgeben, Server nicht aufrufen, nichts schreiben"
    )
    run.add_argument("--context-file", type=Path, default=None, help="Kontext aus JSON-Datei statt Bitvavo")
    run.add_argument("--no-bot", action="store_true", help="Freqtrade-Status nicht abfragen")

    context = sub.add_parser("context", help="Nur den Marktkontext als JSON ausgeben")
    context.add_argument("--no-bot", action="store_true", help="Freqtrade-Status nicht abfragen")
    context.add_argument("--out", type=Path, default=None, help="Zusätzlich in diese Datei schreiben")

    evaluate = sub.add_parser("evaluate", help="Geloggte Regime mit der Folge-Rendite vergleichen")
    evaluate.add_argument("--since", type=date.fromisoformat, default=None, help="ab Datum (YYYY-MM-DD)")
    evaluate.add_argument("--decisions", type=Path, default=None, help="Pfad zu decisions.jsonl")
    evaluate.add_argument("--json", action="store_true", help="Ergebnis als JSON statt Tabelle")
    return parser


def _freqtrade_client(settings: Settings, disabled: bool) -> FreqtradeClient | None:
    if disabled or not (settings.ft_api_user and settings.ft_api_pass):
        return None
    return client_from_settings(settings)


def _load_context_file(path: Path) -> dict[str, Any]:
    obj = read_json(path)
    if obj is None:
        raise SystemExit(f"error=context file {path} missing or not a JSON object")
    return obj


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    context: dict[str, Any] | None = None
    if args.context_file is not None:
        context = _load_context_file(args.context_file)
    if args.dry_run:
        ft = _freqtrade_client(settings, args.no_bot)
        if context is None:
            context = ctx_mod.build_context(ft=ft)
        request = advisor.build_request(settings.advisor_model, advisor.load_system_prompt(), context)
        headers = {"Authorization": "Bearer ***" if settings.advisor_api_key else "(none)"}
        print(
            json.dumps(
                {
                    "url": settings.advisor_base_url.rstrip("/") + "/chat/completions",
                    "headers": headers,
                    "timeout_s": settings.advisor_timeout_s,
                    "body": request,
                    "fallback_body_on_400": advisor.build_fallback_request(request),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    ft = _freqtrade_client(settings, args.no_bot)
    outcome = advisor.run_advisor(settings, context=context, ft_client=ft)
    return outcome.exit_code


def cmd_context(args: argparse.Namespace, settings: Settings) -> int:
    ft = _freqtrade_client(settings, args.no_bot)
    context = ctx_mod.build_context(ft=ft)
    text = json.dumps(context, indent=2, sort_keys=True, ensure_ascii=False)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


def cmd_evaluate(args: argparse.Namespace, settings: Settings) -> int:
    path = args.decisions or Path(settings.advisor_dir) / advisor.DECISIONS_LOG
    decisions = ev_mod.read_decisions(path, since=args.since)
    if not decisions:
        print(f"no decisions found in {path}")
        return 0
    candles = ev_mod.fetch_candles_for(decisions)
    result = ev_mod.evaluate_decisions(decisions, candles)
    if args.json:
        print(
            json.dumps(
                {
                    "decisions": result.decisions,
                    "pending": result.pending,
                    "baseline_positive_share": result.baseline_positive_share,
                    "rows": [
                        {
                            "regime": r.regime,
                            "horizon": r.horizon,
                            "n": r.n,
                            "hit_rate": r.hit_rate,
                            "mean_fwd_return_pct": r.mean_return,
                        }
                        for r in result.rows
                    ],
                },
                indent=2,
            )
        )
    else:
        print(ev_mod.format_table(result))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = setup_logging("btctrader.advisor", args.log_level)
    require = ["advisor_model"] if args.command == "run" else []
    try:
        settings = load_settings(require=require)
    except ConfigError as exc:
        log.error("error=%s", exc)
        return 2
    try:
        if args.command == "run":
            return cmd_run(args, settings)
        if args.command == "context":
            return cmd_context(args, settings)
        return cmd_evaluate(args, settings)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            log.error("%s", exc.code)
            return 1
        raise
    except Exception as exc:  # noqa: BLE001 - top-level guard: log and exit 1 (systemd sees the failure)
        log.error("error=%s: %s", exc.__class__.__name__, exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
