#!/usr/bin/env bash
# Download BTC/EUR candles (1d, 4h, 1h) from Bitvavo with Freqtrade's download-data.
# No API key needed (public endpoint). Parameters via environment, all optional:
#   FT_BIN     Freqtrade binary            (default: /opt/freqtrade/.venv/bin/freqtrade)
#   USERDIR    Freqtrade user directory    (default: <repo>/user_data)
#   DATADIR    target data directory       (default: $USERDIR/data/bitvavo)
#   TIMERANGE  Freqtrade timerange         (default: 20190101-)
#   PAIRS      space separated pairs       (default: BTC/EUR)
#   TIMEFRAMES space separated timeframes  (default: 1d 4h 1h)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FT_BIN="${FT_BIN:-/opt/freqtrade/.venv/bin/freqtrade}"
USERDIR="${USERDIR:-${REPO_DIR}/user_data}"
DATADIR="${DATADIR:-${USERDIR}/data/bitvavo}"
TIMERANGE="${TIMERANGE:-20190101-}"
PAIRS="${PAIRS:-BTC/EUR}"
TIMEFRAMES="${TIMEFRAMES:-1d 4h 1h}"

if [[ ! -x "${FT_BIN}" ]]; then
  echo "freqtrade binary not found: ${FT_BIN} (set FT_BIN)" >&2
  exit 1
fi

mkdir -p "${DATADIR}"

# shellcheck disable=SC2086
"${FT_BIN}" download-data \
  --userdir "${USERDIR}" \
  --config "${USERDIR}/config.json" \
  --config "${USERDIR}/config-private.example.json" \
  --exchange bitvavo \
  --datadir "${DATADIR}" \
  --data-format-ohlcv feather \
  --pairs ${PAIRS} \
  --timeframes ${TIMEFRAMES} \
  --timerange "${TIMERANGE}"

echo "Data written to ${DATADIR}:"
ls -la "${DATADIR}"
