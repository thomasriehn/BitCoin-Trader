"""Settings for all btctrader services, read from environment variables.

See docs/KOMPONENTEN.md section 3 for the variable list and defaults.
Values come from the process environment; an env file
(``BTCTRADER_ENV_FILE`` or ``/etc/freqtrade/btctrader.env``) supplies
defaults for variables not already set in the environment.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DEFAULT_ENV_FILE = Path("/etc/freqtrade/btctrader.env")
ENV_FILE_VAR = "BTCTRADER_ENV_FILE"

LEDGER_SOURCES = ("freqtrade-db", "exchange")
ADVISOR_MODES = ("shadow", "gate")
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*", "0", "::0"})

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class ConfigError(Exception):
    """Raised when settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """One field per variable of KOMPONENTEN.md section 3 (lowercase names)."""

    ft_api_url: str = "http://127.0.0.1:8080"
    ft_api_user: str = ""
    ft_api_pass: str = ""
    ft_db_path: Path = Path("/srv/trading/user_data/tradesv3.dryrun.sqlite")
    bitvavo_api_key_ro: str = ""
    bitvavo_api_secret_ro: str = ""
    bitvavo_operator_id: int = 1
    ledger_db_path: Path = Path("/srv/trading/ledger/ledger.sqlite")
    ledger_account_id: str = "bitvavo-main"
    ledger_source: str = "freqtrade-db"
    ledger_export_dir: Path = Path("/srv/trading/ledger/exports")
    start_capital_eur: Decimal = Decimal("1000")
    benchmark_start: date | None = None
    dca_weeks: int = 52
    fee_maker: Decimal = Decimal("0.0015")
    fee_taker: Decimal = Decimal("0.0025")
    advisor_base_url: str = "http://127.0.0.1:8000/v1"
    advisor_model: str = ""
    advisor_api_key: str = ""
    advisor_mode: str = "shadow"
    advisor_interval_hours: float = 1.0
    advisor_dir: Path = Path("/srv/trading/advisor")
    advisor_timeout_s: float = 120.0
    guard_dir: Path = Path("/srv/trading/guard")
    guard_daily_loss_pct: Decimal = Decimal("3.0")
    guard_max_drawdown_pct: Decimal = Decimal("20.0")
    guard_reconcile_tol_eur: Decimal = Decimal("2.0")
    guard_cod_enabled: bool = False
    guard_cod_seconds: int = 120
    healthchecks_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ntfy_url: str = ""
    dashboard_bind: str = "127.0.0.1:8090"
    tz_display: str = "Europe/Berlin"

    def env_name(self, field_name: str) -> str:
        """Return the environment variable name for a field."""
        return field_name.upper()


FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Settings))


# Characters that a backslash unescapes inside double quotes (systemd SHELL_NEED_ESCAPE);
# any other escaped character keeps its backslash, as in a POSIX shell.
_DQUOTE_UNESCAPE = '"\\`$'


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file with the semantics of systemd ``EnvironmentFile=``.

    The same file is read by the systemd units and by the manual CLI runs, so
    both must see identical values. Rules (port of systemd ``src/basic/env-file.c``):

    * Lines whose first non-blank character is ``#`` or ``;`` are comments.
      ``#`` is **not** special inside a value: ``A=abc #def`` yields ``abc #def``.
    * Unquoted values: leading and trailing whitespace is stripped, ``\\x`` yields
      ``x`` for any character, an escaped newline continues the line.
    * ``'...'``: taken literally, no escapes.
    * ``"..."``: ``\\"``, ``\\\\``, ``\\```, ``\\$`` are unescaped, other backslashes are kept,
      an escaped newline is removed.
    * Text after a closing quote is appended (surrounding blanks dropped).
    * Keys must match ``[A-Za-z_][A-Za-z0-9_]*``; other lines are ignored.
      An ``export`` prefix is tolerated (systemd would ignore such lines).
    """
    result: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for key, value in _env_file_entries(text):
        if key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export") :].strip()
        if not key or not (key[0].isalpha() or key[0] == "_") or not key.replace("_", "").isalnum():
            continue
        result[key] = value
    return result


def _env_file_entries(text: str) -> list[tuple[str, str]]:
    """Character state machine mirroring systemd's env-file parser; returns (key, value) pairs."""
    entries: list[tuple[str, str]] = []
    state = "pre_key"
    key: list[str] = []
    value: list[str] = []
    last_value_ws: int | None = None  # index where trailing whitespace of an unquoted value starts

    def push() -> None:
        nonlocal key, value, last_value_ws
        val = "".join(value)
        if last_value_ws is not None:
            val = val[:last_value_ws]
        entries.append(("".join(key).rstrip(), val))
        key, value, last_value_ws = [], [], None

    for c in text:
        newline = c in "\n\r"
        blank = c in " \t\n\r"
        if state == "pre_key":
            if c in "#;":
                state = "comment"
            elif not blank:
                state, key = "key", [c]
        elif state == "key":
            if newline:
                state, key = "pre_key", []
            elif c == "=":
                state, last_value_ws = "pre_value", None
            else:
                key.append(c)
        elif state == "pre_value":
            if newline:
                push()
                state = "pre_key"
            elif c == "'":
                state = "squote"
            elif c == '"':
                state = "dquote"
            elif c == "\\":
                state = "value_escape"
            elif not blank:
                state = "value"
                value.append(c)
        elif state == "value":
            if newline:
                push()
                state = "pre_key"
            elif c == "\\":
                state, last_value_ws = "value_escape", None
            else:
                if not blank:
                    last_value_ws = None
                elif last_value_ws is None:
                    last_value_ws = len(value)
                value.append(c)
        elif state == "value_escape":
            state = "value"
            if not newline:  # an escaped newline is eaten entirely
                value.append(c)
        elif state == "squote":
            if c == "'":
                state = "pre_value"
            else:
                value.append(c)
        elif state == "dquote":
            if c == '"':
                state = "pre_value"
            elif c == "\\":
                state = "dquote_escape"
            else:
                value.append(c)
        elif state == "dquote_escape":
            state = "dquote"
            if c in _DQUOTE_UNESCAPE:
                value.append(c)
            elif c != "\n":
                value.append("\\")
                value.append(c)
        elif state == "comment":
            if c == "\\":
                state = "comment_escape"
            elif newline:
                state = "pre_key"
        elif state == "comment_escape":
            state = "pre_key" if newline else "comment"
    if state in ("pre_value", "value", "value_escape", "squote", "dquote", "dquote_escape"):
        push()  # last line without a trailing newline
    return entries


def _merged_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Combine env-file values with the environment (environment wins).

    With ``env=None`` the process environment is used and ``DEFAULT_ENV_FILE``
    is read when present. An explicit mapping is hermetic: only an env file it
    names itself via ``BTCTRADER_ENV_FILE`` is consulted, never the host's
    ``/etc/freqtrade/btctrader.env`` (tests must not depend on the host).
    """
    base: Mapping[str, str] = os.environ if env is None else env
    merged: dict[str, str] = {}
    env_file = base.get(ENV_FILE_VAR, "").strip()
    candidate: Path | None
    if env_file:
        candidate = Path(env_file)
    elif env is None:
        candidate = DEFAULT_ENV_FILE
    else:
        candidate = None
    if candidate is not None and candidate.is_file():
        try:
            merged.update(parse_env_file(candidate))
        except OSError as exc:
            raise ConfigError(f"cannot read env file {candidate}: {exc}") from exc
    elif env_file:
        raise ConfigError(f"env file {ENV_FILE_VAR}={candidate} does not exist")
    merged.update({k: v for k, v in base.items() if k in FIELD_NAMES_UPPER or k == ENV_FILE_VAR})
    return merged


FIELD_NAMES_UPPER: frozenset[str] = frozenset(name.upper() for name in FIELD_NAMES)


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}: expected true/false, got {raw!r}")


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: expected an integer, got {raw!r}") from exc


def _parse_float(name: str, raw: str) -> float:
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: expected a number, got {raw!r}") from exc


def _parse_decimal(name: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigError(f"{name}: expected a decimal number, got {raw!r}") from exc
    if not value.is_finite():
        raise ConfigError(f"{name}: expected a finite number, got {raw!r}")
    return value


def _parse_date(name: str, raw: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: expected an ISO date (YYYY-MM-DD), got {raw!r}") from exc


def _convert(field_name: str, field_type: str, raw: str, default: Any) -> Any:
    """Convert a raw string to the field's type. An empty value means "use the default" for every field.

    This keeps ``LEDGER_SOURCE=`` or ``TZ_DISPLAY=`` in the env file harmless
    (they are natural placeholders) instead of yielding ``""`` for string
    fields but the default for typed fields.
    """
    env_name = field_name.upper()
    if raw.strip() == "":
        return default
    if field_type == "str":
        return raw.strip()
    if field_type == "Path":
        return Path(raw.strip()).expanduser()
    if field_type == "int":
        return _parse_int(env_name, raw)
    if field_type == "float":
        return _parse_float(env_name, raw)
    if field_type == "Decimal":
        return _parse_decimal(env_name, raw)
    if field_type == "bool":
        return _parse_bool(env_name, raw)
    if field_type == "date | None":
        return _parse_date(env_name, raw)
    raise ConfigError(f"{env_name}: unsupported field type {field_type}")  # pragma: no cover


def _validate(values: dict[str, Any]) -> list[str]:
    """Return a list of validation problems (empty when the settings are consistent)."""
    problems: list[str] = []
    if values["ledger_source"] not in LEDGER_SOURCES:
        problems.append(f"LEDGER_SOURCE must be one of {', '.join(LEDGER_SOURCES)}")
    if values["advisor_mode"] not in ADVISOR_MODES:
        problems.append(f"ADVISOR_MODE must be one of {', '.join(ADVISOR_MODES)}")
    if values["ledger_source"] == "exchange" and not (
        values["bitvavo_api_key_ro"] and values["bitvavo_api_secret_ro"]
    ):
        problems.append("LEDGER_SOURCE=exchange requires BITVAVO_API_KEY_RO and BITVAVO_API_SECRET_RO")
    if values["bitvavo_operator_id"] < 0:
        problems.append("BITVAVO_OPERATOR_ID must be >= 0")
    if values["start_capital_eur"] <= 0:
        problems.append("START_CAPITAL_EUR must be > 0")
    if values["dca_weeks"] < 1:
        problems.append("DCA_WEEKS must be >= 1")
    for name in ("fee_maker", "fee_taker"):
        if not (Decimal(0) <= values[name] < Decimal(1)):
            problems.append(f"{name.upper()} must be a fraction in [0, 1), e.g. 0.0015")
    if values["advisor_interval_hours"] <= 0:
        problems.append("ADVISOR_INTERVAL_HOURS must be > 0")
    if values["advisor_timeout_s"] <= 0:
        problems.append("ADVISOR_TIMEOUT_S must be > 0")
    for name in ("guard_daily_loss_pct", "guard_max_drawdown_pct"):
        if not (Decimal(0) < values[name] <= Decimal(100)):
            problems.append(f"{name.upper()} must be in (0, 100]")
    if values["guard_reconcile_tol_eur"] < 0:
        problems.append("GUARD_RECONCILE_TOL_EUR must be >= 0")
    if values["guard_cod_seconds"] < 1:
        problems.append("GUARD_COD_SECONDS must be >= 1")
    for name in ("ft_api_url", "advisor_base_url"):
        url = values[name]
        if not url.startswith(("http://", "https://")):
            problems.append(f"{name.upper()} must start with http:// or https://")
    bind = values["dashboard_bind"]
    host, sep, port = bind.rpartition(":")
    if not sep or not host or not port.isdigit() or not (0 < int(port) < 65536):
        problems.append("DASHBOARD_BIND must look like host:port, e.g. 127.0.0.1:8090")
    elif host.strip("[]") in WILDCARD_HOSTS:
        # The dashboard has no login (KOMPONENTEN.md section 10): it must only listen on
        # loopback or the Tailscale address, never on every interface.
        problems.append(
            f"DASHBOARD_BIND must not listen on all interfaces ({host}); the dashboard has no login,"
            " bind to 127.0.0.1 or the Tailscale address"
        )
    return problems


def load_settings(env: Mapping[str, str] | None = None, *, require: Iterable[str] = ()) -> Settings:
    """Build ``Settings`` from ``env`` (default ``os.environ``) plus the optional env file.

    ``require`` lists field names (lowercase, e.g. ``"ft_api_user"``) that must be
    non-empty. All missing ones are reported together in a single ``ConfigError``.
    Enum fields and numeric ranges are validated as well.
    """
    required = [name.lower() for name in require]
    unknown = [name for name in required if name not in FIELD_NAMES]
    if unknown:
        raise ConfigError(f"unknown required settings: {', '.join(unknown)}")

    merged = _merged_env(env)
    values: dict[str, Any] = {}
    problems: list[str] = []
    for f in fields(Settings):
        env_name = f.name.upper()
        raw = merged.get(env_name)
        if raw is None:
            values[f.name] = f.default
            continue
        try:
            values[f.name] = _convert(f.name, str(f.type), raw, f.default)
        except ConfigError as exc:
            problems.append(str(exc))
    if problems:
        raise ConfigError("invalid settings: " + "; ".join(problems))

    missing = [
        name.upper()
        for name in required
        if values[name] is None or (isinstance(values[name], str) and values[name] == "")
    ]
    if missing:
        raise ConfigError("missing required settings: " + ", ".join(missing))

    problems = _validate(values)
    if problems:
        raise ConfigError("invalid settings: " + "; ".join(problems))
    return Settings(**values)
