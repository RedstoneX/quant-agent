#!/usr/bin/env bash
# FRED macro series prefetch runner — systemd entry point. Board item 119.
#
# Same shape as run_pricing_refresh.sh and for the same reasons: the script
# needs `.env` sourced, and it needs an outer timeout so a hung HTTPS call
# cannot wedge the unit.
#
# NOT routed through run_if_et_window.sh. That wrapper is for the six
# ET-windowed trading sessions and brings three things this job must not have:
# the cross-session lock (a prefetch that loses the lock to
# earnings_preprocess would silently exit 0 and leave the open with a cold
# cache), the once-per-ET-date marker, and a 1200-second ceiling. This job
# holds no trading state and must neither block a session nor be blocked by
# one.
#
# The outer timeout below is the provider's own computed ceiling
# (MacroDataProvider.prefetch_deadline_s) plus a margin for interpreter
# startup and config load. At the shipped `macro:` settings in
# config/settings.yaml (request_timeout_s 15, max_retries 2, backoff 2/8/1)
# that ceiling is 15 series x 15s x 4 attempts-worth + 15 x 8s of backoff =
# 1020s, so the Python process is given 1020s and systemd is given more (see
# the unit's TimeoutStartSec). If those settings change, this number is
# recomputed from them — it is not independently chosen.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

# The cache path is relative, so it must resolve against the same working
# directory the trading sessions resolve it against. Getting this wrong would
# write a perfectly good cache somewhere no session ever reads.
cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

exec "$TIMEOUT" --kill-after=30 1020 "$PYTHON" scripts/refresh_macro_series.py "$@"
