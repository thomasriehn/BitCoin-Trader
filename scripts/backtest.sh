#!/usr/bin/env bash
# Backtest BtcTrend on Bitvavo daily candles, then run Freqtrade's lookahead-analysis
# and recursive-analysis. Parameters via environment, all optional:
#   FT_BIN      Freqtrade binary          (default: /opt/freqtrade/.venv/bin/freqtrade)
#   USERDIR     Freqtrade user directory  (default: <repo>/user_data)
#   CONFIG_DIR  directory with config.json and config-private.example.json
#               (default: $USERDIR if it holds a config.json, otherwise <repo>/user_data)
#   DATADIR     data directory            (default: $USERDIR/data/bitvavo)
#   TIMERANGE   Freqtrade timerange       (default: 20190901- , open end = all downloaded data)
#   FEE         fee per side              (default: 0.0015 = Bitvavo maker; taker is 0.0025)
#   STRATEGY    strategy class            (default: BtcTrend)
#   TIMEFRAME   candle timeframe          (default: 1d)
#   PROTECTIONS 1 to pass --enable-protections (default: 1)
#   SKIP_ANALYSIS 1 to skip lookahead/recursive analysis (default: 0)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FT_BIN="${FT_BIN:-/opt/freqtrade/.venv/bin/freqtrade}"
USERDIR="${USERDIR:-${REPO_DIR}/user_data}"
DATADIR="${DATADIR:-${USERDIR}/data/bitvavo}"
TIMERANGE="${TIMERANGE:-20190901-}"
FEE="${FEE:-0.0015}"
STRATEGY="${STRATEGY:-BtcTrend}"
TIMEFRAME="${TIMEFRAME:-1d}"
PROTECTIONS="${PROTECTIONS:-1}"
SKIP_ANALYSIS="${SKIP_ANALYSIS:-0}"
STRATEGY_PATH="${REPO_DIR}/user_data/strategies"
RESULTS_DIR="${USERDIR}/backtest_results"

if [[ ! -x "${FT_BIN}" ]]; then
  echo "freqtrade binary not found: ${FT_BIN} (set FT_BIN)" >&2
  exit 1
fi
if [[ ! -d "${DATADIR}" ]]; then
  echo "data directory not found: ${DATADIR} (run scripts/download-data.sh first)" >&2
  exit 1
fi
mkdir -p "${RESULTS_DIR}"

# A data-only userdir (for example the one download-data.sh fills) has no config.json;
# fall back to the repo's public config and the placeholder private config.
CONFIG_DIR="${CONFIG_DIR:-${USERDIR}}"
if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  CONFIG_DIR="${REPO_DIR}/user_data"
fi
if [[ ! -f "${CONFIG_DIR}/config.json" || ! -f "${CONFIG_DIR}/config-private.example.json" ]]; then
  echo "config.json / config-private.example.json not found in ${CONFIG_DIR} (set CONFIG_DIR)" >&2
  exit 1
fi

COMMON_ARGS=(
  --userdir "${USERDIR}"
  --config "${CONFIG_DIR}/config.json"
  --config "${CONFIG_DIR}/config-private.example.json"
  --strategy-path "${STRATEGY_PATH}"
  --strategy "${STRATEGY}"
  --datadir "${DATADIR}"
  --timeframe "${TIMEFRAME}"
  --timerange "${TIMERANGE}"
)
PROT_ARGS=()
if [[ "${PROTECTIONS}" == "1" ]]; then
  PROT_ARGS=(--enable-protections)
fi

echo "== backtesting ${STRATEGY} timerange=${TIMERANGE} fee=${FEE} datadir=${DATADIR} config=${CONFIG_DIR}"
"${FT_BIN}" backtesting "${COMMON_ARGS[@]}" "${PROT_ARGS[@]}" \
  --fee "${FEE}" \
  --breakdown year \
  --cache none \
  --export trades \
  --backtest-directory "${RESULTS_DIR}" \
  --notes "${STRATEGY} ${TIMERANGE} fee=${FEE}"

if [[ "${SKIP_ANALYSIS}" == "1" ]]; then
  exit 0
fi

echo "== lookahead-analysis"
# lookahead-analysis forces market orders, which Freqtrade only accepts with price_side "other".
# The override applies to this run only (Freqtrade reads FREQTRADE__<SECTION>__<KEY> env vars).
# Freqtrade 2026.8 crashes (pandas LossySetitemError) when it updates an existing row in the
# CSV export, so a previous export for this strategy is removed first.
rm -f "${RESULTS_DIR}/lookahead-${STRATEGY}.csv"
FREQTRADE__ENTRY_PRICING__PRICE_SIDE=other FREQTRADE__EXIT_PRICING__PRICE_SIDE=other \
"${FT_BIN}" lookahead-analysis "${COMMON_ARGS[@]}" \
  --fee "${FEE}" \
  --minimum-trade-amount 10 \
  --targeted-trade-amount 20 \
  --lookahead-analysis-exportfilename "${RESULTS_DIR}/lookahead-${STRATEGY}.csv"

echo "== recursive-analysis"
# recursive-analysis compares indicator values, it takes no --fee argument.
"${FT_BIN}" recursive-analysis "${COMMON_ARGS[@]}" \
  --startup-candle 210 300 400
