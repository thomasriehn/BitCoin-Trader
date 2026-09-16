#!/usr/bin/env bash
# Downloads the pinned frontend libraries into this directory (called by deploy/container/install.sh).
# The page falls back to the same jsDelivr URLs when a file is missing.
#
# Pinned versions (verified on jsDelivr, 15.09.2026):
#   chart.js 4.5.1  https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.js
#     sha384-hfkuqrKeWFmnTMWN31VWyoe8xgdTADD11kgxmdpx2uyE6j5Az5uZq6u6AKYYmAOw
#   htmx.org 2.0.10 https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js
#     sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V
#
# The SRI hashes above must match CHARTJS_SRI / HTMX_SRI in btctrader/dashboard/app.py.
# Bumping a version: change both places, run this script, check the printed hashes.

set -euo pipefail

CHARTJS_VERSION="4.5.1"
HTMX_VERSION="2.0.10"
CHARTJS_SRI="sha384-hfkuqrKeWFmnTMWN31VWyoe8xgdTADD11kgxmdpx2uyE6j5Az5uZq6u6AKYYmAOw"
HTMX_SRI="sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sri() {
  printf 'sha384-%s' "$(openssl dgst -sha384 -binary "$1" | openssl base64 -A)"
}

fetch() {
  local url="$1" target="$2" expected="$3" tmp
  tmp="$(mktemp "${target}.XXXXXX")"
  echo "fetching ${url}"
  curl -fsSL --retry 3 --retry-delay 2 -o "${tmp}" "${url}"
  local actual
  actual="$(sri "${tmp}")"
  if [[ "${actual}" != "${expected}" ]]; then
    rm -f "${tmp}"
    echo "SRI mismatch for ${url}: expected ${expected}, got ${actual}" >&2
    exit 1
  fi
  chmod 644 "${tmp}"
  mv -f "${tmp}" "${target}"
  echo "ok ${target} (${actual})"
}

fetch "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.js" "${DIR}/chart.umd.js" "${CHARTJS_SRI}"
fetch "https://cdn.jsdelivr.net/npm/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" "${DIR}/htmx.min.js" "${HTMX_SRI}"
