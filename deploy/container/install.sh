#!/usr/bin/env bash
# Install or update the trading stack inside the Debian 13 container (run as root in the CT).
#
# Layout after the run (docs/KOMPONENTEN.md section 2):
#   /opt/freqtrade/venv        Freqtrade ${FT_VERSION} from PyPI (+ .venv symlink for the scripts/)
#   /srv/trading/repo          checkout of this repository (owner freqtrade)
#   /srv/trading/venv          venv with `pip install -e /srv/trading/repo` (btctrader CLIs)
#   /srv/trading/user_data     Freqtrade userdir; config.json copied from the repo, strategies -> repo
#   /srv/trading/{advisor,ledger,guard}
#   /etc/freqtrade/*.env       templates copied once (root:freqtrade 0640), never overwritten;
#                              secrets-guard.env (trade key for the dead man's switch) is loaded by the
#                              guard unit only
#   /etc/systemd/system/       units from deploy/container/systemd/
#
# Parameters (environment, all optional):
#   REPO_URL=          git URL to clone when /srv/trading/repo does not exist yet. When empty and this
#                      script lives inside a checkout, that checkout is cloned locally instead.
#   REPO_REF=main      branch or tag to check out on a fresh clone
#   REPO_UPDATE=0      1 = `git pull --ff-only` in an existing /srv/trading/repo
#   FT_VERSION=2026.8  Freqtrade version pin (only change after docs/BETRIEB.md "Freqtrade aktualisieren")
#   INSTALL_TAILSCALE=1  install tailscale from the official apt repo (does not run `tailscale up`)
#   FETCH_VENDOR=1     download the dashboard vendor JS (chart.js, htmx) with SRI check
#   ENABLE_DRYRUN=0    1 = enable and start freqtrade-dryrun.service, the timers and the dashboard.
#                      Refused while placeholders remain in the env files or config-private.json.
#
# The script is idempotent: re-run it after a `git pull` to update the venv and the units.
# It never enables or starts freqtrade.service (the live unit with the trade key).
set -euo pipefail

FT_VERSION="${FT_VERSION:-2026.8}"
REPO_URL="${REPO_URL:-}"
REPO_REF="${REPO_REF:-main}"
REPO_UPDATE="${REPO_UPDATE:-0}"
INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-1}"
FETCH_VENDOR="${FETCH_VENDOR:-1}"
ENABLE_DRYRUN="${ENABLE_DRYRUN:-0}"

SVC_USER=freqtrade
SVC_HOME=/srv/trading
REPO_DIR="${SVC_HOME}/repo"
APP_VENV="${SVC_HOME}/venv"
USERDIR="${SVC_HOME}/user_data"
FT_HOME=/opt/freqtrade
FT_VENV="${FT_HOME}/venv"
ETC_DIR=/etc/freqtrade

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo this script was started from (may be /root/BitCoin-Trader on the first run).
SCRIPT_REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }
as_svc() { runuser -u "${SVC_USER}" -- "$@"; }

[[ "$(id -u)" -eq 0 ]] || die "run as root inside the container"
command -v apt-get >/dev/null 2>&1 || die "apt-get not found: this script targets Debian 13"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "debian" || "${VERSION_ID:-}" != "13" ]]; then
    warn "expected Debian 13 (trixie), found ${PRETTY_NAME:-unknown}; continuing anyway"
  fi
fi

# -- 1. packages ------------------------------------------------------------------------

log "installing apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  build-essential pkg-config libffi-dev libssl-dev \
  git curl ca-certificates gnupg sqlite3 jq less openssl
# The CT shares the host kernel clock and chrony runs on the host, so no time daemon in here.
if dpkg -s systemd-timesyncd >/dev/null 2>&1; then
  apt-get purge -y -q systemd-timesyncd
fi

# Timezone UTC inside the CT: all logs, timers and stored timestamps are UTC.
if [[ "$(cat /etc/timezone 2>/dev/null || true)" != "Etc/UTC" ]]; then
  ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime
  echo "Etc/UTC" > /etc/timezone
  log "timezone set to UTC"
fi

# -- 2. user and directories ------------------------------------------------------------

if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
  log "creating user ${SVC_USER} with home ${SVC_HOME}"
  useradd --system --create-home --home-dir "${SVC_HOME}" --shell /usr/sbin/nologin "${SVC_USER}"
fi
install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${SVC_HOME}"
for d in advisor ledger ledger/exports ledger/reports guard; do
  install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${SVC_HOME}/${d}"
done
install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0755 "${FT_HOME}"
install -d -o root -g "${SVC_USER}" -m 0750 "${ETC_DIR}"

# -- 3. Freqtrade venv (pinned) ---------------------------------------------------------

if [[ ! -x "${FT_VENV}/bin/python" ]]; then
  log "creating Freqtrade venv ${FT_VENV}"
  as_svc python3 -m venv "${FT_VENV}"
fi
# scripts/download-data.sh and scripts/backtest.sh default to /opt/freqtrade/.venv
if [[ ! -e "${FT_HOME}/.venv" ]]; then
  as_svc ln -s venv "${FT_HOME}/.venv"
fi
INSTALLED_FT="$("${FT_VENV}/bin/pip" show freqtrade 2>/dev/null | sed -n 's/^Version: //p' || true)"
if [[ "${INSTALLED_FT}" != "${FT_VERSION}" ]]; then
  log "installing freqtrade==${FT_VERSION} (installed: ${INSTALLED_FT:-none})"
  as_svc "${FT_VENV}/bin/pip" install -q --upgrade pip wheel
  as_svc "${FT_VENV}/bin/pip" install -q "freqtrade==${FT_VERSION}"
else
  log "freqtrade ${FT_VERSION} already installed"
fi
"${FT_VENV}/bin/freqtrade" --version | sed 's/^/[install]   /'

UI_DIR="$("${FT_VENV}/bin/python" -c 'import freqtrade, pathlib; print(pathlib.Path(freqtrade.__file__).parent / "rpc/api_server/ui/installed")')"
if [[ ! -f "${UI_DIR}/index.html" ]]; then
  log "installing FreqUI (freqtrade install-ui)"
  if ! as_svc "${FT_VENV}/bin/freqtrade" install-ui; then
    warn "install-ui failed (network?). FreqUI is optional; re-run later: ${FT_VENV}/bin/freqtrade install-ui"
  fi
else
  log "FreqUI already installed"
fi

# -- 4. repository ----------------------------------------------------------------------

# Clones and pulls run as root (root's SSH keys or tokens reach private remotes), then the
# checkout is handed to the service user. safe.directory covers the ownership mismatch.
git_root() { git -c "safe.directory=${REPO_DIR}" "$@"; }
if [[ -d "${REPO_DIR}/.git" ]]; then
  log "repo present: ${REPO_DIR}"
  if [[ "${REPO_UPDATE}" == "1" ]]; then
    log "git pull --ff-only"
    git_root -C "${REPO_DIR}" pull --ff-only
  fi
else
  if [[ -n "${REPO_URL}" ]]; then
    log "cloning ${REPO_URL} (${REPO_REF}) -> ${REPO_DIR}"
    git clone --branch "${REPO_REF}" "${REPO_URL}" "${REPO_DIR}"
  elif [[ -d "${SCRIPT_REPO}/.git" ]]; then
    log "no REPO_URL: cloning the local checkout ${SCRIPT_REPO} -> ${REPO_DIR}"
    git clone "${SCRIPT_REPO}" "${REPO_DIR}"
    if ORIGIN="$(git -C "${SCRIPT_REPO}" remote get-url origin 2>/dev/null)"; then
      git_root -C "${REPO_DIR}" remote set-url origin "${ORIGIN}"
      log "origin of ${REPO_DIR} set to ${ORIGIN}"
    else
      warn "${REPO_DIR} has no remote; set one with: git -C ${REPO_DIR} remote set-url origin <url>"
    fi
  else
    die "no repo: set REPO_URL=<git url> or run this script from a checkout"
  fi
fi
chown -R "${SVC_USER}:${SVC_USER}" "${REPO_DIR}"
[[ -f "${REPO_DIR}/pyproject.toml" ]] || die "${REPO_DIR} is not the BitCoin-Trader repo"

# -- 5. btctrader venv ------------------------------------------------------------------

if [[ ! -x "${APP_VENV}/bin/python" ]]; then
  log "creating btctrader venv ${APP_VENV}"
  as_svc python3 -m venv "${APP_VENV}"
  as_svc "${APP_VENV}/bin/pip" install -q --upgrade pip wheel
fi
log "pip install -e ${REPO_DIR}"
as_svc "${APP_VENV}/bin/pip" install -q -e "${REPO_DIR}"
for cli in btctrader-advisor btctrader-ledger btctrader-guard btctrader-dashboard; do
  [[ -x "${APP_VENV}/bin/${cli}" ]] || die "entry point missing after install: ${APP_VENV}/bin/${cli}"
done

# -- 6. Freqtrade userdir ---------------------------------------------------------------

if [[ ! -d "${USERDIR}" ]]; then
  log "freqtrade create-userdir --userdir ${USERDIR}"
  as_svc "${FT_VENV}/bin/freqtrade" create-userdir --userdir "${USERDIR}"
fi
for d in data logs backtest_results hyperopt_results plot notebooks; do
  install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 0750 "${USERDIR}/${d}"
done

# strategies -> repo (create-userdir drops sample_strategy.py into a real directory first)
STRAT_LINK="${USERDIR}/strategies"
STRAT_TARGET="${REPO_DIR}/user_data/strategies"
if [[ -L "${STRAT_LINK}" ]]; then
  if [[ "$(readlink -f "${STRAT_LINK}")" != "$(readlink -f "${STRAT_TARGET}")" ]]; then
    log "repointing strategies symlink to ${STRAT_TARGET}"
    as_svc ln -sfn "${STRAT_TARGET}" "${STRAT_LINK}"
  fi
elif [[ -d "${STRAT_LINK}" ]]; then
  rm -f "${STRAT_LINK}/sample_strategy.py"
  rm -rf "${STRAT_LINK}/__pycache__"
  if [[ -z "$(ls -A "${STRAT_LINK}")" ]]; then
    rmdir "${STRAT_LINK}"
  else
    BACKUP="${STRAT_LINK}.orig-$(date -u +%Y%m%dT%H%M%SZ)"
    warn "moving non-empty ${STRAT_LINK} to ${BACKUP}"
    mv "${STRAT_LINK}" "${BACKUP}"
  fi
  as_svc ln -s "${STRAT_TARGET}" "${STRAT_LINK}"
  log "strategies -> ${STRAT_TARGET}"
else
  as_svc ln -s "${STRAT_TARGET}" "${STRAT_LINK}"
  log "strategies -> ${STRAT_TARGET}"
fi
# remove other sample files create-userdir may have added
rm -f "${USERDIR}/hyperopts/sample_hyperopt_loss.py" "${USERDIR}/notebooks/strategy_analysis_example.ipynb"

SYNC_SCRIPT="${REPO_DIR}/deploy/container/sync-config.sh"
REPO_DIR="${REPO_DIR}" USERDIR="${USERDIR}" OWNER="${SVC_USER}" bash "${SYNC_SCRIPT}"

PRIVATE="${USERDIR}/config-private.json"
if [[ ! -f "${PRIVATE}" ]]; then
  install -o "${SVC_USER}" -g "${SVC_USER}" -m 0600 "${REPO_DIR}/user_data/config-private.example.json" "${PRIVATE}"
  log "created ${PRIVATE} from the example (fill it: docs/SETUP.md step 5)"
else
  chown "${SVC_USER}:${SVC_USER}" "${PRIVATE}"
  chmod 0600 "${PRIVATE}"
fi

# -- 7. env templates -------------------------------------------------------------------

for name in btctrader.env secrets-dryrun.env secrets.env secrets-guard.env; do
  target="${ETC_DIR}/${name}"
  if [[ ! -f "${target}" ]]; then
    install -o root -g "${SVC_USER}" -m 0640 "${REPO_DIR}/deploy/container/env/${name}.example" "${target}"
    log "created ${target} from the template"
  else
    chown "root:${SVC_USER}" "${target}"
    chmod 0640 "${target}"
  fi
done

# -- 8. systemd units, journald -----------------------------------------------------------

UNIT_SRC="${REPO_DIR}/deploy/container/systemd"
changed=0
for unit in "${UNIT_SRC}"/*.service "${UNIT_SRC}"/*.timer; do
  name="$(basename "${unit}")"
  if ! cmp -s "${unit}" "/etc/systemd/system/${name}"; then
    install -o root -g root -m 0644 "${unit}" "/etc/systemd/system/${name}"
    changed=1
  fi
done
install -d /etc/systemd/journald.conf.d
JOURNAL_CONF=/etc/systemd/journald.conf.d/50-btctrader.conf
if ! cmp -s <(printf '[Journal]\nSystemMaxUse=500M\nMaxRetentionSec=1year\n') "${JOURNAL_CONF}" 2>/dev/null; then
  printf '[Journal]\nSystemMaxUse=500M\nMaxRetentionSec=1year\n' > "${JOURNAL_CONF}"
  systemctl restart systemd-journald.service || warn "journald restart failed"
  log "journald limits written to ${JOURNAL_CONF}"
fi
systemctl daemon-reload
[[ "${changed}" == "1" ]] && log "systemd units installed/updated (daemon-reload done)"
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify /etc/systemd/system/freqtrade-dryrun.service /etc/systemd/system/btctrader-*.service \
    /etc/systemd/system/btctrader-*.timer 2>&1 | sed 's/^/[install]   verify: /' || true
fi

# -- 9. tailscale -----------------------------------------------------------------------

if [[ "${INSTALL_TAILSCALE}" == "1" ]]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    log "installing tailscale from pkgs.tailscale.com (Debian trixie)"
    curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
      -o /etc/apt/sources.list.d/tailscale.list
    apt-get update -q
    apt-get install -y -q tailscale
  fi
  systemctl enable --now tailscaled.service
  if [[ ! -c /dev/net/tun ]]; then
    warn "/dev/net/tun is missing in the container: on the host run  pct set <CTID> --dev0 path=/dev/net/tun  and restart the CT"
  fi
fi

# -- 10. dashboard vendor JS ------------------------------------------------------------

VENDOR_SCRIPT="${REPO_DIR}/btctrader/dashboard/static/vendor/fetch-vendor.sh"
if [[ "${FETCH_VENDOR}" == "1" && -f "${VENDOR_SCRIPT}" ]]; then
  log "fetching dashboard vendor JS"
  if ! as_svc bash "${VENDOR_SCRIPT}"; then
    warn "vendor download failed; the dashboard falls back to the jsDelivr CDN"
  fi
fi

# -- 11. enable (only on request) -----------------------------------------------------------

placeholders_left() {
  # Any CHANGE_ME / PLACEHOLDER value in an active (uncommented) line of the given files.
  grep -HnE '^[[:space:]]*[A-Za-z_]+=.*(CHANGE_ME|PLACEHOLDER)' "$@" 2>/dev/null || true
}

if [[ "${ENABLE_DRYRUN}" == "1" ]]; then
  problems="$(placeholders_left "${ETC_DIR}/btctrader.env" "${ETC_DIR}/secrets-dryrun.env")"
  if grep -qE 'CHANGE_ME|PLACEHOLDER' "${PRIVATE}"; then
    problems+=$'\n'"${PRIVATE}: placeholders present"
  fi
  if [[ -n "${problems//[[:space:]]/}" ]]; then
    printf '%s\n' "${problems}" >&2
    die "placeholders left in the configuration; fill them first (docs/SETUP.md step 5)"
  fi
  log "enabling timers, dashboard and dry-run bot"
  # Monitoring first: when the bot fails to reach READY (wrong key, API password, exchange down)
  # the guard, heartbeat and dashboard must already be active instead of the script aborting.
  systemctl enable --now btctrader-guard.timer btctrader-heartbeat.timer btctrader-ledger.timer btctrader-advisor.timer
  systemctl enable --now btctrader-dashboard.service \
    || warn "btctrader-dashboard failed to start: journalctl -u btctrader-dashboard -n 50"
  systemctl enable --now freqtrade-dryrun.service \
    || warn "freqtrade-dryrun failed to start (it keeps retrying every 10 s): journalctl -u freqtrade-dryrun -n 50"
  systemctl --no-pager --no-legend list-timers 'btctrader-*' | sed 's/^/[install]   /'
else
  log "ENABLE_DRYRUN not set: nothing enabled or started (freqtrade.service is never started by this script)"
fi

cat <<EOF

[install] done.
  Freqtrade:   ${FT_VENV}/bin/freqtrade   (version ${FT_VERSION})
  btctrader:   ${APP_VENV}/bin/btctrader-{advisor,ledger,guard,dashboard}
  Repo:        ${REPO_DIR}
  Userdir:     ${USERDIR}   (config.json synced, config-private.json 0600)
  Env files:   ${ETC_DIR}/btctrader.env, secrets-dryrun.env, secrets.env, secrets-guard.env (root:${SVC_USER} 0640)

Next: fill the env files and config-private.json (docs/SETUP.md, Schritt 5), run
  tailscale up
then start the dry-run with
  ENABLE_DRYRUN=1 bash ${REPO_DIR}/deploy/container/install.sh
EOF
