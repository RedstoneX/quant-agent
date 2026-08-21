#!/usr/bin/env bash
# QAMC governed production convergence: read-side cockpit + intraday charts.
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

say(){ printf '\n== %s ==\n' "$*"; }
ok(){ printf '   [OK] %s\n' "$*"; }
die(){ printf '   [FAIL] %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" -eq 0 ]] || die "run as root"

LOG="/root/qamc-readside-rollout-v2-$(date -u +%Y%m%dT%H%M%SZ).log"
: >"$LOG"; chmod 0600 "$LOG"; exec > >(tee -a "$LOG") 2>&1
as_qamc(){ sudo -u "$QAMC_USER" -H bash -c "$1"; }
qgit(){ as_qamc "cd '$QAMC_REPO' && git $1"; }
QAMC_UID="$(id -u "$QAMC_USER")"
SYSTEMD_ENV="export XDG_RUNTIME_DIR=/run/user/${QAMC_UID}; export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${QAMC_UID}/bus;"
sysctl_user(){ as_qamc "$SYSTEMD_ENV systemctl --user $1"; }
health_json(){ curl -fsS --max-time 15 "${API_BASE}/health"; }
health_field(){ health_json | python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))' "$1"; }
wait_healthy(){ local end=$((SECONDS+${1:-90})); while ((SECONDS<end)); do [[ "$(health_field status 2>/dev/null || true)" == "ok" ]] && return 0; sleep 2; done; return 1; }

timer_snapshot(){ sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null | awk 'NF{print $1,$2}' | sort; }
unit_snapshot(){ sysctl_user "list-unit-files 'quant-agent-*' --no-legend" 2>/dev/null | awk 'NF{print $1,$2}' | sort; }
assert_timers(){
  local enabled count t
  enabled="$(timer_snapshot | awk '$2 ~ /^enabled/{print $1}')"
  count="$(printf '%s\n' "$enabled" | grep -c . || true)"
  [[ "$count" == "$REQUIRED_TIMERS" ]] || return 1
  while read -r t; do [[ -z "$t" ]] || sysctl_user "is-active --quiet '$t'" || return 1; done <<<"$enabled"
  [[ -z "$(sysctl_user "list-units --failed --no-legend 'quant-agent-*'" 2>/dev/null | tr -d '[:space:]')" ]]
}

verify_allowed_untracked(){
  local p
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    [[ "$p" =~ ^quant_agent\.log\.[0-9]+$ ]] || return 1
  done <<<"$(qgit "ls-files --others --exclude-standard")"
}
verify_override_only(){
  local sha="$1" changed diff n
  changed="$(qgit "diff --name-only '$sha'")"
  [[ "$changed" == "config/settings.yaml" ]] || return 1
  [[ -z "$(qgit "diff --cached --name-only")" ]] || return 1
  verify_allowed_untracked || return 1
  diff="$(qgit "diff --unified=0 '$sha' -- config/settings.yaml")"
  n="$(printf '%s\n' "$diff" | grep -E '^[+-][^+-]' | wc -l | tr -d ' ')"
  [[ "$n" == "2" ]] || return 1
  printf '%s\n' "$diff" | grep -Eq '^-([[:space:]]+)enabled:[[:space:]]*false' || return 1
  printf '%s\n' "$diff" | grep -Eq '^\+([[:space:]]+)enabled:[[:space:]]*true' || return 1
}
apply_intraday_override(){
  sudo -u "$QAMC_USER" -H python3 - "$QAMC_REPO/config/settings.yaml" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); lines=p.read_text().splitlines(True); inside=False
for i,line in enumerate(lines):
    raw=line.rstrip("\n")
    if re.match(r"^intraday_scan:\s*$",raw): inside=True; continue
    if inside and raw and not raw[0].isspace() and not raw.lstrip().startswith("#"): break
    if inside:
        m=re.match(r"^(\s*)enabled:\s*(true|false)(.*)$",line)
        if m:
            lines[i]=f"{m.group(1)}enabled: true{m.group(3)}"; p.write_text("".join(lines)); break
else: raise SystemExit("intraday_scan.enabled not found")
PY
}
committed_intraday_state(){ qgit "show '$1:config/settings.yaml'" | awk '/^intraday_scan:/{f=1;next} f&&/^[^[:space:]#]/{exit} f&&/^[[:space:]]*enabled:/{print $2;exit}'; }

TIMERS_BEFORE=""; UNITS_BEFORE=""; MUTATED=0; SUCCESS=0
rollback(){
  trap - EXIT INT TERM HUP
  printf '\n!! FAILURE AFTER MUTATION — rolling back to %s !!\n' "$BASELINE_SHA" >&2
  qgit "checkout -- config/settings.yaml" >/dev/null 2>&1 || true
  qgit "checkout --detach '$BASELINE_SHA'" >/dev/null 2>&1 || true
  apply_intraday_override || true
  sysctl_user "restart '$API_UNIT'" >/dev/null 2>&1 || true
  if wait_healthy 90 && [[ "$(qgit "rev-parse HEAD")" == "$BASELINE_SHA" ]] && verify_override_only "$BASELINE_SHA" && [[ "$(timer_snapshot)" == "$TIMERS_BEFORE" ]] && assert_timers; then
    printf '   [ROLLBACK OK] production restored\n' >&2
  else
    printf '   [ROLLBACK INCOMPLETE] inspect %s immediately\n' "$LOG" >&2
  fi
}
on_exit(){ local rc=$?; if ((rc!=0 && MUTATED==1 && SUCCESS==0)); then rollback; fi; exit "$rc"; }
trap on_exit EXIT; trap 'exit 130' INT; trap 'exit 143' TERM HUP

say "GATE A — production preflight"
[[ -d "$QAMC_REPO/.git" ]] || die "production repo missing"
[[ -x "$QAMC_REPO/.venv/bin/python" ]] || die "production venv missing"
[[ "$(qgit "rev-parse HEAD")" == "$BASELINE_SHA" ]] || die "production HEAD is not expected baseline $BASELINE_SHA"
verify_override_only "$BASELINE_SHA" || die "working tree is not baseline + intraday override only"
[[ "$(committed_intraday_state "$BASELINE_SHA")" == "false" ]] || die "baseline committed intraday default is not false"
[[ "$(health_field status)" == "ok" ]] || die "API unhealthy before rollout"
[[ "$(health_field paper)" == "True" ]] || die "production is not Alpaca Paper"
[[ "$(health_field session_lock_active)" != "True" ]] || die "active QAMC session lock; retry when idle"
sysctl_user "is-active --quiet '$API_UNIT'" || die "API service inactive"
TIMERS_BEFORE="$(timer_snapshot)"; UNITS_BEFORE="$(unit_snapshot)"
assert_timers || die "seven-timer surface unhealthy"
ok "baseline, paper mode, idle state, override and seven timers verified"

say "GATE B — immutable target"
qgit "fetch --no-tags origin main" >/dev/null 2>&1 || die "git fetch origin main failed"
qgit "cat-file -e '$TARGET_SHA^{commit}'" || die "target commit unavailable"
[[ "$(qgit "show -s --format=%T '$TARGET_SHA'")" == "$TARGET_TREE" ]] || die "target tree hash mismatch"
qgit "merge-base --is-ancestor '$BASELINE_SHA' '$TARGET_SHA'" || die "target does not descend from production baseline"
qgit "diff --quiet '$BASELINE_SHA' '$TARGET_SHA' -- config/settings.yaml" || die "target changes committed settings.yaml"
[[ "$(committed_intraday_state "$TARGET_SHA")" == "false" ]] || die "target committed intraday default is not false"
qgit "grep -qF '5m Today' '$TARGET_SHA' -- frontend/src/components/PriceChartPanel.tsx" || die "target missing chart timeframe UI"
qgit "grep -qF 'get_intraday_chart_bars' '$TARGET_SHA' -- src/execution/broker.py" || die "target missing intraday chart market-data read"
qgit "cat-file -e '$TARGET_SHA:src/trader_feed.py'" || die "target lost Telegram trader feed"
ok "exact target SHA/tree and key invariants verified"

say "GATE C — checkout + focused deterministic tests"
qgit "checkout -- config/settings.yaml"; MUTATED=1
qgit "checkout --detach '$TARGET_SHA'"
[[ "$(qgit "rev-parse HEAD")" == "$TARGET_SHA" ]] || die "checkout missed target"
[[ -z "$(qgit "diff --name-only")" && -z "$(qgit "diff --cached --name-only")" ]] || die "unexpected tracked delta after checkout"
verify_allowed_untracked || die "unexpected untracked files"
timeout 360 sudo -u "$QAMC_USER" -H bash -c "cd '$QAMC_REPO' && .venv/bin/python -m pytest -q tests/test_api_contract.py tests/test_api_quotes.py tests/test_api_journal.py tests/test_broker_market_data.py tests/test_broker_reads.py tests/test_trader_feed.py" || die "focused test suite failed"
ok "focused backend/read-side regression suite passed"

say "GATE D — restart + restore authorized local override"
sysctl_user "restart '$API_UNIT'"; wait_healthy 90 || die "API did not recover"
[[ "$(health_field paper)" == "True" ]] || die "paper mode lost"
apply_intraday_override; verify_override_only "$TARGET_SHA" || die "intraday override not restored exactly"
[[ "$(timer_snapshot)" == "$TIMERS_BEFORE" ]] || die "timer set changed"
[[ "$(unit_snapshot)" == "$UNITS_BEFORE" ]] || die "unit-file surface changed"
assert_timers || die "timer surface unhealthy after restart"
ok "service healthy; override and timers preserved"

say "GATE E — live read-side acceptance"
for p in /health /cockpit/ /ui/; do code="$(curl -sS -L --max-time 20 -o /dev/null -w '%{http_code}' "${API_BASE}${p}" || true)"; [[ "$code" == "200" ]] || die "$p returned ${code:-none}"; done
curl -fsS --max-time 30 "${API_BASE}/quotes?symbols=SPY" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert not d.get("error"),d.get("error"); q=d.get("quotes") or []; assert q and q[0].get("last_price") is not None; assert d.get("as_of")' || die "live quote acceptance failed"
for spec in '5m:1' '15m:5' '1h:30' '1d:5'; do tf="${spec%%:*}"; days="${spec##*:}"; curl -fsS --max-time 30 "${API_BASE}/prices/SPY?lookback_days=${days}&timeframe=${tf}" | python3 -c 'import json,sys; tf=sys.argv[1]; d=json.load(sys.stdin); assert not d.get("error"),d.get("error"); assert d.get("symbol")=="SPY"; assert d.get("timeframe")==tf; assert len(d.get("bars") or [])>0' "$tf" || die "price timeframe $tf failed"; done
curl -fsS --max-time 30 "${API_BASE}/account" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert not d.get("error"),d.get("error"); assert d.get("paper") is True' || die "account/paper acceptance failed"
for m in POST PUT PATCH DELETE; do code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -X "$m" "${API_BASE}/quotes?symbols=SPY" || true)"; [[ "$code" == "405" ]] || die "$m write-surface check returned $code"; done
OPENAPI="$(curl -fsS --max-time 20 "${API_BASE}/openapi.json")"; printf '%s' "$OPENAPI" | python3 -c 'import json,sys; d=json.load(sys.stdin); bad=[f"{m.upper()} {p}" for p,o in (d.get("paths") or {}).items() for m in ("post","put","patch","delete") if m in o]; assert not bad,bad' || die "OpenAPI exposes write routes"
ok "cockpit/UI, quotes, all four chart timeframes, account and GET-only contract passed"

say "GATE F — provider/Telegram preservation"
timeout 300 sudo -u "$QAMC_USER" -H bash -c "cd '$QAMC_REPO' && set -a && . ./.env && set +a && .venv/bin/python ops/commissioning/verify_commissioning.py --live" || die "live commissioning verifier failed"
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" || die "Telegram env placeholder changed"
GETME="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && curl -sS --max-time 20 -o /dev/null -w '%{http_code}' 'https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe'" 2>/dev/null || true)"
[[ "$GETME" == "200" ]] || die "Telegram getMe returned ${GETME:-none}"
ok "commissioning and Telegram preservation passed"

[[ "$(qgit "rev-parse HEAD")" == "$TARGET_SHA" ]] || die "final HEAD moved"
verify_override_only "$TARGET_SHA" || die "final working tree incorrect"
[[ "$(timer_snapshot)" == "$TIMERS_BEFORE" && "$(unit_snapshot)" == "$UNITS_BEFORE" ]] || die "final systemd surface changed"
assert_timers || die "final timer health failed"
[[ "$(health_field status)" == "ok" && "$(health_field paper)" == "True" ]] || die "final health/paper check failed"
SUCCESS=1; trap - EXIT INT TERM HUP
say "FINISH LINE PASSED"
printf 'Production SHA : %s\nProduction tree: %s\nOverride       : intraday_scan.enabled=true (local only)\nTimers         : 7 enabled/active, unchanged\nChart          : 5m Today / 15m / 1h / 1D live acceptance PASS\nTrading        : Alpaca Paper; Mission Control GET-only\nTranscript     : %s\n' "$TARGET_SHA" "$TARGET_TREE" "$LOG"
