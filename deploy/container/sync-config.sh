#!/usr/bin/env bash
# Copy user_data/config.json from the repo checkout into the Freqtrade userdir.
# Only config.json is copied: secrets stay in config-private.json and /etc/freqtrade/*.env,
# which this script never touches. Run as root (chowns to freqtrade) or as freqtrade.
#
#   REPO_DIR=/srv/trading/repo          source checkout
#   USERDIR=/srv/trading/user_data      Freqtrade userdir
#   OWNER=freqtrade                     owner of the copied file (only applied when run as root)
#
# Exit code 0 when the file is unchanged or copied; prints whether a restart of the bot is needed.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/srv/trading/repo}"
USERDIR="${USERDIR:-/srv/trading/user_data}"
OWNER="${OWNER:-freqtrade}"

SRC="${REPO_DIR}/user_data/config.json"
DST="${USERDIR}/config.json"

[[ -f "${SRC}" ]] || { echo "sync-config: source missing: ${SRC}" >&2; exit 1; }
[[ -d "${USERDIR}" ]] || { echo "sync-config: userdir missing: ${USERDIR}" >&2; exit 1; }

# Refuse to copy a config with secrets in it: config.json must keep every secret field empty.
# Checked: exchange.key/secret/password/uid, telegram.token/chat_id, api_server.password/
# jwt_secret_key/ws_token (jwt_secret_key or ws_token alone grant access to the REST API).
if python3 - "${SRC}" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
secret_fields = {
    "exchange": ("key", "secret", "password", "uid"),
    "telegram": ("token", "chat_id"),
    "api_server": ("password", "jwt_secret_key", "ws_token"),
}
leaks = [
    f"{section}.{field}"
    for section, fields in secret_fields.items()
    for field in fields
    if (cfg.get(section) or {}).get(field) not in (None, "", [], {})
]
if leaks:
    print("sync-config: non-empty secret fields: " + ", ".join(leaks), file=sys.stderr)
sys.exit(1 if leaks else 0)
PY
then :; else
  echo "sync-config: ${SRC} contains a key, secret, token, password or chat id. Secrets belong in config-private.json." >&2
  exit 1
fi

if [[ -f "${DST}" ]] && cmp -s "${SRC}" "${DST}"; then
  echo "sync-config: ${DST} is up to date"
  exit 0
fi

tmp="$(mktemp "${DST}.XXXXXX")"
cp "${SRC}" "${tmp}"
chmod 0640 "${tmp}"
if [[ "$(id -u)" -eq 0 ]] && id -u "${OWNER}" >/dev/null 2>&1; then
  chown "${OWNER}:${OWNER}" "${tmp}"
fi
mv -f "${tmp}" "${DST}"
echo "sync-config: copied ${SRC} -> ${DST}"
echo "sync-config: restart the bot to apply: systemctl restart freqtrade-dryrun.service"
