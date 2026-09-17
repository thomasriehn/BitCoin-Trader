"""Static checks for the deployment files (systemd units, env templates, scripts, docs).

Nothing here talks to systemd or Proxmox. The tests parse the files that install.sh
installs and pin the invariants the review findings were about, so a regression shows
up in CI instead of on the host:

* long-running units must not fall into the permanent ``failed`` state after a fast
  crash loop (``StartLimitIntervalSec=0`` with ``Restart=always``),
* the guard oneshot may take minutes (``TimeoutStartSec`` >= 4 min),
* the trade key for the dead man's switch reaches only the guard unit,
* every btctrader unit gets write access to its own directory only,
* the env templates carry placeholders where a stale value would be accepted silently,
* the placeholder check in docs/SETUP.md is the one install.sh applies,
* sync-config.sh refuses a config.json with any secret field filled,
* the docs describe the behaviour of the code (Healthchecks, kill-switch test, backups).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from btctrader.common.config import ConfigError, load_settings

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
SYSTEMD = DEPLOY / "container" / "systemd"
ENV_DIR = DEPLOY / "container" / "env"
INSTALL_SH = DEPLOY / "container" / "install.sh"
SYNC_SH = DEPLOY / "container" / "sync-config.sh"
CREATE_LXC_SH = DEPLOY / "proxmox" / "create-lxc.sh"
FIREWALL = DEPLOY / "proxmox" / "firewall" / "210.fw"
NUT_README = DEPLOY / "proxmox" / "nut" / "README.md"
SETUP_MD = REPO / "docs" / "SETUP.md"
BETRIEB_MD = REPO / "docs" / "BETRIEB.md"

BTCTRADER_UNITS = ("advisor", "dashboard", "guard", "heartbeat", "ledger")
BOT_UNITS = ("freqtrade-dryrun.service", "freqtrade.service")

# The expression install.sh uses in placeholders_left(); docs/SETUP.md must show the same one.
PLACEHOLDER_RE = r"^[[:space:]]*[A-Za-z_]+=.*(CHANGE_ME|PLACEHOLDER)"


def unit(name: str) -> dict[str, list[str]]:
    """Parse a unit file into {key: [values]} across all sections (keys may repeat)."""
    values: dict[str, list[str]] = {}
    text = (SYSTEMD / name).read_text(encoding="utf-8")
    # join backslash continuations (ExecStart spans several lines)
    text = re.sub(r"\\\n\s*", " ", text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


def seconds(value: str) -> int:
    """Minimal systemd time span parser for the units here (``55``, ``2min``, ``4min``, ``10min``)."""
    m = re.fullmatch(r"(\d+)\s*(s|sec|min|m|h)?", value)
    assert m, value
    factor = {"s": 1, "sec": 1, None: 1, "min": 60, "m": 60, "h": 3600}[m.group(2)]
    return int(m.group(1)) * factor


def active_lines(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


# -- scripts ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [INSTALL_SH, SYNC_SH, CREATE_LXC_SH])
def test_scripts_parse(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


# -- systemd units ---------------------------------------------------------------------


def test_all_units_present() -> None:
    names = {p.name for p in SYSTEMD.iterdir()}
    for svc in BTCTRADER_UNITS:
        assert f"btctrader-{svc}.service" in names
    for svc in ("advisor", "guard", "heartbeat", "ledger"):
        assert f"btctrader-{svc}.timer" in names
    assert set(BOT_UNITS) <= names


def test_restart_always_units_have_no_start_rate_limit() -> None:
    """Finding: 5 fast failures within a minute (DNS or exchange down after a power outage) put the
    unit into ``failed`` for good; Restart=always is not applied any more. With interval 0 the
    rate limit is off and systemd keeps retrying every RestartSec."""
    for name in (*BOT_UNITS, "btctrader-dashboard.service"):
        u = unit(name)
        assert u.get("Restart") == ["always"], name
        assert u.get("StartLimitIntervalSec") == ["0"], f"{name}: start rate limit must be disabled"
        assert "StartLimitBurst" not in u, f"{name}: StartLimitBurst is meaningless with interval 0"


def test_guard_and_heartbeat_timeouts_allow_slow_freqtrade() -> None:
    """Finding: token login + 4 API calls + forceexit + alerts with 10 s timeouts each can exceed
    55 s; a SIGTERM between forceexit and writing killswitch.lock must not happen."""
    guard = unit("btctrader-guard.service")
    heartbeat = unit("btctrader-heartbeat.service")
    assert seconds(guard["TimeoutStartSec"][0]) >= 240
    assert seconds(heartbeat["TimeoutStartSec"][0]) >= 120
    assert guard["Type"] == ["oneshot"] and heartbeat["Type"] == ["oneshot"]


def test_trade_key_file_is_loaded_by_the_guard_only() -> None:
    """Finding: the trade key for cancelOrdersAfter must not reach the dashboard (no login),
    the advisor or the bots. The guard CLI defaults to /etc/freqtrade/secrets-guard.env."""
    guard = unit("btctrader-guard.service")
    assert "-/etc/freqtrade/secrets-guard.env" in guard["EnvironmentFile"], "guard must load it with -"
    for path in SYSTEMD.glob("*.service"):
        if path.name == "btctrader-guard.service":
            continue
        assert "secrets-guard.env" not in path.read_text(encoding="utf-8"), path.name


def test_btctrader_env_template_has_no_trade_key() -> None:
    lines = active_lines(ENV_DIR / "btctrader.env.example")
    assert not [ln for ln in lines if ln.startswith("GUARD_TRADE_API_")], "trade key belongs in secrets-guard"


def test_secrets_guard_template() -> None:
    lines = active_lines(ENV_DIR / "secrets-guard.env.example")
    assert lines == ["GUARD_TRADE_API_KEY=", "GUARD_TRADE_API_SECRET="]
    # install.sh copies every template under deploy/container/env/
    loop = re.search(r"^for name in (.+); do$", INSTALL_SH.read_text(encoding="utf-8"), re.M)
    assert loop, "template loop not found in install.sh"
    installed = set(loop.group(1).split())
    templates = {p.name.removesuffix(".example") for p in ENV_DIR.glob("*.example")}
    assert templates == installed


def test_read_write_paths_are_scoped_per_unit() -> None:
    """Finding: ReadWritePaths=/srv/trading gave read-only services write access to the strategy
    code (repo) and to user_data. Each btctrader unit gets its own directory only. The ledger
    keeps user_data: a mode=ro SQLite connection to the WAL Freqtrade DB still has to create the
    -shm file when the bot is stopped (verified empirically, fails on a read-only mount)."""
    expected = {
        "btctrader-dashboard.service": {"/srv/trading/ledger"},
        "btctrader-advisor.service": {"/srv/trading/advisor"},
        "btctrader-guard.service": {"/srv/trading/guard"},
        "btctrader-heartbeat.service": set(),
        "btctrader-ledger.service": {"/srv/trading/ledger", "/srv/trading/user_data"},
    }
    for name, paths in expected.items():
        u = unit(name)
        assert u.get("ProtectSystem") == ["strict"], name
        got = {p for value in u.get("ReadWritePaths", []) for p in value.split()}
        assert got == paths, name
        assert "/srv/trading/repo" not in got and "/srv/trading" not in got, name


def test_units_keep_lxc_compatible_hardening() -> None:
    """Options that need /proc bind mounts fail in an unprivileged LXC; they must stay out."""
    forbidden = {
        "ProtectKernelTunables",
        "ProtectKernelLogs",
        "ProtectClock",
        "PrivateDevices",
        "SystemCallFilter",
    }
    for path in SYSTEMD.glob("*.service"):
        assert not forbidden & set(unit(path.name)), path.name


# -- env templates and placeholder check -----------------------------------------------


def test_benchmark_start_ships_as_placeholder() -> None:
    """Finding: a hardcoded date passes the placeholder check and computes the benchmarks from
    the wrong price. The template must carry a placeholder that config.py rejects."""
    template = ENV_DIR / "btctrader.env.example"
    line = next(ln for ln in active_lines(template) if ln.startswith("BENCHMARK_START="))
    assert re.match(PLACEHOLDER_RE.replace("[[:space:]]", r"\s"), line), line
    with pytest.raises(ConfigError, match="BENCHMARK_START"):
        load_settings({"BTCTRADER_ENV_FILE": str(template)})


def test_placeholder_regex_in_docs_matches_install_sh(tmp_path: Path) -> None:
    """Finding: docs/SETUP.md counted commented placeholders (option b in secrets-dryrun.env)
    while install.sh only looks at active lines. Both must use the same expression."""
    assert f"grep -HnE '{PLACEHOLDER_RE}'" in INSTALL_SH.read_text(encoding="utf-8")
    assert f"grep -cE '{PLACEHOLDER_RE}'" in SETUP_MD.read_text(encoding="utf-8")
    env = tmp_path / "secrets-dryrun.env"
    env.write_text(
        "# FREQTRADE__EXCHANGE__KEY=BITVAVO_VIEW_KEY_PLACEHOLDER\n"
        "# FREQTRADE__EXCHANGE__SECRET=BITVAVO_VIEW_SECRET_PLACEHOLDER\n"
        "FREQTRADE__TELEGRAM__ENABLED=true\n"
    )
    count = subprocess.run(["grep", "-cE", PLACEHOLDER_RE, str(env)], capture_output=True, text=True)
    assert count.stdout.strip() == "0", "commented placeholders must not count"
    env.write_text("FT_API_PASS=CHANGE_ME_API_PASSWORD\nBENCHMARK_START=CHANGE_ME_YYYY-MM-DD\n")
    count = subprocess.run(["grep", "-cE", PLACEHOLDER_RE, str(env)], capture_output=True, text=True)
    assert count.stdout.strip() == "2"


def test_shipped_templates_have_only_intended_placeholders() -> None:
    """secrets-guard.env ships empty (no placeholder: the key is optional until Phase 4);
    btctrader.env and secrets-dryrun.env ship placeholders that install.sh refuses."""
    pattern = re.compile(PLACEHOLDER_RE.replace("[[:space:]]", r"\s"))
    hits = {
        name: [ln for ln in active_lines(ENV_DIR / f"{name}.example") if pattern.match(ln)]
        for name in ("btctrader.env", "secrets-dryrun.env", "secrets.env", "secrets-guard.env")
    }
    assert [ln.split("=")[0] for ln in hits["btctrader.env"]] == ["FT_API_PASS", "BENCHMARK_START"]
    assert len(hits["secrets-dryrun.env"]) == 2
    assert len(hits["secrets.env"]) == 2
    assert hits["secrets-guard.env"] == []


# -- install.sh enable order -----------------------------------------------------------


def test_enable_dryrun_starts_monitoring_before_the_bot() -> None:
    """Finding: with set -e a bot that fails to reach READY aborted the script before the
    timers and the dashboard were enabled. Monitoring first, the bot last and non-fatal."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    block = text[text.index('if [[ "${ENABLE_DRYRUN}" == "1" ]]; then') : text.index("[install] done.")]
    timers = block.index("btctrader-guard.timer btctrader-heartbeat.timer btctrader-ledger.timer")
    dashboard = block.index("systemctl enable --now btctrader-dashboard.service")
    bot = block.index("systemctl enable --now freqtrade-dryrun.service")
    assert timers < dashboard < bot
    after_bot = block[bot:].split("\n", 2)
    assert "|| warn" in after_bot[0] + after_bot[1], "bot start failure must be a warning, not an abort"
    assert "systemctl enable --now freqtrade.service" not in block, "live unit never enabled"


# -- sync-config.sh secret check -------------------------------------------------------


def run_sync(tmp_path: Path, cfg: dict) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    (repo / "user_data").mkdir(parents=True)
    (repo / "user_data" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    userdir = tmp_path / "userdir"
    userdir.mkdir()
    env = {"REPO_DIR": str(repo), "USERDIR": str(userdir), "OWNER": "no-such-user", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["bash", str(SYNC_SH)], env=env, capture_output=True, text=True)


def test_sync_config_copies_the_repo_config(tmp_path: Path) -> None:
    cfg = json.loads((REPO / "user_data" / "config.json").read_text(encoding="utf-8"))
    result = run_sync(tmp_path, cfg)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "userdir" / "config.json").is_file()


@pytest.mark.parametrize(
    "section,field",
    [
        ("exchange", "key"),
        ("exchange", "secret"),
        ("exchange", "password"),
        ("exchange", "uid"),
        ("telegram", "token"),
        ("telegram", "chat_id"),
        ("api_server", "password"),
        ("api_server", "jwt_secret_key"),
        ("api_server", "ws_token"),
    ],
)
def test_sync_config_refuses_every_secret_field(tmp_path: Path, section: str, field: str) -> None:
    """Finding: jwt_secret_key, ws_token and chat_id were not checked; a jwt_secret_key in the
    public config.json lets anyone mint tokens for the REST API published via tailscale."""
    cfg = json.loads((REPO / "user_data" / "config.json").read_text(encoding="utf-8"))
    cfg.setdefault(section, {})[field] = "leaked-value"
    result = run_sync(tmp_path, cfg)
    assert result.returncode == 1
    assert f"{section}.{field}" in result.stderr
    assert not (tmp_path / "userdir" / "config.json").exists()


# -- create-lxc.sh and firewall --------------------------------------------------------


def test_create_lxc_warns_when_ct_dns_is_empty_and_documents_the_resolver_alias() -> None:
    """Finding: the firewall allows DNS only to the `resolver` alias while CT_DNS defaulted to
    'inherit from the host' without any hint."""
    text = CREATE_LXC_SH.read_text(encoding="utf-8")
    assert "resolver" in text.split("set -euo pipefail")[0], "header must tie CT_DNS to the resolver alias"
    assert 'if [[ -z "${CT_DNS}" && -f "${FW_TARGET}" ]]; then' in text
    assert "--nameserver" in text
    fw = FIREWALL.read_text(encoding="utf-8")
    resolver = re.search(r"^resolver\s+(\S+)", fw, re.M)
    assert resolver, "resolver alias missing in 210.fw"
    assert f"CT_DNS={resolver.group(1)} CT_IP=" in SETUP_MD.read_text(encoding="utf-8")
    assert re.search(r"^OUT ACCEPT -dest resolver -p udp -dport 53", fw, re.M)


# -- docs describe the code ------------------------------------------------------------


def test_setup_bitvavo_error_codes_match_ccxt() -> None:
    """ccxt bitvavo: 305/306 = no active API key (AuthenticationError), 307 = IP not allowed."""
    text = SETUP_MD.read_text(encoding="utf-8")
    assert "`errorCode 307`" in text and "Whitelist" in text.split("`errorCode 307`", 1)[1][:120]
    assert "`errorCode 305`" in text
    assert "305...}` bedeutet: IP" not in text


def test_setup_healthchecks_test_expects_the_fail_ping() -> None:
    """heartbeat.py pings HEALTHCHECKS_URL/fail as soon as /health fails; the alert does not
    wait for the grace period."""
    text = SETUP_MD.read_text(encoding="utf-8")
    assert "nach 3 bis 4 Minuten kommt die Healthchecks-Benachrichtigung" not in text
    assert "`HEALTHCHECKS_URL/fail`" in text


def test_betrieb_killswitch_test_raises_the_peak() -> None:
    """guard.py: drawdown = (peak - equity) / peak with peak = max(peak, equity). Without an open
    position the dry-run equity is constant, so a small limit alone can never fire."""
    section = BETRIEB_MD.read_text(encoding="utf-8").split("## 5. Kill-Switch")[1].split("## 6.")[0]
    assert "peak_equity" in section and "state.json" in section
    assert "btctrader-guard check" in section
    assert "eine Minute warten, Alarm und Lock prüfen" not in section


def test_betrieb_restore_keeps_the_firewall_file() -> None:
    """pve-container: vzdump stores /etc/pve/firewall/<vmid>.fw as etc/vzdump/pct.fw and
    pct restore writes it back."""
    text = BETRIEB_MD.read_text(encoding="utf-8")
    assert "liegt außerhalb des Backups" not in text
    assert "etc/vzdump/pct.fw" in text


def test_docs_mention_reset_failed_and_mount_namespacing() -> None:
    assert "systemctl reset-failed" in BETRIEB_MD.read_text(encoding="utf-8")
    assert "reset-failed" in NUT_README.read_text(encoding="utf-8")
    setup = SETUP_MD.read_text(encoding="utf-8")
    assert "Failed to set up mount namespacing" in setup and "nesting=1" in setup
    assert "secrets-guard.env" in setup and "secrets-guard.env" in BETRIEB_MD.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_units_have_no_unknown_directives(tmp_path: Path) -> None:
    """systemd-analyze verify flags unknown keys and syntax errors. Missing executables are
    reported too (the venvs do not exist here), so only directive problems are asserted."""
    for path in SYSTEMD.iterdir():
        shutil.copy(path, tmp_path / path.name)
    result = subprocess.run(
        ["systemd-analyze", "verify", "--man=no", *(str(tmp_path / p.name) for p in SYSTEMD.iterdir())],
        capture_output=True,
        text=True,
    )
    problems = [
        ln
        for ln in (result.stdout + result.stderr).splitlines()
        if ("Unknown" in ln or "Failed to parse" in ln or "Invalid" in ln) and "is not executable" not in ln
    ]
    assert not problems, "\n".join(problems)
