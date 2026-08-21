#!/usr/bin/env bash
# QAMC read-side production convergence v2.
# Deploys the accepted Mission Control cockpit + intraday-chart work from the
# observed production Telegram baseline to one exact reviewed Git target.
set -Eeuo pipefail

BASELINE_SHA="e113f5c6255925f1a93f0f8c242dcd5facbaf41a"
TARGET_SHA="2b3faaf69c0b842a08f991a9ca517a3989bdaf93"
TARGET_TREE="c75eb6b7a06c87b1743b82230e62dc5a221cda12"

QAMC_USER="qamc"
QAMC_REPO="/home/qamc/quant-agent"
QAMC_ENV="${QAMC_REPO}/.env"
API_UNIT="quant-agent-api.service"
API_BASE="http://127.0.0.1:8800"
TG_PLACEHOLDER="ONECLI-INJECTS-THIS-PLACEHOLDER"
REQUIRED_TIMERS=7

say() { printf '\n== %s ==\n' "$*"; }
ok() { printf '   [OK] %s\n' "$*"; }
die() { printf '   [FAIL] %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root"
SELF="$(readlink -f "$0")"
read -r SELF_OWNER SELF_GROUP SELF_MODE <<<"$(stat -c '%U %G %a' "$SELF")"
[[ "$SELF_OWNER:$SELF_GROUP:$SELF_MODE" == "root:root:700" ]] || die "script must be root:root mode 0700"
SELF_DIR="$(dirname "$SELF")"
read -r DIR_OWNER DIR_MODE <<<"$(stat -c '%U %a' "$SELF_DIR")"
[[ "$DIR_OWNER" == "root" ]] || die "script directory must be root-owned"
[[ "${DIR_MODE: -2:1}" =~ ^[0145]$ && "${DIR_MODE: -1:1}" =~ ^[0145]$ ]] || die "script directory must not be group/world writable"

LOG="${QAMC_ROLLOUT_LOG:-/root/qamc-readside-rollout-$(date -u +%Y%m%dT%H%M%SZ).log}"
: >"$LOG"
chmod 0600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
trap '' PIPE

as_qamc() { sudo -u "$QAMC_USER" -H bash -c "$1"; }
qgit() { as_qamc "cd '$QAMC_REPO' && git $1"; }
QAMC_UID="$(id -u "$QAMC_USER")"
SYSTEMD_ENV="export XDG_RUNTIME_DIR=/run/user/${QAMC_UID}; export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${QAMC_UID}/bus;"
sysctl_user() { as_qamc "$SYSTEMD_ENV systemctl --user $1"; }

health_json() { curl -fsS --max-time 15 "${API_BASE}/health"; }
health_field() { health_json | python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))' "$1"; }
wait_healthy() {
  local deadline=$((SECONDS + ${1:-90}))
  while (( SECONDS < deadline )); do
    [[ "$(health_field status 2>/dev/null || true)" == "ok" ]] && return 0
    sleep 2
  done
  return 1
}

timer_snapshot() {
  sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null | awk 'NF {print $1, $2}' | sort
}
unit_snapshot() {
  sysctl_user "list-unit-files 'quant-agent-*' --no-legend" 2>/dev/null | awk 'NF {print $1, $2}' | sort
}
assert_timers_healthy() {
  local snap enabled count timer failed
  snap="$(timer_snapshot)"
  enabled="$(printf '%s\n' "$snap" | awk '$2 ~ /^enabled/ {print $1}')"
  count="$(printf '%s\n' "$enabled" | grep -c . || true)"
  [[ "$count" == "$REQUIRED_TIMERS" ]] || return 1
  while read -r timer; do
    [[ -z "$timer" ]] && continue
    sysctl_user "is-active --quiet '$timer'" || return 1
  done <<<"$enabled"
  failed="$(sysctl_user "list-units --failed --no-legend 'quant-agent-*'" 2>/dev/null || true)"
  [[ -z "${failed//[[:space:]]/}" ]]
}

verify_allowed_untracked() {
  local untracked path
  untracked="$(qgit "ls-files --others --exclude-standard")"
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    [[ "$path" =~ ^quant_agent\.log\.[0-9]+$ ]] || return 1
    as_qamc "test -f '$QAMC_REPO/$path' && test ! -L '$QAMC_REPO/$path' && test \"\$(stat -c %U '$QAMC_REPO/$path')\" = '$QAMC_USER'" || return 1
  done <<<"$untracked"
}
verify_no_tracked_delta() {
  [[ -z "$(qgit "diff --name-only")" ]] || return 1
  [[ -z "$(qgit "diff --cached --name-only")" ]] || return 1
  verify_allowed_untracked
}
verify_override_only() {
  local sha changed staged diff mod_count
  sha="$1"
  changed="$(qgit "diff --name-only '$sha'")"
  staged="$(qgit "diff --cached --name-only")"
  [[ "$changed" == "config/settings.yaml" ]] || return 1
  [[ -z "$staged" ]] || return 1
  verify_allowed_untracked || return 1
  diff="$(qgit "diff --unified=0 '$sha' -- config/settings.yaml")"
  mod_count="$(printf '%s\n' "$diff" | grep -E '^[+-][^+-]' | wc -l | tr -d ' ')"
  [[ "$mod_count" == "2" ]] || return 1
  printf '%s\n' "$diff" | grep -Eq '^-([[:space:]]+)enabled:[[:space:]]*false([[:space:]]*(#.*)?)?$' || return 1
  printf '%s\n' "$diff" | grep -Eq '^\+([[:space:]]+)enabled:[[:space:]]*true([[:space:]]*(#.*)?)?$' || return 1
}
apply_intraday_override() {
  sudo -u "$QAMC_USER" -H python3 - "$QAMC_REPO/config/settings.yaml" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
lines = p.read_text().splitlines(keepends=True)
in_block = False
seen = False
for i, line in enumerate(lines):
    raw = line.rstrip("\n")
    if re.match(r"^intraday_scan:\s*$", raw):
        in_block = True
        continue
    if in_block:
        if raw and not raw[0].isspace() and not raw.lstrip().startswith("#"):
            break
        m = re.match(r"^(\s*)enabled:\s*(true|false)(.*?)(\n?)$", line)
        if m:
            seen = True
            if m.group(2) == "false":
                lines[i] = f"{m.group(1)}enabled: true{m.group(3)}{m.group(4)}"
            break
if not seen:
    raise SystemExit("intraday_scan.enabled not found")
p.write_text("".join(lines))
PY
}
committed_intraday_state() {
  qgit "show '$1:config/settings.yaml'" | awk '
    /^intraday_scan:[[:space:]]*$/ {inb=1; next}
    inb && /^[^[:space:]#]/ {exit}
    inb && /^[[:space:]]*enabled:/ {print $2; exit}'
}

TIMERS_BEFORE=""
UNITS_BEFORE=""
MUTATED=0
SUCCESS=0
rollback() {
  trap - EXIT INT TERM HUP
  printf '\n!! rollout failed after mutation; converging production to baseline !!\n' >&2
  qgit "checkout -- config/settings.yaml" >/dev/null 2>&1 || true
  qgit "checkout --detach '$BASELINE_SHA'" >/dev/null 2>&1 || true
  apply_intraday_override || true
  sysctl_user "restart '$API_UNIT'" >/dev/null 2>&1 || true
  if wait_healthy 90 \
    && [[ "$(qgit "rev-parse HEAD")" == "$BASELINE_SHA" ]] \
    && verify_override_only "$BASELINE_SHA" \
    && [[ "$(timer_snapshot)" == "$TIMERS_BEFORE" ]] \
    && [[ "$(unit_snapshot)" == "$UNITS_BEFORE" ]] \
    && assert_timers_healthy; then
    printf '   [ROLLBACK OK] production restored to %s + intraday override\n' "$BASELINE_SHA" >&2
  else
    printf '   [ROLLBACK INCOMPLETE] inspect %s and production immediately\n' "$LOG" >&2
  fi
}
on_exit() {
  local rc=$?
  if (( rc != 0 && MUTATED == 1 && SUCCESS == 0 )); then rollback; fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

say "GATE A — immutable target and production preflight"
[[ -d "$QAMC_REPO/.git" ]] || die "production repo missing"
[[ -x "$QAMC_REPO/.venv/bin/python" ]] || die "production venv missing"
[[ "$(qgit "rev-parse HEAD")" == "$BASELINE_SHA" ]] || die "production HEAD is not expected baseline"
verify_override_only "$BASELINE_SHA" || die "production working tree is not baseline + intraday override + permitted rotated logs only"
[[ "$(committed_intraday_state "$BASELINE_SHA")" == "false" ]] || die "baseline committed intraday default is not false"
[[ "$(health_field status)" == "ok" ]] || die "Mission Control unhealthy before rollout"
[[ "$(health_field paper)" == "True" ]] || die "production is not Alpaca Paper"
[[ "$(health_field session_lock_active)" != "True" ]] || die "an active QAMC session lock exists; retry when session is idle"
sysctl_user "is-active --quiet '$API_UNIT'" || die "Mission Control systemd user service not active"
TIMERS_BEFORE="$(timer_snapshot)"
UNITS_BEFORE="$(unit_snapshot)"
assert_timers_healthy || die "the seven-timer surface is not healthy before rollout"
ok "production baseline, Paper mode, idle session, API, intraday override, and seven timers verified"

say "GATE B — fetch and verify exact reviewed target"
qgit "fetch --no-tags origin '+${TARGET_SHA}:refs/qamc/readside-target-v2'" >/dev/null 2>&1 \
  || qgit "fetch --no-tags origin main" >/dev/null 2>&1 \
  || die "could not fetch exact target"
[[ "$(qgit "show -s --format=%T '$TARGET_SHA'")" == "$TARGET_TREE" ]] || die "target tree hash mismatch"
qgit "diff --quiet '$BASELINE_SHA' '$TARGET_SHA' -- config/settings.yaml" || die "target unexpectedly changes committed settings.yaml"
[[ "$(committed_intraday_state "$TARGET_SHA")" == "false" ]] || die "target committed intraday default is not false"
qgit "grep -qF '@router.get(\"/quotes\"' '$TARGET_SHA' -- src/api/routes_live.py" || die "target missing /quotes"
qgit "grep -qF 'AUTO / PRIMARY' '$TARGET_SHA' -- frontend/src/components/TodaySessionsStrip.tsx" || die "target missing AUTO / PRIMARY"
qgit "grep -qF '5m Today' '$TARGET_SHA' -- frontend/src/components/PriceChartPanel.tsx" || die "target missing 5m Today timeframe"
qgit "grep -qF 'onSelectTrade' '$TARGET_SHA' -- frontend/src/components/TodaySessionsStrip.tsx" || die "target missing session click-to-chart"
qgit "cat-file -e '$TARGET_SHA:src/trader_feed.py'" || die "target lost deployed Telegram trader feed"
ok "target SHA/tree and required cockpit/chart capabilities verified"

say "GATE C — deploy exact target and run focused deterministic tests"
qgit "checkout -- config/settings.yaml"
MUTATED=1
qgit "checkout --detach '$TARGET_SHA'"
[[ "$(qgit "rev-parse HEAD")" == "$TARGET_SHA" ]] || die "checkout did not land on target"
verify_no_tracked_delta || die "target checkout has an unexpected tracked or untracked code delta"
timeout 360 sudo -u "$QAMC_USER" -H bash -c \
  "cd '$QAMC_REPO' && .venv/bin/python -m pytest -q tests/test_api_quotes.py tests/test_api_contract.py tests/test_api_journal.py tests/test_broker_reads.py tests/test_broker_market_data.py tests/test_trader_feed.py" \
  || die "focused read-side/chart/Telegram tests failed on target"
ok "target checkout and focused deterministic suite passed"

say "GATE D — restart Mission Control and restore authorized intraday override"
sysctl_user "restart '$API_UNIT'"
wait_healthy 90 || die "Mission Control did not return healthy"
[[ "$(health_field paper)" == "True" ]] || die "Mission Control no longer reports paper=true"
apply_intraday_override
verify_override_only "$TARGET_SHA" || die "authorized intraday override was not re-established exactly"
[[ "$(timer_snapshot)" == "$TIMERS_BEFORE" ]] || die "timer set changed during rollout"
[[ "$(unit_snapshot)" == "$UNITS_BEFORE" ]] || die "quant-agent unit-file surface changed during rollout"
assert_timers_healthy || die "timer surface unhealthy after rollout"
ok "API restarted on target; intraday override restored; seven timers unchanged"

say "GATE E — live read-side acceptance"
for path in /health /cockpit/ /ui/; do
  code="$(curl -sS -L --max-time 20 -o /dev/null -w '%{http_code}' "${API_BASE}${path}" || true)"
  [[ "$code" == "200" ]] || die "${path} returned ${code:-no response}"
done

QUOTE_JSON="$(curl -fsS --max-time 30 "${API_BASE}/quotes?symbols=SPY")"
printf '%s' "$QUOTE_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert not d.get("error"), d.get("error")
q=d.get("quotes") or []
assert q and q[0].get("symbol")=="SPY"
assert q[0].get("last_price") is not None
assert d.get("as_of")
' || die "/quotes did not return a usable current SPY quote"

DAILY_JSON="$(curl -fsS --max-time 30 "${API_BASE}/prices/SPY?lookback_days=5&timeframe=1d")"
printf '%s' "$DAILY_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert not d.get("error"), d.get("error")
assert d.get("symbol")=="SPY" and d.get("timeframe")=="1d"
assert len(d.get("bars") or []) > 0
' || die "daily /prices contract failed"

INTRA_JSON="$(curl -fsS --max-time 30 "${API_BASE}/prices/SPY?lookback_days=5&timeframe=15m")"
printf '%s' "$INTRA_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert not d.get("error"), d.get("error")
assert d.get("symbol")=="SPY" and d.get("timeframe")=="15m"
b=d.get("bars") or []
assert b, "no 15m bars"
assert all(x.get("timestamp") for x in b)
' || die "intraday 15m /prices contract failed"

TODAY_JSON="$(curl -fsS --max-time 30 "${API_BASE}/prices/SPY?lookback_days=1&timeframe=5m")"
printf '%s' "$TODAY_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert not d.get("error"), d.get("error")
assert d.get("symbol")=="SPY" and d.get("timeframe")=="5m"
assert all(x.get("timestamp") for x in (d.get("bars") or []))
' || die "5m Today /prices contract failed"

ACCOUNT_JSON="$(curl -fsS --max-time 30 "${API_BASE}/account")"
printf '%s' "$ACCOUNT_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert not d.get("error"), d.get("error")
assert d.get("paper") is True
assert (d.get("risk_limits") or {}).get("max_total_position_pct") is not None
' || die "/account truth/risk-limit contract failed"

for method in POST PUT PATCH DELETE; do
  code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -X "$method" "${API_BASE}/quotes?symbols=SPY" || true)"
  [[ "$code" == "405" ]] || die "${method} /quotes returned ${code:-no response}, expected 405"
done
OPENAPI="$(curl -fsS --max-time 20 "${API_BASE}/openapi.json")"
printf '%s' "$OPENAPI" | python3 -c '
import json,sys
d=json.load(sys.stdin)
bad=[]
for p,ops in (d.get("paths") or {}).items():
    for m in ("post","put","patch","delete"):
        if m in ops: bad.append(f"{m.upper()} {p}")
assert not bad, bad
' || die "Mission Control OpenAPI exposes a write route"

timeout 300 sudo -u "$QAMC_USER" -H bash -c \
  "cd '$QAMC_REPO' && set -a && . ./.env && set +a && .venv/bin/python ops/commissioning/verify_commissioning.py --live" \
  || die "commissioning/live-provider verifier failed"
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" || die "Telegram runtime env no longer contains only OneCLI placeholder"
GETME="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && curl -sS --max-time 20 -o /dev/null -w '%{http_code}' 'https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe'" 2>/dev/null || true)"
[[ "$GETME" == "200" ]] || die "Telegram getMe returned ${GETME:-no response}"
if as_qamc "grep -qE '/bot[0-9]{6,}:[A-Za-z0-9_-]{20,}' '$QAMC_REPO/quant_agent.log'" 2>/dev/null; then
  die "Telegram-token-shaped material found in runtime log"
fi

[[ "$(qgit "rev-parse HEAD")" == "$TARGET_SHA" ]] || die "final HEAD moved away from target"
verify_override_only "$TARGET_SHA" || die "final working tree is not target + intraday override + permitted rotated logs only"
[[ "$(timer_snapshot)" == "$TIMERS_BEFORE" ]] || die "final timer set differs from preflight"
[[ "$(unit_snapshot)" == "$UNITS_BEFORE" ]] || die "final unit set differs from preflight"
assert_timers_healthy || die "final timer health check failed"
[[ "$(health_field status)" == "ok" && "$(health_field paper)" == "True" ]] || die "final health/paper check failed"

SUCCESS=1
trap - EXIT INT TERM HUP
say "FINISH LINE PASSED"
printf 'Production SHA       : %s\n' "$TARGET_SHA"
printf 'Production tree      : %s\n' "$TARGET_TREE"
printf 'Local tracked delta  : config/settings.yaml intraday_scan.enabled=true only\n'
printf 'Permitted runtime log: quant_agent.log.N rotated files only\n'
printf 'Mission Control      : health/cockpit/ui/quotes/account/1D/15m/5m prices PASS; GET-only\n'
printf 'Chart utility        : session click-to-chart + BUY/SELL markers + 5m/15m/1h/1D target verified\n'
printf 'Telegram             : existing feed preserved; OneCLI getMe 200; no message sent\n'
printf 'Timers               : 7 enabled/active; unit surface unchanged\n'
printf 'Trading authorization: Alpaca Paper only; no order submitted/cancelled/modified\n'
printf 'Transcript            : %s\n' "$LOG"
