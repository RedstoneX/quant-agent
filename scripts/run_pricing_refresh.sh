#!/usr/bin/env bash
# LLM price-cache refresh runner — systemd entry point.
#
# Same shape as run_daily_export.sh and for the same two reasons: the script
# needs `.env` sourced (the failure alert goes out over Telegram, and a
# creds-less notifier would turn a loud failure into a silent one — which is
# the exact defect this timer exists to close, reproduced one level up), and
# it needs an outer timeout so a hung HTTPS call cannot wedge the unit.
#
# NOT routed through run_if_et_window.sh: that wrapper is for the six
# ET-windowed trading sessions and rejects unknown modes by design. This is a
# two-request fetch that must run on days the market is CLOSED — the weekend
# is precisely when the cache went stale unnoticed on 2026-08-30 — so every
# piece of session-window machinery is the wrong tool.
#
# Arguments are passed through to the Python script (the unit passes --force).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
# Linux: /usr/bin/timeout. macOS fallback: brew coreutils via TIMEOUT_OVERRIDE
# (same convention as run_if_et_window.sh and run_daily_export.sh).
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

# The cache paths are relative, so they resolve against the working directory
# exactly as the trading process resolves them. Getting this wrong would write
# a pristine cache somewhere no session ever reads.
cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 120s is generous for two HTTPS GETs each carrying a 10s client timeout
# (_FETCH_TIMEOUT_S in src/cost_table.py) plus one Telegram push.
exec "$TIMEOUT" --kill-after=15 120 "$PYTHON" scripts/refresh_pricing.py "$@"
