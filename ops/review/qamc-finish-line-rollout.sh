#!/usr/bin/env bash
#
# QAMC finish-line paper rollout — run ONCE, as root, from a root-owned copy.
#
#   sudo install -o root -g root -m 0700 \
#     /home/dev/projects/quant-agent/ops/review/qamc-finish-line-rollout.sh \
#     /root/qamc-finish-line-rollout.sh \
#     && sudo bash /root/qamc-finish-line-rollout.sh 2>&1 | tee /root/qamc-rollout.log
#
# The root-owned copy is not ceremony: /home/dev is writable by the Claude Code
# account, and root must not execute a file that account can rewrite between
# review and run. This script refuses to run unless it is owned by root and is
# not group/world writable.
#
# WHAT IT DOES — converges production from the pinned Telegram hotfix to the
# pinned, externally accepted PR #48 target, then enables the already-authorized
# intraday opportunity scanner. Every stage gate is an in-script fail-closed
# check, so the staged ordering of docs/WORK.md is preserved without needing a
# human round trip between stages.
#
#   PHASE 1  PREFLIGHT — verify everything, change nothing.
#   PHASE 2  FETCH the exact target commit by explicit refspec (origin/main is
#            never updated, no branch is followed) and verify its CONTENT
#            before it is checked out. Production still untouched.
#   PHASE 3  DEPLOY the exact pinned SHA (detached) + import/config smoke test
#            under the production venv. Auto-rolls back on smoke failure.
#   PHASE 4  RESTART the Mission Control API and prove it healthy. Auto-rolls
#            back (code + service) if health does not return.
#   PHASE 5  GATE B — production health, Telegram, timers, secret hygiene, and
#            intraday still DISABLED. Any failure stops here, deployed but with
#            intraday off, and prints the rollback command.
#   PHASE 6  GATE D — enable intraday_scan (one line, in place, idempotent) and
#            verify the value from the ACTUAL loaded runtime configuration.
#   PHASE 7  FINAL REPORT.
#
# WHAT IT DOES NOT DO — no new service/timer/daemon, no package installs, no
# firewall/network/account changes, no OneCLI changes, no secret is read,
# written, printed or moved, no Telegram message is sent (getMe only), no
# trading run is invoked and no order is ever placed.
#
set -euo pipefail

# ── Pinned constants (verified on the dev account before this script shipped) ──
BASELINE_SHA="9c736c158fec84129765c25a9429254d3602ad6b"   # current production / rollback point
TARGET_SHA="bb223eadde30654d72ab11e055185a757d0cddc0"     # accepted GitHub main at handoff
TARGET_TREE="ff27c9458ba6f4677c8db2329af7d8d47b176e77"    # tree of TARGET_SHA
EXPECTED_CHANGED_FILES=21
# sha256 of `git diff --name-only BASELINE TARGET` — pins the exact file set
# that was reviewed, so a moved/rewritten target cannot slip through.
EXPECTED_FILELIST_SHA="bc872bac49c3abea63128f907c80445e6156902bdbb2279693c739016b98af11"

QAMC_USER="qamc"
QAMC_REPO="/home/qamc/quant-agent"
QAMC_ENV="${QAMC_REPO}/.env"
API_UNIT="quant-agent-api.service"
API_HEALTH="http://127.0.0.1:8800/health"
ONECLI_API="http://127.0.0.1:10254/api"
TG_PLACEHOLDER="ONECLI-INJECTS-THIS-PLACEHOLDER"
EXPECTED_TIMERS=7

# ── Output helpers (never used to print secret material) ─────────────────────
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   [ OK ] %s\n' "$*"; }
info() { printf '   [ .. ] %s\n' "$*"; }
warn() { printf '   \033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m[FAIL]\033[0m %s\n\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run this as root: sudo bash $0"

SELF="$(readlink -f "$0")"
SELF_META="$(stat -c '%U %a' "$SELF")"
read -r SELF_OWNER SELF_MODE <<< "$SELF_META"
[[ "$SELF_OWNER" == "root" ]] || die "this script is owned by '$SELF_OWNER', not root.
       Copy it to a root-owned path first:
         sudo install -o root -g root -m 0700 $SELF /root/qamc-finish-line-rollout.sh"
# Read the group/other digits off the END of the mode, so a 4-digit mode
# (setuid/sticky bits present) cannot shift the index and silently pass.
SELF_GRP="${SELF_MODE: -2:1}"; SELF_OTH="${SELF_MODE: -1:1}"
[[ "$SELF_GRP" =~ ^[0-5]$ && "$SELF_OTH" =~ ^[0-5]$ ]] \
  || die "this script is group/world writable (mode $SELF_MODE). Re-install with -m 0700."

as_qamc() { sudo -u "$QAMC_USER" -H bash -c "$1"; }
qgit()    { as_qamc "cd '$QAMC_REPO' && git $1"; }

QAMC_UID="$(id -u "$QAMC_USER")"
SYSTEMD_ENV="export XDG_RUNTIME_DIR=/run/user/${QAMC_UID}; export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${QAMC_UID}/bus;"
sysctl_user() { as_qamc "$SYSTEMD_ENV systemctl --user $1"; }

health_json() { curl -sS --max-time 15 "$API_HEALTH" 2>/dev/null || true; }

health_field() {  # $1 = field
  health_json | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1]))
except Exception:
    print("")
' "$1" 2>/dev/null || true
}

wait_healthy() {  # $1 = seconds
  local deadline=$(( SECONDS + ${1:-60} )) status
  while (( SECONDS < deadline )); do
    status="$(health_field status)"
    if [[ "$status" == "ok" ]]; then return 0; fi
    sleep 2
  done
  return 1
}

ROLLED_BACK=0
rollback_code() {  # $1 = why
  printf '\n\033[1;31m[ROLLBACK]\033[0m %s\n' "$1" >&2
  qgit "checkout -- config/settings.yaml" >/dev/null 2>&1 || true
  if qgit "checkout --detach '$BASELINE_SHA'" >/dev/null 2>&1; then
    ROLLED_BACK=1
    printf '   production code restored to %s\n' "$BASELINE_SHA" >&2
  else
    printf '   \033[1;31mAUTOMATIC ROLLBACK FAILED\033[0m — do this by hand:\n' >&2
    printf '     sudo -u %s -H bash -c "cd %s && git checkout -- config/settings.yaml && git checkout --detach %s"\n' \
      "$QAMC_USER" "$QAMC_REPO" "$BASELINE_SHA" >&2
  fi
}

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 1 — PREFLIGHT (nothing is changed in this phase)"
# ═════════════════════════════════════════════════════════════════════════════

id "$QAMC_USER" >/dev/null 2>&1 || die "user '$QAMC_USER' does not exist."
[[ -d "$QAMC_REPO/.git" ]] || die "$QAMC_REPO is not a git checkout."

HEAD_NOW="$(qgit 'rev-parse HEAD')"
[[ "$HEAD_NOW" == "$BASELINE_SHA" ]] || die "production HEAD is $HEAD_NOW, expected the
       reviewed baseline $BASELINE_SHA.
       Production is not where this rollout was reviewed against. STOP and
       reconcile before deploying anything."
ok "production HEAD == $BASELINE_SHA (the reviewed baseline)"

DIRTY="$(qgit 'status --porcelain')"
[[ -z "$DIRTY" ]] || die "production working tree is dirty:
$(printf '%s\n' "$DIRTY" | sed 's/^/         /')
       Reconcile by hand before deploying. STOP."
ok "production working tree clean"

REPO_OWNER="$(stat -c '%U:%G' "$QAMC_REPO/main.py")"
[[ "$REPO_OWNER" == "${QAMC_USER}:${QAMC_USER}" ]] \
  || die "checkout is owned by $REPO_OWNER, expected ${QAMC_USER}:${QAMC_USER}. STOP."
ok "checkout owned by ${QAMC_USER}:${QAMC_USER}"

# --- runtime env file: hygiene only. No value is read or printed. ---
[[ -f "$QAMC_ENV" ]] || die "$QAMC_ENV is missing. STOP."
ENV_PERM="$(stat -c '%a %U:%G' "$QAMC_ENV")"
[[ "$ENV_PERM" == "600 ${QAMC_USER}:${QAMC_USER}" ]] \
  || die "runtime env file is '$ENV_PERM', expected '600 ${QAMC_USER}:${QAMC_USER}'. STOP."
ok "runtime env file is 600 ${QAMC_USER}:${QAMC_USER}"

TOKEN_LINES="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN=' '$QAMC_ENV'" || true)"
[[ "$TOKEN_LINES" == "1" ]] || die "TELEGRAM_BOT_TOKEN appears '${TOKEN_LINES:-0}' time(s) in the
       runtime env file — expected exactly 1. STOP."
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" \
  || die "the runtime env file does not hold the expected OneCLI placeholder in
       TELEGRAM_BOT_TOKEN. The real bot token must live ONLY in OneCLI.
       Inspect it as $QAMC_USER before continuing. STOP."
ok "runtime env holds ONLY the Telegram placeholder (real token stays in OneCLI)"

WIRING="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?(HTTPS_PROXY|SSL_CERT_FILE|REQUESTS_CA_BUNDLE)=' '$QAMC_ENV'" || true)"
[[ "$WIRING" == "3" ]] || die "OneCLI proxy/CA wiring lines = '${WIRING:-0}', expected 3. STOP."
ok "OneCLI proxy/CA wiring intact (3 lines)"

ENV_SHA_BEFORE="$(sha256sum "$QAMC_ENV" | cut -d' ' -f1)"
ok "runtime env fingerprint recorded (checked again after deploy)"

# --- OneCLI gateway ---
ONECLI_CODE="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$ONECLI_API/agents" || true)"
[[ "$ONECLI_CODE" == "200" ]] || die "OneCLI GET /agents returned '${ONECLI_CODE:-no response}',
       expected 200. The credential gateway must be healthy before deploying. STOP."
ok "OneCLI gateway healthy (GET /agents 200)"

# --- Mission Control health BEFORE ---
[[ "$(health_field status)" == "ok" ]] || die "Mission Control /health is not 'ok' before we
       start. Fix production health first. STOP."
for f in db_reachable broker_reachable paper; do
  [[ "$(health_field "$f")" == "True" ]] || die "pre-deploy /health.$f is not true. STOP."
done
ok "Mission Control healthy pre-deploy (db + broker reachable, paper=true)"

API_PID_BEFORE="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]')"
[[ -n "$API_PID_BEFORE" && "$API_PID_BEFORE" != "0" ]] \
  || die "could not read MainPID of $API_UNIT — is Mission Control a qamc user unit? STOP."
ok "$API_UNIT running (pid $API_PID_BEFORE)"

# --- Scheduled timers BEFORE ---
# The load-bearing safety property is that this set is IDENTICAL after the
# deploy (no timer added, removed, enabled or disabled). The expected count of
# 7 is a documentation expectation, not a safety invariant, so a mismatch warns
# loudly and is reported rather than blocking a rollout that changes nothing
# about scheduling.
TIMERS_BEFORE="$(sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null \
                  | awk '{print $1, $2}' | sort || true)"
[[ -n "$TIMERS_BEFORE" ]] || die "no quant-agent-*.timer unit files are visible to $QAMC_USER.
       The scheduled paper-trading surface cannot be verified. STOP."
TIMER_COUNT_BEFORE="$(printf '%s\n' "$TIMERS_BEFORE" | awk '$2 ~ /^enabled/ {c++} END {print c+0}')"
if [[ "$TIMER_COUNT_BEFORE" == "$EXPECTED_TIMERS" ]]; then
  ok "$EXPECTED_TIMERS enabled quant-agent timers (recorded for post-deploy comparison)"
else
  warn "found $TIMER_COUNT_BEFORE enabled quant-agent timers, expected $EXPECTED_TIMERS —"
  warn "reporting rather than blocking; this deploy changes no scheduling. Current set:"
  printf '%s\n' "$TIMERS_BEFORE" | sed 's/^/          /'
fi

# --- No trading session in flight ---
LOCK_ACTIVE="$(health_field session_lock_active)"
[[ "$LOCK_ACTIVE" == "False" ]] || die "a trading session lock is ACTIVE right now — a run is
       in flight. Wait for it to finish and re-run this script. STOP."
if pgrep -u "$QAMC_USER" -f 'main\.py --mode' >/dev/null 2>&1; then
  die "a 'main.py --mode ...' trading process is running as $QAMC_USER right now.
       Wait for it to finish and re-run this script. STOP."
fi
ok "no trading session in flight"

VENV_PY="${QAMC_REPO}/.venv/bin/python"
as_qamc "test -x '$VENV_PY'" || die "production venv python not found at $VENV_PY. STOP."
ok "production venv python present"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 2 — fetch + verify the target commit (production still untouched)"
# ═════════════════════════════════════════════════════════════════════════════
#
# Preferred path: an explicit SHA refspec into a throwaway ref, so no branch is
# followed at all. If the remote refuses a by-SHA want, we fall back to a plain
# `fetch origin main` — that updates refs/remotes/origin/main but is still safe,
# because nothing below reads a branch: the checkout is by exact SHA and the
# commit's TREE HASH is asserted against the reviewed one first.

qgit "fetch --no-tags origin '+${TARGET_SHA}:refs/qamc/finish-line-target'" >/dev/null 2>&1 \
  || qgit "fetch --no-tags origin main" >/dev/null 2>&1 \
  || die "could not fetch the target commit from origin. STOP."

qgit "cat-file -e ${TARGET_SHA}^{commit}" \
  || die "target commit $TARGET_SHA is not present after fetch. STOP."
ok "target commit $TARGET_SHA fetched"

FETCHED_TREE="$(qgit "rev-parse ${TARGET_SHA}^{tree}")"
[[ "$FETCHED_TREE" == "$TARGET_TREE" ]] \
  || die "target tree is $FETCHED_TREE, expected $TARGET_TREE.
       The commit's CONTENT is not what was reviewed. STOP."
ok "target tree matches the reviewed content exactly"

# --- the delta must be exactly the reviewed 21-file set ---
CHANGED="$(qgit "diff --name-only ${BASELINE_SHA} ${TARGET_SHA}")"
CHANGED_COUNT="$(printf '%s\n' "$CHANGED" | grep -c . || true)"
CHANGED_SHA="$(printf '%s\n' "$CHANGED" | sha256sum | cut -d' ' -f1)"
[[ "$CHANGED_COUNT" == "$EXPECTED_CHANGED_FILES" ]] \
  || die "delta touches $CHANGED_COUNT files, expected $EXPECTED_CHANGED_FILES. STOP."
[[ "$CHANGED_SHA" == "$EXPECTED_FILELIST_SHA" ]] \
  || die "the changed-file list does not match the reviewed one:
$(printf '%s\n' "$CHANGED" | sed 's/^/         /')
       STOP — unreviewed files would be deployed."
ok "delta is exactly the reviewed $EXPECTED_CHANGED_FILES-file set"

# --- content canaries on the target commit (searches the commit, not the
#     working tree, so untracked leftovers cannot fake a pass) ---
qgit "grep -q 'intraday_scan' ${TARGET_SHA} -- config/settings.yaml src/config.py" \
  || die "intraday_scan is absent from the target commit — this is not the PR #48 target. STOP."
ok "PR #48 intraday_scan present in the target commit"

qgit "grep -q '_compute_deployable_cash' ${TARGET_SHA} -- src/pipeline.py" \
  || die "SGOV deployable-cash fix absent from the target commit. STOP."
qgit "grep -q 'return confirmed' ${TARGET_SHA} -- src/execution/cash_sweep.py" \
  || die "cash-sweep confirmed-funding fix absent from the target commit (fund_buys must
       return the CONFIRMED raw-cash rise, not the submitted notional). STOP."
ok "PR #48 SGOV funding fix present in the target commit"

qgit "grep -q '_MAX_MISSING_RETRIES' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || die "Tech batch-completeness fix absent from the target commit. STOP."
ok "PR #48 Tech batch-completeness fix present in the target commit"

qgit "grep -q 'self._redact(exc)' ${TARGET_SHA} -- src/notifier.py" \
  || die "Telegram token redaction absent from the target commit — a failed send could
       write the bot token into the log. STOP."
ok "Telegram token redaction present in the target commit"

if qgit "grep -nE '^[[:space:]]*paper:[[:space:]]*false' ${TARGET_SHA} -- config/settings.yaml" 2>/dev/null; then
  die "config/settings.yaml in the target commit sets paper: false. HARD STOP."
fi
ok "Alpaca paper-only intact in the target commit"

TARGET_INTRADAY="$(qgit "show ${TARGET_SHA}:config/settings.yaml" \
  | awk '/^intraday_scan:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&/enabled:/{print $2; exit}')"
[[ "$TARGET_INTRADAY" == "false" ]] \
  || die "intraday_scan.enabled is '$TARGET_INTRADAY' in the target commit, expected false —
       the cutover must land with the scanner OFF. STOP."
ok "target commit ships intraday_scan.enabled: false (cutover lands with it off)"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 3 — deploy exact $TARGET_SHA (detached; main is never followed)"
# ═════════════════════════════════════════════════════════════════════════════

qgit "checkout --detach '$TARGET_SHA'" >/dev/null 2>&1 \
  || die "checkout of $TARGET_SHA failed — production may be mid-transition.
       Roll back by hand (command at the end of this script) and STOP."

NEW_SHA="$(qgit 'rev-parse HEAD')"
[[ "$NEW_SHA" == "$TARGET_SHA" ]] || { rollback_code "post-checkout HEAD is $NEW_SHA"; die "deploy verification failed. Rolled back."; }
DIRTY_AFTER="$(qgit 'status --porcelain')"
[[ -z "$DIRTY_AFTER" ]] || { rollback_code "working tree dirty right after checkout"; die "deploy verification failed. Rolled back."; }
ok "production HEAD == $TARGET_SHA, working tree clean"

ENV_SHA_AFTER="$(sha256sum "$QAMC_ENV" | cut -d' ' -f1)"
[[ "$ENV_SHA_AFTER" == "$ENV_SHA_BEFORE" ]] \
  || { rollback_code "the runtime env file changed during checkout"; die "env file must never be touched by a deploy. Rolled back."; }
ok "runtime env file byte-identical (deploy touched no secret material)"

REPO_OWNER_AFTER="$(stat -c '%U:%G' "$QAMC_REPO/main.py")"
[[ "$REPO_OWNER_AFTER" == "${QAMC_USER}:${QAMC_USER}" ]] \
  || { rollback_code "checkout ownership became $REPO_OWNER_AFTER"; die "ownership changed. Rolled back."; }
ok "checkout still owned by ${QAMC_USER}:${QAMC_USER}"

# --- import + config smoke under the PRODUCTION venv, before any restart ---
# Prints booleans and counts only. No secret is echoed. No broker order, no LLM
# call, no trading mode, no DB write.
say "PHASE 3b — production-venv import/config smoke (no trading action)"
SMOKE="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' - <<'PY'
import importlib, sys
for m in ('src.config', 'src.pipeline', 'src.pipeline_stages', 'src.execution.cash_sweep',
          'src.agents.tech_analyst', 'src.notifier', 'src.api'):
    importlib.import_module(m)
from src.config import load_config
c = load_config('config/settings.yaml')
print('paper=%s' % c.alpaca.paper)
print('intraday_enabled=%s' % c.intraday_scan.enabled)
print('intraday_threshold=%s' % c.intraday_scan.move_threshold_pct)
print('intraday_cooldown=%s' % c.intraday_scan.cooldown_hours)
print('intraday_cap=%s' % c.intraday_scan.max_candidates_per_scan)
print('sweep_enabled=%s' % c.cash_sweep.enabled)
print('sweep_symbol=%s' % c.cash_sweep.symbol)
print('reserve_pct=%s' % c.cash_sweep.reserve_pct)
print('universe=%d' % len(c.trading.universe))
print('inverse_etfs=%s' % ','.join(s for s in ('SH','SDS','PSQ','SQQQ') if s in c.trading.universe))
PY
" 2>&1)" || { rollback_code "production-venv smoke test failed"; printf '%s\n' "$SMOKE" | sed 's/^/         /' >&2; die "the deployed code does not import/load under the production venv. Rolled back."; }
printf '%s\n' "$SMOKE" | sed 's/^/         /'

grep -q '^paper=True$'            <<< "$SMOKE" || { rollback_code "loaded config is not paper-only"; die "HARD STOP. Rolled back."; }
grep -q '^intraday_enabled=False$' <<< "$SMOKE" || { rollback_code "intraday is not disabled at cutover"; die "STOP. Rolled back."; }
grep -q '^inverse_etfs=SH,SDS,PSQ,SQQQ$' <<< "$SMOKE" || warn "inverse ETF set is not exactly SH,SDS,PSQ,SQQQ — check the universe."
ok "deployed code imports cleanly; config is paper-only with intraday still OFF"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 4 — restart Mission Control on the deployed code"
# ═════════════════════════════════════════════════════════════════════════════
#
# Mission Control is read-only and non-critical to trading; restarting it is
# what makes "production runs the pinned SHA" true for the long-running
# process (it caches its config at startup). No trading process is touched.

sysctl_user "restart $API_UNIT" || { rollback_code "systemctl restart $API_UNIT failed"; die "restart failed. Code rolled back; restart the service by hand."; }
if ! wait_healthy 90; then
  rollback_code "Mission Control did not return healthy within 90s after restart"
  sysctl_user "restart $API_UNIT" || true
  wait_healthy 90 && warn "Mission Control recovered on the rolled-back code." \
                  || warn "Mission Control is STILL not healthy after rollback — investigate: systemctl --user status $API_UNIT"
  die "post-deploy health check failed. Production code rolled back to $BASELINE_SHA."
fi
API_PID_AFTER="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]')"
ok "$API_UNIT restarted (pid $API_PID_BEFORE -> $API_PID_AFTER) and healthy"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 5 — GATE B: production health, Telegram, timers, secret hygiene"
# ═════════════════════════════════════════════════════════════════════════════

for f in db_reachable broker_reachable paper; do
  [[ "$(health_field "$f")" == "True" ]] \
    || { rollback_code "post-deploy /health.$f is not true"; die "GATE B failed. Rolled back."; }
done
ok "post-deploy health: db reachable, broker reachable, paper=true"

ACC="$(curl -sS --max-time 20 http://127.0.0.1:8800/account 2>/dev/null || true)"
printf '%s' "$ACC" | python3 -c '
import json, sys
a = json.load(sys.stdin)
assert a.get("paper") is True, "account.paper is not True"
liq = a.get("liquidity") or {}
print("   [ OK ] account reads live: equity $%.2f, raw cash $%.2f, sweep parked $%.2f (%s)"
      % (a.get("portfolio_value") or 0.0, liq.get("raw_cash") or 0.0,
         liq.get("sweep_parked_value") or 0.0, liq.get("sweep_symbol")))
' || { rollback_code "post-deploy /account read failed or is not a paper account"; die "GATE B failed. Rolled back."; }

# --- Telegram: prove the credential path still works. getMe sends NO message. ---
GETME="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && \
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
  'https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe'" 2>/dev/null || true)"
if [[ "$GETME" == "200" ]]; then
  ok "Telegram healthy — OneCLI injected the real token (getMe 200; no message sent)"
else
  rollback_code "Telegram getMe returned '${GETME:-no response}', expected 200"
  die "GATE B failed: the Telegram credential path is broken on the deployed code.
       401 = token wrong/revoked, 404 = gateway did not substitute, 000 = egress blocked.
       Production code rolled back to $BASELINE_SHA."
fi

TOKEN_LINES_AFTER="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN=' '$QAMC_ENV'" || true)"
[[ "$TOKEN_LINES_AFTER" == "1" ]] || { rollback_code "TELEGRAM_BOT_TOKEN line count changed"; die "GATE B failed. Rolled back."; }
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" \
  || { rollback_code "runtime env no longer holds only the placeholder"; die "GATE B failed. Rolled back."; }
ok "runtime env still holds ONLY the placeholder; real token remains only in OneCLI"

# --- no secret material in the deployed tree ---
if qgit "grep -nE '^[[:space:]]*(ALPACA|OPENROUTER|TELEGRAM|FRED)[A-Z_]*=[A-Za-z0-9_-]{16,}' ${TARGET_SHA}" 2>/dev/null; then
  rollback_code "credential-looking material found in the deployed commit"
  die "HARD STOP. Rolled back."
fi
ok "no credential-looking material committed in the deployed tree"

# --- timers unchanged ---
TIMERS_AFTER="$(sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null \
                 | awk '{print $1, $2}' | sort || true)"
[[ "$TIMERS_AFTER" == "$TIMERS_BEFORE" ]] || {
  printf '   BEFORE:\n%s\n   AFTER:\n%s\n' \
    "$(printf '%s\n' "$TIMERS_BEFORE" | sed 's/^/     /')" \
    "$(printf '%s\n' "$TIMERS_AFTER" | sed 's/^/     /')" >&2
  rollback_code "the set of quant-agent timers changed across the deploy"
  die "GATE B failed. Rolled back."
}
ok "$EXPECTED_TIMERS quant-agent timers unchanged (no timer added, removed or disabled)"
printf '%s\n' "$TIMERS_AFTER" | sed 's/^/          /'

# --- wrappers still source the runtime env ---
for wrapper in run_if_et_window.sh run_daily_export.sh; do
  if [[ -f "$QAMC_REPO/scripts/$wrapper" ]]; then
    if grep -Eq 'source[[:space:]]+"?\$\{?PROJECT_ROOT\}?/\.env' "$QAMC_REPO/scripts/$wrapper"; then
      ok "$wrapper sources \$PROJECT_ROOT/.env"
    else
      warn "$wrapper does NOT source \$PROJECT_ROOT/.env — scheduled runs would lose the"
      warn "OneCLI proxy/CA and Telegram vars. Investigate before the next session."
    fi
  else
    warn "$QAMC_REPO/scripts/$wrapper not found in the deployed tree."
  fi
done

# --- intra_check is still scheduled by the existing wrapper (no new schedule) ---
grep -q 'intra_check)' "$QAMC_REPO/scripts/run_if_et_window.sh" \
  || warn "run_if_et_window.sh has no intra_check window case — the scanner would never fire."
ok "intra_check remains a case in the existing wrapper (no new schedule introduced)"

say "GATE B PASSED — production is healthy on $TARGET_SHA with intraday still OFF"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 6 — GATE D: enable intraday opportunity discovery"
# ═════════════════════════════════════════════════════════════════════════════
#
# Authorized by docs/WORK.md Stage D once Gates A-C pass. This edits exactly
# one line of the deployed config/settings.yaml, in place, scoped to the
# intraday_scan block. It adds NO timer, service, daemon or scheduler: the
# scanner runs inside the existing intra_check cadence.

EDITOR_PY='
import re, sys
path = sys.argv[1]
with open(path) as fh:
    lines = fh.read().split("\n")
in_block = False
changed = 0
already = False
for i, line in enumerate(lines):
    if re.match(r"^intraday_scan:\s*$", line):
        in_block = True
        continue
    if in_block:
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break                      # left the block
        m = re.match(r"^(\s*)enabled:\s*(\S+)(.*)$", line)
        if m:
            if m.group(2) == "true":
                already = True
            elif m.group(2) == "false":
                lines[i] = "%senabled: true%s" % (m.group(1), m.group(3))
                changed += 1
            break
if already and changed == 0:
    print("ALREADY_ENABLED")
    sys.exit(0)
if changed != 1:
    sys.exit("expected exactly 1 replacement inside intraday_scan, made %d" % changed)
with open(path, "w") as fh:
    fh.write("\n".join(lines))
print("ENABLED")
'
EDIT_RESULT="$(sudo -u "$QAMC_USER" -H python3 -c "$EDITOR_PY" "$QAMC_REPO/config/settings.yaml")" \
  || { rollback_code "failed to enable intraday_scan in the deployed config"; die "GATE D failed. Rolled back."; }
ok "config/settings.yaml intraday_scan: $EDIT_RESULT"

# The ONLY expected production-vs-commit delta is this one line.
NUMSTAT="$(qgit 'diff --numstat -- config/settings.yaml' | tr -s '[:space:]' ' ' | sed 's/ $//')"
[[ "$NUMSTAT" == "1 1 config/settings.yaml" ]] \
  || { rollback_code "config edit produced an unexpected diff: '$NUMSTAT'"; die "GATE D failed. Rolled back."; }
STATUS_LINES="$(qgit 'status --porcelain')"
[[ "$STATUS_LINES" == " M config/settings.yaml" ]] \
  || { rollback_code "unexpected working-tree changes: $STATUS_LINES"; die "GATE D failed. Rolled back."; }
ok "exactly one line changed, in exactly one file"
qgit 'diff -- config/settings.yaml' | sed 's/^/          /'

# Verify from the ACTUAL loaded runtime configuration, not from the file text.
SMOKE2="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' - <<'PY'
from src.config import load_config
c = load_config('config/settings.yaml')
print('paper=%s' % c.alpaca.paper)
print('intraday_enabled=%s' % c.intraday_scan.enabled)
print('intraday_threshold=%s' % c.intraday_scan.move_threshold_pct)
print('intraday_cooldown=%s' % c.intraday_scan.cooldown_hours)
print('intraday_cap=%s' % c.intraday_scan.max_candidates_per_scan)
PY
" 2>&1)" || { rollback_code "runtime config failed to load after enabling intraday"; die "GATE D failed. Rolled back."; }
printf '%s\n' "$SMOKE2" | sed 's/^/         /'
grep -q '^intraday_enabled=True$' <<< "$SMOKE2" \
  || { rollback_code "runtime config still reports intraday disabled"; die "GATE D failed. Rolled back."; }
grep -q '^paper=True$' <<< "$SMOKE2" \
  || { rollback_code "runtime config is no longer paper-only"; die "HARD STOP. Rolled back."; }
ok "RUNTIME configuration reports intraday_scan.enabled = True, paper = True"

# Mission Control is still healthy after the config edit (it caches config at
# startup, so this proves the edit broke nothing; the scanner is a trading-side
# feature and needs no API restart).
[[ "$(health_field status)" == "ok" ]] || warn "Mission Control /health is not ok after the config edit — check it."
ok "Mission Control still healthy"

say "GATE D PASSED — intraday opportunity discovery is ENABLED on the existing cadence"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 7 — FINAL STATE"
# ═════════════════════════════════════════════════════════════════════════════

FINAL_SHA="$(qgit 'rev-parse HEAD')"
cat <<EOF

  Production SHA        : $FINAL_SHA
  Was                   : $BASELINE_SHA
  Config delta          : config/settings.yaml — intraday_scan.enabled false -> true
                          (the ONE expected, recorded production-vs-commit delta)
  Alpaca                : Paper only (enforced in loaded config)
  Intraday scan         : ENABLED — existing intra_check cadence, no new timer
  Telegram              : healthy (getMe 200); real token only in OneCLI; no message sent
  Mission Control       : restarted on the deployed SHA and healthy
  Timers                : $EXPECTED_TIMERS quant-agent timers, unchanged
  Trading runs          : none invoked by this script; no order was placed

  Next scheduled intra_check ticks fire every 30 min inside 09:30-16:00 ET on
  the next weekday. The scanner is bounded: >=3.0% move since last close,
  3.0h per-symbol cooldown, max 5 candidates per tick, and it backs off when
  another session is mid-flight or another scan holds the process lock.

  ROLLBACK (code + config, one command):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout -- config/settings.yaml && git checkout --detach $BASELINE_SHA" \\
        && sudo -u $QAMC_USER -H bash -c "$SYSTEMD_ENV systemctl --user restart $API_UNIT"

  DISABLE INTRADAY ONLY (keep the deployed code):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout -- config/settings.yaml"

  The checkout is intentionally DETACHED at a pinned SHA. Do not "tidy" it with
  \`git checkout main\` — that would follow a moving branch into production.

EOF
[[ "$ROLLED_BACK" -eq 0 ]] || die "script finished in a rolled-back state — read the log above."
say "DONE — finish-line rollout complete"
