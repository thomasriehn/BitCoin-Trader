"""Tests for btctrader.common.config."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from btctrader.common.config import ConfigError, Settings, load_settings, parse_env_file


def test_defaults_match_contract() -> None:
    s = load_settings({})
    assert s.ft_api_url == "http://127.0.0.1:8080"
    assert s.ft_api_user == "" and s.ft_api_pass == ""
    assert s.ft_db_path == Path("/srv/trading/user_data/tradesv3.dryrun.sqlite")
    assert s.bitvavo_operator_id == 1
    assert s.ledger_db_path == Path("/srv/trading/ledger/ledger.sqlite")
    assert s.ledger_account_id == "bitvavo-main"
    assert s.ledger_source == "freqtrade-db"
    assert s.ledger_export_dir == Path("/srv/trading/ledger/exports")
    assert s.start_capital_eur == Decimal("1000")
    assert s.benchmark_start is None
    assert s.dca_weeks == 52
    assert s.fee_maker == Decimal("0.0015") and s.fee_taker == Decimal("0.0025")
    assert s.advisor_base_url == "http://127.0.0.1:8000/v1"
    assert s.advisor_model == ""
    assert s.advisor_mode == "shadow"
    assert s.advisor_interval_hours == 1.0
    assert s.advisor_dir == Path("/srv/trading/advisor")
    assert s.advisor_timeout_s == 120.0
    assert s.guard_dir == Path("/srv/trading/guard")
    assert s.guard_daily_loss_pct == Decimal("3.0")
    assert s.guard_max_drawdown_pct == Decimal("20.0")
    assert s.guard_reconcile_tol_eur == Decimal("2.0")
    assert s.guard_cod_enabled is False
    assert s.guard_cod_seconds == 120
    assert s.healthchecks_url == "" and s.telegram_bot_token == "" and s.ntfy_url == ""
    assert s.dashboard_bind == "127.0.0.1:8090"
    assert s.tz_display == "Europe/Berlin"


def test_settings_is_frozen() -> None:
    s = load_settings({})
    with pytest.raises(AttributeError):
        s.dca_weeks = 10  # type: ignore[misc]


def test_typed_parsing() -> None:
    s = load_settings(
        {
            "BENCHMARK_START": "2026-09-15",
            "START_CAPITAL_EUR": "1500.50",
            "DCA_WEEKS": "26",
            "GUARD_COD_ENABLED": "true",
            "ADVISOR_INTERVAL_HOURS": "2.5",
            "GUARD_DIR": "/tmp/guard",
            "BITVAVO_OPERATOR_ID": "42",
        }
    )
    assert s.benchmark_start == date(2026, 9, 15)
    assert s.start_capital_eur == Decimal("1500.50")
    assert isinstance(s.start_capital_eur, Decimal)
    assert s.dca_weeks == 26
    assert s.guard_cod_enabled is True
    assert s.advisor_interval_hours == 2.5
    assert s.guard_dir == Path("/tmp/guard")
    assert s.bitvavo_operator_id == 42


def test_required_fields_reported_together() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({}, require=["ft_api_user", "ft_api_pass", "benchmark_start", "advisor_model"])
    msg = str(exc.value)
    for name in ("FT_API_USER", "FT_API_PASS", "BENCHMARK_START", "ADVISOR_MODEL"):
        assert name in msg


def test_required_fields_satisfied() -> None:
    s = load_settings(
        {"FT_API_USER": "u", "FT_API_PASS": "p", "BENCHMARK_START": "2026-01-01"},
        require=("ft_api_user", "ft_api_pass", "benchmark_start"),
    )
    assert s.ft_api_user == "u"


def test_unknown_required_name_is_error() -> None:
    with pytest.raises(ConfigError, match="unknown required"):
        load_settings({}, require=["does_not_exist"])


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"LEDGER_SOURCE": "csv"}, "LEDGER_SOURCE"),
        ({"ADVISOR_MODE": "auto"}, "ADVISOR_MODE"),
        ({"LEDGER_SOURCE": "exchange"}, "BITVAVO_API_KEY_RO"),
        ({"DCA_WEEKS": "0"}, "DCA_WEEKS"),
        ({"DCA_WEEKS": "abc"}, "DCA_WEEKS"),
        ({"FEE_MAKER": "1.5"}, "FEE_MAKER"),
        ({"GUARD_DAILY_LOSS_PCT": "0"}, "GUARD_DAILY_LOSS_PCT"),
        ({"GUARD_MAX_DRAWDOWN_PCT": "150"}, "GUARD_MAX_DRAWDOWN_PCT"),
        ({"GUARD_COD_ENABLED": "maybe"}, "GUARD_COD_ENABLED"),
        ({"BENCHMARK_START": "15.09.2026"}, "BENCHMARK_START"),
        ({"START_CAPITAL_EUR": "-5"}, "START_CAPITAL_EUR"),
        ({"FT_API_URL": "127.0.0.1:8080"}, "FT_API_URL"),
        ({"DASHBOARD_BIND": "localhost"}, "DASHBOARD_BIND"),
        ({"ADVISOR_TIMEOUT_S": "0"}, "ADVISOR_TIMEOUT_S"),
    ],
)
def test_validation_errors(env: dict[str, str], fragment: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    assert fragment in str(exc.value)


def test_exchange_source_with_keys_is_valid() -> None:
    s = load_settings(
        {"LEDGER_SOURCE": "exchange", "BITVAVO_API_KEY_RO": "k", "BITVAVO_API_SECRET_RO": "s"}
    )
    assert s.ledger_source == "exchange"


def test_empty_value_keeps_default_for_typed_fields() -> None:
    s = load_settings({"DCA_WEEKS": "", "BENCHMARK_START": "", "GUARD_COD_ENABLED": ""})
    assert s.dca_weeks == 52
    assert s.benchmark_start is None
    assert s.guard_cod_enabled is False


def test_empty_value_keeps_default_for_str_fields_too() -> None:
    # "leer = Default" must hold for every field: LEDGER_SOURCE= or TZ_DISPLAY= in the env file
    # are placeholders, not a request for an empty (invalid) value.
    s = load_settings({"LEDGER_SOURCE": "", "ADVISOR_MODE": " ", "TZ_DISPLAY": "", "FT_API_URL": ""})
    assert s.ledger_source == "freqtrade-db"
    assert s.advisor_mode == "shadow"
    assert s.tz_display == "Europe/Berlin"
    assert s.ft_api_url == "http://127.0.0.1:8080"
    with pytest.raises(ConfigError, match="FT_API_USER"):
        load_settings({"FT_API_USER": ""}, require=["ft_api_user"])  # still counts as missing


@pytest.mark.parametrize("bind", ["0.0.0.0:8090", ":::8090", "[::]:8090", "*:8090"])
def test_dashboard_bind_rejects_wildcard_hosts(bind: str) -> None:
    with pytest.raises(ConfigError, match="no login"):
        load_settings({"DASHBOARD_BIND": bind})


def test_dashboard_bind_accepts_loopback_and_tailscale() -> None:
    assert load_settings({"DASHBOARD_BIND": "100.64.1.2:8090"}).dashboard_bind == "100.64.1.2:8090"
    assert load_settings({"DASHBOARD_BIND": "[::1]:8090"}).dashboard_bind == "[::1]:8090"


def test_env_file_loading_and_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / "btctrader.env"
    env_file.write_text(
        "# comment\n"
        "FT_API_USER=filebot\n"
        "export FT_API_PASS='se cret'\n"
        'ADVISOR_MODEL="Qwen/Qwen3-14B"\n'
        "DCA_WEEKS=26\n"
        "BENCHMARK_START=2026-09-01\n"
        "\n",
        encoding="utf-8",
    )
    s = load_settings({"BTCTRADER_ENV_FILE": str(env_file), "FT_API_USER": "envbot"})
    assert s.ft_api_user == "envbot"  # process env wins
    assert s.ft_api_pass == "se cret"
    assert s.advisor_model == "Qwen/Qwen3-14B"
    assert s.dca_weeks == 26
    assert s.benchmark_start == date(2026, 9, 1)


def test_env_file_missing_when_explicit(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings({"BTCTRADER_ENV_FILE": str(tmp_path / "nope.env")})


def test_default_env_file_is_used_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import btctrader.common.config as cfg

    env_file = tmp_path / "default.env"
    env_file.write_text("TZ_DISPLAY=UTC\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.delenv("BTCTRADER_ENV_FILE", raising=False)
    monkeypatch.delenv("TZ_DISPLAY", raising=False)
    assert load_settings().tz_display == "UTC"  # process environment: default file applies


def test_explicit_env_ignores_default_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit mapping is hermetic, otherwise every test would depend on the host's
    # /etc/freqtrade/btctrader.env once install.sh has created it.
    import btctrader.common.config as cfg

    env_file = tmp_path / "default.env"
    env_file.write_text("TZ_DISPLAY=UTC\nFT_API_USER=hostbot\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "DEFAULT_ENV_FILE", env_file)
    s = load_settings({})
    assert s.tz_display == "Europe/Berlin" and s.ft_api_user == ""
    assert load_settings({"BTCTRADER_ENV_FILE": str(env_file)}).ft_api_user == "hostbot"


def test_load_from_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ_DISPLAY", "Europe/Vienna")
    monkeypatch.delenv("BTCTRADER_ENV_FILE", raising=False)
    assert load_settings().tz_display == "Europe/Vienna"


def test_parse_env_file_ignores_garbage(tmp_path: Path) -> None:
    f = tmp_path / "x.env"
    f.write_text("no equals here\n=novalue\nGOOD=1\n1BAD=2\nA B=3\n", encoding="utf-8")
    assert parse_env_file(f) == {"GOOD": "1"}


@pytest.mark.parametrize(
    "line, expected",
    [
        # '#' is never a comment inside a value (systemd src/basic/env-file.c, VALUE state)
        ("A=abc #def", "abc #def"),
        ("A=p4ss#word", "p4ss#word"),
        # text after a closing quote is appended, blanks around it dropped
        ('A="v" # c', "v# c"),
        # unquoted backslash escapes the next character
        ("A=a\\#b", "a#b"),
        ("A=a\\\\b", "a\\b"),
        # double quotes: only \" \\ \` \$ are unescaped, other backslashes are kept
        ('A="q\\"x\\$y\\\\z"', 'q"x$y\\z'),
        ('A="keep\\nthis"', "keep\\nthis"),
        # single quotes are literal
        ("A='lit\\n#x'", "lit\\n#x"),
        # whitespace around an unquoted value is stripped, inner whitespace kept
        ("A=  two words  ", "two words"),
        ("A=", ""),
        ('A=""', ""),
        # escaped newline continues the line
        ("A=one\\\ntwo", "onetwo"),
        ('A="con\\\ntinued"', "continued"),
    ],
)
def test_parse_env_file_matches_systemd(tmp_path: Path, line: str, expected: str) -> None:
    f = tmp_path / "x.env"
    f.write_text(line + "\n", encoding="utf-8")
    assert parse_env_file(f) == {"A": expected}
    f.write_text(line, encoding="utf-8")  # no trailing newline
    assert parse_env_file(f) == {"A": expected}


def test_parse_env_file_comment_lines_and_export(tmp_path: Path) -> None:
    f = tmp_path / "x.env"
    f.write_text("# c\n  ; also a comment\nexport A=1\nB=2\r\n\nC='3'\n", encoding="utf-8")
    assert parse_env_file(f) == {"A": "1", "B": "2", "C": "3"}


def test_settings_field_names_are_lowercase_env_names() -> None:
    from dataclasses import fields

    for f in fields(Settings):
        assert f.name == f.name.lower()
        assert f.name.upper() == Settings().env_name(f.name)
