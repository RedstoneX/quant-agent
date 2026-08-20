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
# intraday opportunity scanner. The governed A -> B -> C -> D sequence of
# docs/WORK.md is preserved: every stage gate is an explicit, ordered,
# fail-closed checkpoint, and D is not reached unless C passed on the deployed
# production tree.
#
#   PHASE 1  PREFLIGHT (Gate A, runtime half) — verify everything, change nothing.
#   PHASE 2  FETCH + VERIFY the exact target commit's CONTENT before checkout.
#   PHASE 3  DEPLOY the pinned SHA + import/config smoke under the production venv.
#   PHASE 4  RESTART Mission Control onto the deployed SHA.
#   PHASE 5  GATE B  — production healthy on the target, intraday still OFF.
#   PHASE 6  GATE C  — PR #48 behaviour proven on the DEPLOYED tree.
#   PHASE 7  GATE D  — enable intraday_scan, verified from the runtime config.
#   PHASE 8  FINAL REPORT.
#
# FAIL-CLOSED: every check is a hard stop. There is no warn-and-continue path
# for any prerequisite. Any failure from PHASE 3 onward runs `rollback_all`,
# which converges BOTH the checkout/config AND the Mission Control process back
# to the baseline — a rollback never leaves the API executing target code.
#
# WHAT IT DOES NOT DO — no new service/timer/daemon, no package installs, no
# firewall/network/account changes, no OneCLI changes, no secret is read,
# written, printed or moved, no Telegram message is sent, no paid model call is
# made, no trading run is invoked and no order is ever placed.
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
API_BASE="http://127.0.0.1:8800"
ONECLI_API="http://127.0.0.1:10254/api"
TG_PLACEHOLDER="ONECLI-INJECTS-THIS-PLACEHOLDER"
REQUIRED_TIMERS=7          # asserted, never assumed — see PHASE 1e
GATE_C_MIN_TESTS=240       # 246 collected on the reviewed tree; floor guards under-collection

# ── Output helpers (never used to print secret material) ─────────────────────
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   [ OK ] %s\n' "$*"; }
info() { printf '   [ .. ] %s\n' "$*"; }
note() { printf '   [NOTE] %s\n' "$*"; }
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

TMPD="$(mktemp -d /tmp/qamc-rollout.XXXXXX)"
# 0711: root-owned, and traversable (not listable) by qamc so the Gate C
# export subdir below — which qamc owns at 0700 — is reachable by that account.
chmod 0711 "$TMPD"
cleanup() { [[ -n "${TMPD:-}" && -d "$TMPD" ]] && rm -rf "$TMPD" || true; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

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
  local deadline=$(( SECONDS + ${1:-60} ))
  while (( SECONDS < deadline )); do
    [[ "$(health_field status)" == "ok" ]] && return 0
    sleep 2
  done
  return 1
}

# ── Rollback: converge BOTH the tree/config AND the running API to baseline ──
#
# Mission Control caches its configuration at startup and holds imported
# modules in memory, so reverting the checkout alone would leave the API
# process executing target code against a baseline tree. Every post-deploy
# failure therefore restarts the service as part of the rollback and proves it
# came back healthy, and says so explicitly if it could not.
rollback_all() {  # $1 = why
  printf '\n\033[1;31m[ROLLBACK]\033[0m %s\n' "$1" >&2
  local head_after="" dirty_after="" pid_after="" converged=1

  qgit "checkout -- config/settings.yaml" >/dev/null 2>&1 || true
  if qgit "checkout --detach '$BASELINE_SHA'" >/dev/null 2>&1; then
    head_after="$(qgit 'rev-parse HEAD' 2>/dev/null || echo unknown)"
    dirty_after="$(qgit 'status --porcelain' 2>/dev/null || echo '?')"
    if [[ "$head_after" == "$BASELINE_SHA" && -z "$dirty_after" ]]; then
      printf '   [ROLLBACK] checkout + config restored to %s (tree clean)\n' "$BASELINE_SHA" >&2
    else
      converged=0
      printf '   [ROLLBACK] \033[1;31mtree NOT clean\033[0m — HEAD=%s status=%s\n' \
        "$head_after" "${dirty_after:-clean}" >&2
    fi
  else
    converged=0
    printf '   [ROLLBACK] \033[1;31mgit rollback FAILED\033[0m\n' >&2
  fi

  # Converge the process, not just the files.
  if sysctl_user "restart $API_UNIT" >/dev/null 2>&1 && wait_healthy 90; then
    pid_after="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]' || true)"
    printf '   [ROLLBACK] %s restarted on baseline code and healthy (pid %s)\n' \
      "$API_UNIT" "${pid_after:-?}" >&2
  else
    converged=0
    printf '   [ROLLBACK] \033[1;31m%s did NOT come back healthy\033[0m\n' "$API_UNIT" >&2
  fi

  if [[ "$converged" -eq 1 ]]; then
    die "$1

       PRODUCTION HAS BEEN ROLLED BACK AND IS CONVERGED:
         checkout : $BASELINE_SHA (clean, no config delta)
         API      : restarted on baseline code, /health ok
         intraday : NOT enabled
       Trading is unaffected. Investigate, then re-run this script."
  fi
  die "$1

       \033[1;31mROLLBACK DID NOT FULLY CONVERGE — FINISH IT BY HAND:\033[0m
         sudo -u $QAMC_USER -H bash -c \"cd $QAMC_REPO && git checkout -- config/settings.yaml && git checkout --detach $BASELINE_SHA\"
         sudo -u $QAMC_USER -H bash -c \"$SYSTEMD_ENV systemctl --user restart $API_UNIT\"
         curl -s $API_HEALTH
       Then confirm HEAD == $BASELINE_SHA and /health is ok before anything else."
}

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 1 — PREFLIGHT / Gate A runtime half (nothing is changed in this phase)"
# ═════════════════════════════════════════════════════════════════════════════

id "$QAMC_USER" >/dev/null 2>&1 || die "user '$QAMC_USER' does not exist."
[[ -d "$QAMC_REPO/.git" ]] || die "$QAMC_REPO is not a git checkout."

# ── 1a. code position ────────────────────────────────────────────────────────
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

# ── 1b. runtime environment file: hygiene only, no value is read or printed ──
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
ok "runtime env fingerprint recorded (re-checked after deploy)"

# ── 1c. OneCLI gateway ───────────────────────────────────────────────────────
ONECLI_CODE="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$ONECLI_API/agents" || true)"
[[ "$ONECLI_CODE" == "200" ]] || die "OneCLI GET /agents returned '${ONECLI_CODE:-no response}',
       expected 200. The credential gateway must be healthy before deploying. STOP."
ok "OneCLI gateway healthy (GET /agents 200)"

# ── 1d. Mission Control before ───────────────────────────────────────────────
[[ "$(health_field status)" == "ok" ]] || die "Mission Control /health is not 'ok' before we
       start. Fix production health first. STOP."
for f in db_reachable broker_reachable paper; do
  [[ "$(health_field "$f")" == "True" ]] || die "pre-deploy /health.$f is not true. STOP."
done
ok "Mission Control healthy pre-deploy (db + broker reachable, paper=true)"

API_PID_BEFORE="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]' || true)"
[[ -n "$API_PID_BEFORE" && "$API_PID_BEFORE" != "0" ]] \
  || die "could not read MainPID of $API_UNIT. Mission Control must be a running
       $QAMC_USER systemd --user unit for this rollout's restart/rollback path
       to work. STOP."
ok "$API_UNIT running (pid $API_PID_BEFORE)"

# ── 1e. scheduled surface: exactly REQUIRED_TIMERS enabled AND all active ────
#
# Fail-closed. The count is asserted from the live unit files, never printed
# unless it was actually verified, and the exact set is captured so PHASE 5 can
# prove the deploy changed nothing about scheduling.
TIMER_UNITS_RAW="$(sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null || true)"
[[ -n "${TIMER_UNITS_RAW//[[:space:]]/}" ]] || die "no quant-agent-*.timer unit files are visible
       to $QAMC_USER — the scheduled paper-trading surface cannot be verified. STOP."
TIMERS_BEFORE="$(printf '%s\n' "$TIMER_UNITS_RAW" | awk 'NF {print $1, $2}' | sort)"
ENABLED_TIMERS="$(printf '%s\n' "$TIMERS_BEFORE" | awk '$2 ~ /^enabled/ {print $1}')"
ENABLED_COUNT="$(printf '%s\n' "$ENABLED_TIMERS" | grep -c . || true)"
[[ "$ENABLED_COUNT" == "$REQUIRED_TIMERS" ]] || die "found $ENABLED_COUNT enabled quant-agent
       timer(s), require exactly $REQUIRED_TIMERS. Current unit files:
$(printf '%s\n' "$TIMERS_BEFORE" | sed 's/^/         /')
       The scheduled paper-trading surface is not the reviewed one. STOP."

while read -r t; do
  [[ -n "$t" ]] || continue
  TSTATE="$(sysctl_user "is-active $t" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$TSTATE" == "active" ]] || die "timer $t is '$TSTATE', expected 'active'.
       An enabled-but-inactive timer never fires. STOP."
done <<< "$ENABLED_TIMERS"

FAILED_UNITS="$(sysctl_user "list-units --failed --no-legend 'quant-agent-*'" 2>/dev/null || true)"
[[ -z "${FAILED_UNITS//[[:space:]]/}" ]] || die "quant-agent unit(s) are in a FAILED state:
$(printf '%s\n' "$FAILED_UNITS" | sed 's/^/         /')
       Fix the scheduled surface before deploying. STOP."
ok "$ENABLED_COUNT enabled quant-agent timers, all active, none failed (verified)"
printf '%s\n' "$ENABLED_TIMERS" | sed 's/^/          /'

# ── 1f. wrappers + intra_check cadence (fail-closed) ────────────────────────
check_wrappers() {  # returns non-zero with a reason on stderr
  local w
  for w in run_if_et_window.sh run_daily_export.sh; do
    [[ -f "$QAMC_REPO/scripts/$w" ]] || { echo "scripts/$w is missing from the checkout"; return 1; }
    grep -Eq 'source[[:space:]]+"?\$\{?PROJECT_ROOT\}?/\.env' "$QAMC_REPO/scripts/$w" \
      || { echo "scripts/$w does not source \$PROJECT_ROOT's runtime env file — scheduled runs would lose the OneCLI proxy/CA and Telegram variables"; return 1; }
  done
  grep -q 'intra_check)' "$QAMC_REPO/scripts/run_if_et_window.sh" \
    || { echo "run_if_et_window.sh has no intra_check window case — the intraday scanner would never fire"; return 1; }
  return 0
}
WRAP_ERR="$(check_wrappers)" || die "scheduled wrapper prerequisite failed: $WRAP_ERR. STOP."
ok "both wrappers exist and source the runtime env; intra_check window case present"

UNITS_CAT="$(sysctl_user "cat 'quant-agent-*.service'" 2>/dev/null || true)"
[[ -n "${UNITS_CAT//[[:space:]]/}" ]] || die "could not read $QAMC_USER's quant-agent service
       units — the scheduled path cannot be verified. STOP."
grep -q 'run_if_et_window.sh' <<< "$UNITS_CAT" \
  || die "no quant-agent service unit invokes run_if_et_window.sh. STOP."
grep -q 'intra_check' <<< "$UNITS_CAT" \
  || die "no quant-agent service unit invokes the intra_check mode — the intraday
       scanner this rollout enables would never be reached. STOP."

INTRA_SERVICE="$(printf '%s\n' "$UNITS_CAT" | awk '
  /^# \// { unit = $2; sub(/.*\//, "", unit); next }
  /intra_check/ { if (unit != "") { print unit; exit } }
')"
[[ -n "$INTRA_SERVICE" ]] || die "could not identify which service unit runs intra_check. STOP."
INTRA_TIMER="${INTRA_SERVICE%.service}.timer"
grep -qx "$INTRA_TIMER" <<< "$ENABLED_TIMERS" \
  || die "the intra_check service is $INTRA_SERVICE but its timer ($INTRA_TIMER) is not in
       the enabled set. The scanner would never fire on the existing cadence. STOP."
ok "intra_check cadence verified: $INTRA_SERVICE scheduled by $INTRA_TIMER (enabled + active)"

# ── 1g. nothing in flight ────────────────────────────────────────────────────
[[ "$(health_field session_lock_active)" == "False" ]] || die "a trading session lock is ACTIVE
       right now — a run is in flight. Wait for it to finish and re-run. STOP."
if pgrep -u "$QAMC_USER" -f 'main\.py --mode' >/dev/null 2>&1; then
  die "a 'main.py --mode ...' trading process is running as $QAMC_USER right now.
       Wait for it to finish and re-run this script. STOP."
fi
ok "no trading session in flight"

# ── 1h. production interpreter ───────────────────────────────────────────────
VENV_PY="${QAMC_REPO}/.venv/bin/python"
as_qamc "test -x '$VENV_PY'" || die "production venv python not found at $VENV_PY. STOP."
if as_qamc "'$VENV_PY' -m pytest --version" >/dev/null 2>&1; then
  HAVE_PYTEST=1
  ok "production venv python present, pytest available (Gate C will re-run the focused suites)"
else
  HAVE_PYTEST=0
  ok "production venv python present (pytest not installed — see Gate C, C2)"
fi

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
ok "target tree matches the reviewed content exactly ($TARGET_TREE)"

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

# Content canaries against the COMMIT (not the working tree), so untracked
# leftovers cannot fake a pass.
qgit "grep -q 'intraday_scan' ${TARGET_SHA} -- config/settings.yaml src/config.py" \
  || die "intraday_scan is absent from the target commit — this is not the PR #48 target. STOP."
qgit "grep -q '_compute_deployable_cash' ${TARGET_SHA} -- src/pipeline.py" \
  || die "SGOV deployable-cash fix absent from the target commit. STOP."
qgit "grep -qF 'return confirmed' ${TARGET_SHA} -- src/execution/cash_sweep.py" \
  || die "cash-sweep confirmed-funding fix absent from the target commit (fund_buys must
       return the CONFIRMED raw-cash rise, not the submitted notional). STOP."
qgit "grep -qF '_MAX_MISSING_RETRIES' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || die "Tech batch-completeness fix absent from the target commit. STOP."
qgit "grep -qF 'self._redact(exc)' ${TARGET_SHA} -- src/notifier.py" \
  || die "Telegram token redaction absent from the target commit — a failed send could
       write the bot token into the log. STOP."
ok "all three PR #48 fixes and the Telegram redaction present in the target commit"

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
  || die "checkout of $TARGET_SHA failed — production may be mid-transition. Roll back:
         sudo -u $QAMC_USER -H bash -c \"cd $QAMC_REPO && git checkout --detach $BASELINE_SHA\"
       then STOP."

NEW_SHA="$(qgit 'rev-parse HEAD')"
[[ "$NEW_SHA" == "$TARGET_SHA" ]] || rollback_all "post-checkout HEAD is $NEW_SHA, expected $TARGET_SHA"
[[ -z "$(qgit 'status --porcelain')" ]] || rollback_all "working tree is dirty right after checkout"
DEPLOYED_TREE="$(qgit 'rev-parse HEAD^{tree}')"
[[ "$DEPLOYED_TREE" == "$TARGET_TREE" ]] || rollback_all "deployed tree is $DEPLOYED_TREE, expected $TARGET_TREE"
ok "production HEAD == $TARGET_SHA, tree == $TARGET_TREE, working tree clean"

ENV_SHA_AFTER="$(sha256sum "$QAMC_ENV" | cut -d' ' -f1)"
[[ "$ENV_SHA_AFTER" == "$ENV_SHA_BEFORE" ]] \
  || rollback_all "the runtime env file changed during checkout — a deploy must never touch it"
ok "runtime env file byte-identical (deploy touched no secret material)"

REPO_OWNER_AFTER="$(stat -c '%U:%G' "$QAMC_REPO/main.py")"
[[ "$REPO_OWNER_AFTER" == "${QAMC_USER}:${QAMC_USER}" ]] \
  || rollback_all "checkout ownership became $REPO_OWNER_AFTER"
ok "checkout still owned by ${QAMC_USER}:${QAMC_USER}"

WRAP_ERR="$(check_wrappers)" || rollback_all "post-deploy wrapper check failed: $WRAP_ERR"
ok "wrappers in the deployed tree still source the runtime env; intra_check case present"

# ── import + config smoke under the PRODUCTION venv, before any restart ──────
# Prints booleans and counts only. No secret is echoed, no trading mode, no
# order, no LLM call, no DB write.
say "PHASE 3b — production-venv import/config smoke (no trading action)"
SMOKE="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' - <<'PY'
import importlib
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
" 2>&1)" || { printf '%s\n' "$SMOKE" | sed 's/^/         /' >&2; rollback_all "the deployed code does not import/load under the production venv"; }
printf '%s\n' "$SMOKE" | sed 's/^/         /'

grep -q '^paper=True$'                  <<< "$SMOKE" || rollback_all "loaded config is not paper-only"
grep -q '^intraday_enabled=False$'      <<< "$SMOKE" || rollback_all "intraday is not disabled at cutover"
grep -q '^sweep_enabled=True$'          <<< "$SMOKE" || rollback_all "cash sweep is not enabled in the deployed config"
grep -q '^sweep_symbol=SGOV$'           <<< "$SMOKE" || rollback_all "sweep symbol is not SGOV"
grep -q '^inverse_etfs=SH,SDS,PSQ,SQQQ$' <<< "$SMOKE" \
  || rollback_all "the approved inverse ETFs (SH, SDS, PSQ, SQQQ) are not all in the trading universe — bearish expression would be unavailable"
ok "deployed code imports cleanly; paper-only, sweep on SGOV, all four inverse ETFs present, intraday OFF"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 4 — restart Mission Control onto the deployed code"
# ═════════════════════════════════════════════════════════════════════════════
#
# Mission Control is read-only and non-critical to trading; restarting it is
# what makes "production runs the pinned SHA" true for the long-running process
# (it caches its configuration at startup). No trading process is touched.

sysctl_user "restart $API_UNIT" || rollback_all "systemctl restart $API_UNIT failed"
wait_healthy 90 || rollback_all "Mission Control did not return healthy within 90s of restarting on the target code"
API_PID_AFTER="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]' || true)"
ok "$API_UNIT restarted (pid $API_PID_BEFORE -> ${API_PID_AFTER:-?}) and healthy"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 5 — GATE B: production healthy on the target, intraday still OFF"
# ═════════════════════════════════════════════════════════════════════════════

for f in db_reachable broker_reachable paper; do
  [[ "$(health_field "$f")" == "True" ]] || rollback_all "post-deploy /health.$f is not true"
done
ok "post-deploy health: database reachable, broker reachable, paper=true"

ACC="$(curl -sS --max-time 20 "$API_BASE/account" 2>/dev/null || true)"
printf '%s' "$ACC" | python3 -c '
import json, sys
a = json.load(sys.stdin)
assert a.get("paper") is True, "account.paper is not True"
liq = a.get("liquidity") or {}
print("   [ OK ] account reads live: equity $%.2f, raw cash $%.2f, sweep parked $%.2f (%s)"
      % (a.get("portfolio_value") or 0.0, liq.get("raw_cash") or 0.0,
         liq.get("sweep_parked_value") or 0.0, liq.get("sweep_symbol")))
' || rollback_all "post-deploy /account read failed or is not a paper account"

# ── OneCLI / provider / model-routing / market-data / FRED wiring ────────────
#
# docs/WORK.md Gate B requires OpenRouter/model routing, provider, database and
# FRED wiring to be verified. This uses the ACCEPTED commissioning verifier
# rather than an ad-hoc probe, and deliberately WITHOUT `--live`:
#
#   * `config`          pins the per-seat model-routing policy from the loaded
#                       configuration (all agents on openrouter, the accepted
#                       two-model split) and re-asserts alpaca paper-only.
#   * `gateway`/`wiring` prove OneCLI is up, loopback-bound, and that the
#                       runtime proxy/CA wiring resolves.
#   * `providers`       proves credential INJECTION end to end through the real
#                       gateway for OpenRouter, Alpaca trading, Alpaca market
#                       data and FRED — using each provider's free metadata
#                       endpoint with a FAKE credential. No model completion is
#                       requested, so no paid call is made.
#   * `mission-control` proves the API is up, private and read-only.
#
# Deliberately EXCLUDED, with reasons (not silent skips):
#   * `preflight` is the only group that spends money (one real completion per
#     policy model) and it SKIPs unless `--live` is passed. It is not repeated
#     here: docs/STATE.md records the accepted commissioning run of 2026-08-14
#     (37 PASS / 0 FAIL / 0 WARN / 1 SKIP, "COMMISSIONING ACCEPTANCE: PASS")
#     which included real OpenRouter completions for BOTH accepted policy
#     models, and this rollout's 21-file delta changes no provider, client,
#     routing or credential code. `providers` above still proves the OpenRouter
#     credential path works right now, end to end, for free.
#   * `safety`'s "trading timers disabled" assertion inverts after activation —
#     it FAILs by design once timers are enabled, which they have been since
#     the authorized soak began on 2026-08-14. Its "no secrets committed" half
#     is covered by the explicit canary below, and the timer surface is
#     verified far more strictly in PHASE 1e / below.
#   * `isolation` is the known off-account check, resolved by the already-green
#     `dev` commissioning run recorded in docs/STATE.md.
# stderr is captured separately so a traceback cannot corrupt the JSON on
# stdout, and is printed if parsing fails — a silent "no parsable JSON" would
# be unactionable.
VERIFY_JSON="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' \
  ops/commissioning/verify_commissioning.py \
  --group config --group gateway --group wiring --group providers \
  --group mission-control --json" 2>"$TMPD/verify.err" || true)"
if [[ -z "${VERIFY_JSON//[[:space:]]/}" ]]; then
  [[ -s "$TMPD/verify.err" ]] && sed 's/^/         /' "$TMPD/verify.err" >&2
  rollback_all "the commissioning verifier produced no output on the deployed code"
fi
#
# Fail-closed parsing: every result in every requested group must be PASS. A
# SKIP or WARN here is a regression, not an acceptable outcome — the accepted
# 2026-08-14 commissioning run on this account recorded 0 FAIL / 0 WARN and its
# single SKIP was the `isolation` group, which is not requested above. An empty
# or short result set is also rejected, so "no checks ran" can never read as
# "nothing failed".
printf '%s' "$VERIFY_JSON" | python3 -c '
import json, sys

REQUIRED_GROUPS = {"config", "gateway", "wiring", "providers", "mission-control"}
MIN_PROVIDER_PROBES = 4   # openrouter, alpaca trading, alpaca market data, fred

try:
    d = json.load(sys.stdin)
except Exception as exc:
    sys.exit("commissioning verifier produced no parsable JSON (%s)" % exc)

results = d.get("results") or []
if not results:
    sys.exit("commissioning verifier returned no results at all")

bad, seen, providers = [], set(), 0
for r in results:
    group, name = r.get("group"), r.get("name")
    status = r.get("status")
    detail = (r.get("detail") or "").strip()
    seen.add(group)
    if group == "providers":
        providers += 1
    print("   [%4s] %s / %s" % (status, group, name))
    if detail:
        print("          " + detail.splitlines()[0][:150])
    if status != "PASS":
        bad.append("%s/%s is %s: %s" % (group, name, status, detail.splitlines()[0] if detail else ""))

missing = REQUIRED_GROUPS - seen
if missing:
    sys.exit("commissioning group(s) produced no result: %s" % ", ".join(sorted(missing)))
if providers < MIN_PROVIDER_PROBES:
    sys.exit("only %d provider probe(s) ran, expected at least %d "
             "(OpenRouter, Alpaca trading, Alpaca market data, FRED)"
             % (providers, MIN_PROVIDER_PROBES))
if bad:
    sys.exit("commissioning non-PASS result(s): " + " | ".join(bad))

print("   [ OK ] commissioning: %d checks, all PASS across %s "
      "(%d provider credential-injection probes, no paid model call)"
      % (len(results), ", ".join(sorted(seen)), providers))
' || { [[ -s "$TMPD/verify.err" ]] && sed 's/^/         /' "$TMPD/verify.err" >&2
       rollback_all "commissioning verification failed on the deployed code (see output above)"; }

# ── Telegram: prove the credential path. getMe sends NO message. ─────────────
GETME="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && \
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
  'https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe'" 2>/dev/null || true)"
[[ "$GETME" == "200" ]] || rollback_all "Telegram getMe returned '${GETME:-no response}', expected 200
       (401 = token wrong/revoked, 404 = gateway did not substitute, 000 = egress blocked)"
ok "Telegram healthy — OneCLI injected the real token (getMe 200; no message sent)"

TOKEN_LINES_AFTER="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN=' '$QAMC_ENV'" || true)"
[[ "$TOKEN_LINES_AFTER" == "1" ]] || rollback_all "TELEGRAM_BOT_TOKEN line count changed to '${TOKEN_LINES_AFTER:-0}'"
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" \
  || rollback_all "the runtime env no longer holds only the Telegram placeholder"
ok "runtime env still holds ONLY the placeholder; real token remains only in OneCLI"

if qgit "grep -nE '^[[:space:]]*(ALPACA|OPENROUTER|TELEGRAM|FRED)[A-Z_]*=[A-Za-z0-9_-]{16,}' ${TARGET_SHA}" 2>/dev/null; then
  rollback_all "credential-looking material found in the deployed commit"
fi
ok "no credential-looking material committed in the deployed tree"

# ── scheduled surface unchanged by the deploy (fail-closed) ─────────────────
TIMER_UNITS_RAW2="$(sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null || true)"
TIMERS_AFTER="$(printf '%s\n' "$TIMER_UNITS_RAW2" | awk 'NF {print $1, $2}' | sort)"
[[ "$TIMERS_AFTER" == "$TIMERS_BEFORE" ]] || {
  printf '   BEFORE:\n%s\n   AFTER:\n%s\n' \
    "$(printf '%s\n' "$TIMERS_BEFORE" | sed 's/^/     /')" \
    "$(printf '%s\n' "$TIMERS_AFTER" | sed 's/^/     /')" >&2
  rollback_all "the set of quant-agent timer units changed across the deploy"
}
while read -r t; do
  [[ -n "$t" ]] || continue
  TSTATE="$(sysctl_user "is-active $t" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$TSTATE" == "active" ]] || rollback_all "timer $t is '$TSTATE' after the deploy, expected 'active'"
done <<< "$ENABLED_TIMERS"
FAILED_UNITS2="$(sysctl_user "list-units --failed --no-legend 'quant-agent-*'" 2>/dev/null || true)"
[[ -z "${FAILED_UNITS2//[[:space:]]/}" ]] || rollback_all "quant-agent unit(s) entered a FAILED state during the deploy"
ok "$ENABLED_COUNT quant-agent timers unchanged, all still active, none failed"

# ── intraday must still be OFF at the end of Gate B ─────────────────────────
[[ "$TARGET_INTRADAY" == "false" ]] || rollback_all "internal ordering error: intraday was not false at Gate B"
grep -q '^intraday_enabled=False$' <<< "$SMOKE" || rollback_all "intraday is not disabled at the end of Gate B"
ok "intraday_scan still DISABLED (enablement is Gate D, after Gate C)"

say "GATE B PASSED — production healthy on $TARGET_SHA, intraday still OFF"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 6 — GATE C: prove PR #48 behaviour on the DEPLOYED tree"
# ═════════════════════════════════════════════════════════════════════════════
#
# docs/WORK.md Stage C, run AFTER deployment and BEFORE enablement. It uses
# deterministic evidence plus safe read-only production verification. No trade
# is forced, no order is placed, no LLM call is made.

# ── C1. deterministic evidence transfer ─────────────────────────────────────
# The reviewed full suite (1925 passed, 0 failed) ran against tree
# $TARGET_TREE on the dev account. PHASE 3 proved the DEPLOYED tree hash is
# byte-identical to it, so that evidence applies to this checkout rather than
# to "some commit called main".
[[ "$DEPLOYED_TREE" == "$TARGET_TREE" ]] \
  || rollback_all "C1: deployed tree no longer matches the reviewed tree"
ok "C1 deployed tree == reviewed tree $TARGET_TREE (full-suite evidence: 1925 passed, 0 failed)"

# ── C2. re-run the focused PR #48 suites on the deployed commit ─────────────
# Exported to a throwaway directory with `git archive`, so the production
# checkout is never written to, and run WITHOUT the runtime env sourced, so no
# real credential is ever in scope for a test process. storage.db_path is
# relative, so the export is fully self-contained.
if [[ "$HAVE_PYTEST" -eq 1 ]]; then
  EXPORTDIR="$TMPD/gate-c-export"
  install -d -o "$QAMC_USER" -g "$QAMC_USER" -m 0700 "$EXPORTDIR"
  as_qamc "cd '$QAMC_REPO' && git archive '$TARGET_SHA' | tar -x -C '$EXPORTDIR'" \
    || rollback_all "C2: could not export the deployed commit for testing"
  set +e
  GATEC_OUT="$(as_qamc "cd '$EXPORTDIR' && '$VENV_PY' -m pytest \
      tests/test_cash_sweep.py tests/test_tech_analyst.py tests/test_pipeline_stages.py \
      tests/test_invariants.py tests/test_intraday_scan.py tests/test_broker.py \
      tests/test_broker_market_data.py tests/test_agents_audit_round2.py \
      -q -p no:randomly" 2>&1)"
  GATEC_RC=$?
  set -e
  GATEC_SUMMARY="$(printf '%s\n' "$GATEC_OUT" | grep -E '[0-9]+ (passed|failed)' | tail -1)"
  if [[ "$GATEC_RC" -ne 0 ]]; then
    printf '%s\n' "$GATEC_OUT" | tail -40 | sed 's/^/         /' >&2
    rollback_all "C2: the PR #48 focused suites FAILED on the deployed commit under the production venv"
  fi
  GATEC_PASSED="$(sed -n 's/^\([0-9]\+\) passed.*/\1/p' <<< "$GATEC_SUMMARY")"
  [[ -n "$GATEC_PASSED" && "$GATEC_PASSED" -ge "$GATE_C_MIN_TESTS" ]] \
    || rollback_all "C2: only '${GATEC_PASSED:-0}' tests ran, expected at least $GATE_C_MIN_TESTS — collection is incomplete"
  rm -rf "$EXPORTDIR"
  ok "C2 focused PR #48 suites on the deployed commit, production venv: $GATEC_SUMMARY"
else
  note "C2 DEFERRED, with cause: pytest is not installed in the production venv, which is"
  note "     correct for a runtime-only account. The deterministic evidence is therefore"
  note "     inherited through C1: the identical tree hash proves this checkout is the"
  note "     artifact the reviewed 1925-test run (0 failed) executed against, including"
  note "     test_deployable_cash_never_uses_margin_buying_power_fields,"
  note "     test_fund_buys_reports_only_confirmed_proceeds,"
  note "     test_fund_buys_fails_closed_when_it_cannot_confirm, and"
  note "     test_symbols_unresolved_after_retry_are_explicit_none_not_absent."
fi

# ── C3. SGOV funding semantics, live and read-only ──────────────────────────
# Both reads are taken here, back to back, so the reconciliation below compares
# one consistent snapshot — reusing the older /account body from Gate B could
# fail spuriously if the live account moved between the two calls.
ACC_C3="$(curl -sS --max-time 20 "$API_BASE/account" 2>/dev/null || true)"
POS_C3="$(curl -sS --max-time 20 "$API_BASE/positions" 2>/dev/null || true)"
[[ -n "${ACC_C3//[[:space:]]/}" && -n "${POS_C3//[[:space:]]/}" ]] \
  || rollback_all "C3: /account or /positions returned nothing — Mission Control cannot be verified"
python3 - "$ACC_C3" "$POS_C3" <<'PY' || rollback_all "C3: live SGOV/liquidity verification failed"
import json, sys
acct = json.loads(sys.argv[1]); pos = json.loads(sys.argv[2])

assert acct.get("paper") is True, "account is not a paper account"
liq = acct.get("liquidity") or {}
for f in ("sweep_enabled", "sweep_symbol", "raw_cash", "sweep_parked_value",
          "reserve_usd", "total_liquidity"):
    assert f in liq and liq[f] is not None, "liquidity.%s missing or fabricated as null" % f
assert liq["sweep_enabled"] is True, "cash sweep reports disabled"
sym = liq["sweep_symbol"]

raw, parked, total = liq["raw_cash"], liq["sweep_parked_value"], liq["total_liquidity"]
assert abs((raw + parked) - total) < 0.01, \
    "liquidity does not reconcile: raw %.2f + parked %.2f != total %.2f" % (raw, parked, total)

positions = pos.get("positions") or []
assert pos.get("error") is None, "positions read reported an error: %r" % pos.get("error")
sweep_rows = [p for p in positions if p.get("symbol") == sym]
cash_equiv = [p for p in positions if p.get("is_cash_equivalent")]
# Every cash-equivalent row must be the sweep vehicle and must be labelled as
# such — SGOV must never present as ordinary risk capital.
for p in cash_equiv:
    assert p["symbol"] == sym, "%s is flagged cash-equivalent but is not the sweep vehicle" % p["symbol"]
    assert p.get("direction") == "cash_equivalent", \
        "%s is cash-equivalent but direction=%r" % (p["symbol"], p.get("direction"))
for p in sweep_rows:
    assert p.get("is_cash_equivalent") is True, "%s is held but not flagged cash-equivalent" % sym
    assert p.get("direction") == "cash_equivalent", "%s direction=%r" % (sym, p.get("direction"))
if sweep_rows:
    mv = sum(p.get("market_value") or 0.0 for p in sweep_rows)
    assert abs(mv - parked) < 0.01, \
        "sweep position market value %.2f != reported parked %.2f" % (mv, parked)

risk_rows = [p for p in positions if not p.get("is_cash_equivalent")]
print("   [ OK ] C3 %s parked $%.2f, flagged cash_equivalent; raw cash $%.2f; "
      "reserve $%.2f; %d non-sweep risk position(s); liquidity reconciles"
      % (sym, parked, raw, liq["reserve_usd"], len(risk_rows)))
PY

# ── C4. funding-path invariants on the deployed commit ──────────────────────
# The margin-field exclusion is proven by
# test_deployable_cash_never_uses_margin_buying_power_fields (inspect.getsource
# over the real function), which runs in C2 / is inherited via C1 — a raw grep
# here would false-positive on the docstrings that explain WHY those fields are
# never used. These canaries cover what a grep can prove unambiguously.
qgit "grep -qF 'return confirmed' ${TARGET_SHA} -- src/execution/cash_sweep.py" \
  || rollback_all "C4: fund_buys does not return the confirmed raw-cash rise"
qgit "grep -qF 'estimated_cost > available_cash' ${TARGET_SHA} -- src/pipeline_stages.py" \
  || rollback_all "C4: ExecutionStage's final raw-cash gate is missing"
qgit "grep -qF '_compute_deployable_cash' ${TARGET_SHA} -- src/execution/cash_sweep.py" \
  || rollback_all "C4: fund_buys does not refresh deployable cash from the broker"
ok "C4 confirmed-only funding + ExecutionStage raw-cash gate present in the deployed commit"

# ── C5. Tech batch-completeness invariants on the deployed commit ───────────
qgit "grep -qE 'analyses\.get\(sym\) for sym in submitted' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || rollback_all "C5: the every-submitted-symbol-is-a-key guarantee is missing"
qgit "grep -qE '_MAX_MISSING_RETRIES[[:space:]]*=[[:space:]]*1' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || rollback_all "C5: the bounded single retry is missing or not bounded at 1"
qgit "grep -qF 'a for a in analyses_map.values() if a is not None' ${TARGET_SHA} -- src/pipeline_stages.py" \
  || rollback_all "C5: the call site does not filter explicit None outcomes"
qgit "grep -qF 'data_status[\"tech\"] = \"partial\"' ${TARGET_SHA} -- src/pipeline_stages.py" \
  || rollback_all "C5: partial batch outcomes are not surfaced in data_status"
ok "C5 batch completeness: every symbol keyed, one bounded retry, partial/failed surfaced"
note "C5 the first LIVE batch log line (\"Batch: N/M symbols analyzed\") can only appear"
note "     after the next scheduled research run. Forcing a run to manufacture it is"
note "     prohibited by docs/WORK.md, so that observation is deferred to Gate E, read"
note "     from the journal/agent log after the next morning session."

# ── C6. observability truthfulness (read-only) ─────────────────────────────
curl -sS --max-time 20 "$API_BASE/runs?limit=1" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
runs = d.get("runs", d if isinstance(d, list) else [])
print("   [ OK ] C6 Mission Control run history readable (%d recent run(s) listed)" % len(runs))
' || rollback_all "C6: Mission Control run history is not readable"

[[ "$(health_field status)" == "ok" ]] || rollback_all "C6: Mission Control health degraded during Gate C"

say "GATE C PASSED — SGOV funding and Tech batch completeness verified on the deployed tree"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 7 — GATE D: enable intraday opportunity discovery"
# ═════════════════════════════════════════════════════════════════════════════
#
# Authorized by docs/WORK.md Stage D, reachable only because Gates A, B and C
# passed above in that order. This edits exactly one line of the deployed
# config/settings.yaml, in place, scoped to the intraday_scan block. It adds NO
# timer, service, daemon or scheduler: the scanner runs inside the existing
# intra_check cadence verified in PHASE 1f.

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
  || rollback_all "GATE D: failed to enable intraday_scan in the deployed config"
ok "config/settings.yaml intraday_scan: $EDIT_RESULT"

NUMSTAT="$(qgit 'diff --numstat -- config/settings.yaml' | tr -s '[:space:]' ' ' | sed 's/ $//')"
[[ "$NUMSTAT" == "1 1 config/settings.yaml" ]] \
  || rollback_all "GATE D: the config edit produced an unexpected diff ('$NUMSTAT')"
STATUS_LINES="$(qgit 'status --porcelain')"
[[ "$STATUS_LINES" == " M config/settings.yaml" ]] \
  || rollback_all "GATE D: unexpected working-tree changes ('$STATUS_LINES')"
ok "exactly one line changed, in exactly one file — the single expected config delta"
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
print('sweep_enabled=%s' % c.cash_sweep.enabled)
PY
" 2>&1)" || rollback_all "GATE D: the runtime configuration failed to load after enabling intraday"
printf '%s\n' "$SMOKE2" | sed 's/^/         /'
grep -q '^intraday_enabled=True$' <<< "$SMOKE2" || rollback_all "GATE D: the runtime config still reports intraday disabled"
grep -q '^paper=True$'            <<< "$SMOKE2" || rollback_all "GATE D: the runtime config is no longer paper-only"
grep -q '^sweep_enabled=True$'    <<< "$SMOKE2" || rollback_all "GATE D: the cash sweep was disabled by the edit"
ok "RUNTIME configuration reports intraday_scan.enabled = True, paper = True, sweep = True"

# Nothing about the scheduled surface may have changed to enable this.
TIMER_UNITS_RAW3="$(sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null || true)"
TIMERS_FINAL="$(printf '%s\n' "$TIMER_UNITS_RAW3" | awk 'NF {print $1, $2}' | sort)"
[[ "$TIMERS_FINAL" == "$TIMERS_BEFORE" ]] \
  || rollback_all "GATE D: the timer set changed while enabling intraday — no new schedule is authorized"
ok "no timer, service or daemon added — the scanner runs on the existing $INTRA_TIMER cadence"

[[ "$(health_field status)" == "ok" ]] || rollback_all "GATE D: Mission Control health degraded after the config edit"
ok "Mission Control still healthy"

say "GATE D PASSED — intraday opportunity discovery ENABLED on the existing cadence"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 8 — FINAL STATE"
# ═════════════════════════════════════════════════════════════════════════════

FINAL_SHA="$(qgit 'rev-parse HEAD')"
cat <<EOF

  Production SHA        : $FINAL_SHA
  Was                   : $BASELINE_SHA
  Config delta          : config/settings.yaml — intraday_scan.enabled false -> true
                          (the ONE expected, recorded production-vs-commit delta)
  Alpaca                : Paper only (asserted from the loaded runtime config)
  Gate order            : A (preflight) -> B (health) -> C (PR #48 behaviour) -> D (enable)
  Intraday scan         : ENABLED — $INTRA_SERVICE on $INTRA_TIMER, no new timer
  Telegram              : healthy (getMe 200); real token only in OneCLI; no message sent
  Providers             : OpenRouter / Alpaca trading / Alpaca market data / FRED
                          credential injection proven through the gateway, no paid call
  Model routing         : per-seat policy verified from the loaded configuration
  Mission Control       : restarted on the deployed SHA and healthy
  Timers                : $ENABLED_COUNT quant-agent timers, unchanged, all active, none failed
  Trading runs          : none invoked by this script; no order was placed

  Next scheduled intra_check ticks fire every 30 min inside 09:30-16:00 ET on
  the next weekday. The scanner is bounded: >=3.0% move since last close,
  3.0h per-symbol cooldown, max 5 candidates per tick, and it backs off when
  another session is mid-flight or another scan holds the process lock.

  DEFERRED TO GATE E (explicitly, not silently): the first live Tech batch log
  line and the first live intraday tick can only be observed after the next
  scheduled session. Forcing a run to manufacture them is prohibited.

  ROLLBACK (code + config + process, converged):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout -- config/settings.yaml && git checkout --detach $BASELINE_SHA" \\
        && sudo -u $QAMC_USER -H bash -c "$SYSTEMD_ENV systemctl --user restart $API_UNIT" \\
        && sleep 5 && curl -s $API_HEALTH

  DISABLE INTRADAY ONLY (keep the deployed code):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout -- config/settings.yaml"

  The checkout is intentionally DETACHED at a pinned SHA. Do not "tidy" it with
  \`git checkout main\` — that would follow a moving branch into production.

EOF
say "DONE — finish-line rollout complete"
