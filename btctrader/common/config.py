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


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` env file (systemd EnvironmentFile style).

    Supports comments, blank lines, an optional ``export`` prefix and single or
    double quotes around the value. Unknown lines are ignored.
    """
    result: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Strip trailing unquoted comments: FOO=bar  # comment
            hash_pos = value.find(" #")
            if hash_pos >= 0:
                value = value[:hash_pos].rstrip()
        result[key] = value
    return result


def _merged_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Combine env-file values with the process environment (environment wins)."""
    base: Mapping[str, str] = os.environ if env is None else env
    merged: dict[str, str] = {}
    env_file = base.get(ENV_FILE_VAR, "").strip()
    candidate = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if candidate.is_file():
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
    """Convert a raw string to the field's type. Empty strings keep the default for non-str fields."""
    env_name = field_name.upper()
    if field_type == "str":
        return raw.strip()
    if raw.strip() == "":
        return default
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
