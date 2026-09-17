#!/usr/bin/env bash
# Create the unprivileged Debian 13 LXC for the trading bot on a Proxmox VE 9 host.
#
# Run as root on the PVE host. All parameters come from the environment; only
# CT_IP and CT_GW have no usable default because they depend on your LAN.
#
#   CTID=210                  container id
#   CT_HOSTNAME=btc-bot       container hostname (HOSTNAME itself is reserved by bash for the
#                             host's own name, so the parameter is called CT_HOSTNAME)
#   STORAGE=local-zfs         storage for the root disk (ZFS or LVM-thin for vzdump snapshot mode)
#   TEMPLATE_STORAGE=local    storage that holds the CT templates (vztmpl)
#   TEMPLATE=                 template volume id, e.g. local:vztmpl/debian-13-standard_13.1-1_amd64.tar.zst;
#                             empty = newest debian-13-standard from `pveam available`, downloaded if needed
#   CT_IP=192.168.1.210/24    static LAN address in CIDR notation (required)
#   CT_GW=192.168.1.1         default gateway (required)
#   CT_DNS=                   nameserver for the container (empty = inherit from the host).
#                             Set it to the same address as the `resolver` alias in the firewall
#                             file: the rules allow DNS only to that alias, and a host that
#                             resolves via 9.9.9.9 or the ISP would leave the CT without DNS.
#   BRIDGE=vmbr0              bridge for net0
#   CORES=2 MEMORY=4096 SWAP=512 ROOTFS_GB=32
#   SSH_KEYS=                 path to an authorized_keys file for root inside the CT (optional)
#   FIREWALL_SRC=             firewall file to install as /etc/pve/firewall/<CTID>.fw
#                             (default: firewall/<CTID>.fw next to this script, if it exists)
#   START=1                   start the container at the end (0 = only create)
#
# What the script does (idempotent, every step checks first):
#   1. verifies pveversion / pct / storage,
#   2. downloads the Debian 13 template when missing,
#   3. pct create: unprivileged, no nesting/keyctl features, static IP, onboot, startup order 20
#      with 30 s delay, root disk on ZFS, firewall=1 on net0,
#   4. passes /dev/net/tun through with the PVE 9 device passthrough syntax (--dev0, see
#      https://pve.proxmox.com/pve-docs/pct.1.html and https://tailscale.com/kb/1130/lxc-unprivileged),
#   5. installs the firewall file when none exists for the CT,
#   6. starts the container and prints the next steps.
#
# Nothing here touches other containers. Re-running on an existing CTID only adds missing settings.
set -euo pipefail

CTID="${CTID:-210}"
CT_HOSTNAME="${CT_HOSTNAME:-btc-bot}"
STORAGE="${STORAGE:-local-zfs}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
TEMPLATE="${TEMPLATE:-}"
CT_IP="${CT_IP:-}"
CT_GW="${CT_GW:-}"
CT_DNS="${CT_DNS:-}"
BRIDGE="${BRIDGE:-vmbr0}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-4096}"
SWAP="${SWAP:-512}"
ROOTFS_GB="${ROOTFS_GB:-32}"
SSH_KEYS="${SSH_KEYS:-}"
START="${START:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIREWALL_SRC="${FIREWALL_SRC:-${SCRIPT_DIR}/firewall/${CTID}.fw}"

log() { printf '[create-lxc] %s\n' "$*"; }
die() { printf '[create-lxc] ERROR: %s\n' "$*" >&2; exit 1; }

# -- 0. preconditions -------------------------------------------------------------------

[[ "$(id -u)" -eq 0 ]] || die "run as root on the Proxmox host"
command -v pct >/dev/null 2>&1 || die "pct not found: this is not a Proxmox VE host"
command -v pveam >/dev/null 2>&1 || die "pveam not found"
command -v pvesm >/dev/null 2>&1 || die "pvesm not found"

PVE_VERSION="$(pveversion 2>/dev/null | sed -n 's#^pve-manager/\([0-9.]*\).*#\1#p')"
log "pveversion: ${PVE_VERSION:-unknown} (this script targets PVE 9.x)"
case "${PVE_VERSION}" in
  9.*) ;;
  *) log "WARNING: not tested on PVE ${PVE_VERSION:-unknown}; the --dev0 syntax needs PVE >= 8.2" ;;
esac

[[ "${CTID}" =~ ^[0-9]+$ ]] || die "CTID must be numeric, got '${CTID}'"
[[ "${CTID}" -ge 100 ]] || die "CTID must be >= 100"

if [[ -z "${CT_IP}" || -z "${CT_GW}" ]]; then
  die "CT_IP (CIDR, e.g. 192.168.1.210/24) and CT_GW (e.g. 192.168.1.1) are required"
fi
[[ "${CT_IP}" == */* ]] || die "CT_IP must be in CIDR notation (address/prefix), got '${CT_IP}'"

if ! pvesm status 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "${STORAGE}"; then
  die "storage '${STORAGE}' not found (pvesm status)"
fi
if ! pvesm status 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "${TEMPLATE_STORAGE}"; then
  die "template storage '${TEMPLATE_STORAGE}' not found (pvesm status)"
fi

# -- 1. template ------------------------------------------------------------------------

if [[ -z "${TEMPLATE}" ]]; then
  # Newest debian-13-standard template known to pveam. Update the index first (harmless offline).
  pveam update >/dev/null 2>&1 || log "WARNING: pveam update failed, using the cached index"
  TEMPLATE_FILE="$(pveam available --section system 2>/dev/null | awk '{print $2}' \
    | grep -E '^debian-13-standard_.*\.tar\.(zst|xz|gz)$' | sort -V | tail -n 1 || true)"
  [[ -n "${TEMPLATE_FILE}" ]] || die "no debian-13-standard template in 'pveam available'; set TEMPLATE explicitly"
  TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE_FILE}"
else
  TEMPLATE_FILE="${TEMPLATE##*/}"
fi

if pveam list "${TEMPLATE_STORAGE}" 2>/dev/null | awk '{print $1}' | grep -qx "${TEMPLATE}"; then
  log "template present: ${TEMPLATE}"
else
  log "downloading template ${TEMPLATE_FILE} to ${TEMPLATE_STORAGE}"
  pveam download "${TEMPLATE_STORAGE}" "${TEMPLATE_FILE}"
fi

# -- 2. create the container ------------------------------------------------------------

ct_exists() { pct status "${CTID}" >/dev/null 2>&1; }

if ct_exists; then
  log "CT ${CTID} already exists, skipping pct create (existing settings are kept)"
else
  NET0="name=eth0,bridge=${BRIDGE},firewall=1,ip=${CT_IP},gw=${CT_GW}"
  CREATE_ARGS=(
    "${CTID}" "${TEMPLATE}"
    --hostname "${CT_HOSTNAME}"
    --ostype debian
    --unprivileged 1
    --cores "${CORES}"
    --memory "${MEMORY}"
    --swap "${SWAP}"
    --rootfs "${STORAGE}:${ROOTFS_GB}"
    --net0 "${NET0}"
    --onboot 1
    --startup "order=20,up=30"
    --dev0 "path=/dev/net/tun"
    --timezone UTC
  )
  # No --features: nesting and keyctl are not needed for Freqtrade, Python and tailscaled.
  # If `tailscale up` ever complains about keyctl on your kernel, add: pct set CTID --features keyctl=1
  if [[ -n "${CT_DNS}" ]]; then
    CREATE_ARGS+=(--nameserver "${CT_DNS}")
  fi
  if [[ -n "${SSH_KEYS}" ]]; then
    [[ -r "${SSH_KEYS}" ]] || die "SSH_KEYS file not readable: ${SSH_KEYS}"
    CREATE_ARGS+=(--ssh-public-keys "${SSH_KEYS}")
  fi
  log "pct create ${CTID} (${CT_HOSTNAME}, ${CORES} cores, ${MEMORY} MB, ${STORAGE}:${ROOTFS_GB}G, ip ${CT_IP})"
  pct create "${CREATE_ARGS[@]}"
fi

# -- 3. make sure the important settings are present on re-runs -------------------------

CONF="$(pct config "${CTID}")"

if ! grep -qE '^dev[0-9]+: .*(^|[ ,=])/dev/net/tun' <<<"${CONF}"; then
  log "adding /dev/net/tun passthrough (--dev0 path=/dev/net/tun)"
  pct set "${CTID}" --dev0 "path=/dev/net/tun"
fi
if ! grep -qE '^onboot: 1' <<<"${CONF}"; then
  pct set "${CTID}" --onboot 1
fi
if ! grep -qE '^startup: ' <<<"${CONF}"; then
  pct set "${CTID}" --startup "order=20,up=30"
fi
if grep -qE '^unprivileged: 0' <<<"${CONF}" || ! grep -qE '^unprivileged: 1' <<<"${CONF}"; then
  log "WARNING: CT ${CTID} is not unprivileged. Recreate it; privileged containers are not supported here."
fi
if ! grep -qE '^net0: .*firewall=1' <<<"${CONF}"; then
  log "WARNING: net0 has no firewall=1; the PVE firewall rules for this CT are inactive."
  log "         Fix with: pct set ${CTID} --net0 '$(grep -E '^net0: ' <<<"${CONF}" | sed 's/^net0: //'),firewall=1'"
fi

# -- 4. firewall file -------------------------------------------------------------------

FW_TARGET="/etc/pve/firewall/${CTID}.fw"
if [[ -f "${FW_TARGET}" ]]; then
  log "firewall file exists: ${FW_TARGET} (not touched)"
elif [[ -f "${FIREWALL_SRC}" ]]; then
  log "installing firewall rules ${FIREWALL_SRC} -> ${FW_TARGET}"
  cp "${FIREWALL_SRC}" "${FW_TARGET}"
  log "         edit the [ALIASES] section (vLLM host, LAN, resolver) before enabling the datacenter firewall"
else
  log "no firewall file found (${FIREWALL_SRC}); copy deploy/proxmox/firewall/210.fw manually"
fi

if [[ -z "${CT_DNS}" && -f "${FW_TARGET}" ]]; then
  RESOLVER="$(sed -n 's/^resolver[[:space:]]\{1,\}//p' "${FW_TARGET}" | head -n 1)"
  log "WARNING: CT_DNS is empty, the CT inherits the host's /etc/resolv.conf. ${FW_TARGET} allows DNS"
  log "         only to the alias 'resolver' (${RESOLVER:-not set}). If the host uses another resolver,"
  log "         set it now: pct set ${CTID} --nameserver ${RESOLVER:-<resolver>}"
fi

# -- 5. start ---------------------------------------------------------------------------

if [[ "${START}" == "1" ]]; then
  if [[ "$(pct status "${CTID}")" == *running* ]]; then
    log "CT ${CTID} is running"
  else
    log "starting CT ${CTID}"
    pct start "${CTID}"
  fi
fi

cat <<EOF

Done. CT ${CTID} (${CT_HOSTNAME}) with ${CT_IP} via ${CT_GW}.

Next steps:
  1. Datacenter firewall (once, on the host): check /etc/pve/firewall/cluster.fw has "enable: 1"
     under [OPTIONS]. Without it the rules in ${FW_TARGET} are inactive.
     Edit the aliases in ${FW_TARGET} first (vllm_host, lan_admin, resolver). The CT's nameserver
     (pct config ${CTID} | grep nameserver, or the host's /etc/resolv.conf when unset) must be the
     resolver alias, everything else is dropped by the firewall.
  2. Enter the container:      pct enter ${CTID}
  3. Inside, fetch the repo and run the installer:
       apt-get update && apt-get install -y git ca-certificates
       git clone <REPO_URL> /root/BitCoin-Trader
       REPO_URL=<REPO_URL> bash /root/BitCoin-Trader/deploy/container/install.sh
  4. Fill /etc/freqtrade/*.env and /srv/trading/user_data/config-private.json (docs/SETUP.md, Schritt 5).
  5. tailscale up  (docs/SETUP.md, Schritt 6), then
       ENABLE_DRYRUN=1 bash /srv/trading/repo/deploy/container/install.sh
EOF
