#!/usr/bin/env bash
#
# QAMC finish-line paper rollout — run ONCE, as root, from a root-owned 0700 copy.
#
# DO NOT copy this file by hand. Use the provenance-verified operator command in
# ops/review/README-finish-line-rollout.md, which extracts this script from the
# reviewed git blob, installs it root-owned 0700, verifies its sha256 against the
# reviewed hash, and only then executes it.
#
# WHAT IT DOES — converges production from the pinned Telegram hotfix to the
# pinned, externally accepted PR #48 target, enables the already-authorized
# intraday opportunity scanner, and runs the governed Stage E acceptance pass —
# in one run, in the docs/WORK.md order, with every stage gate an explicit
# fail-closed checkpoint. A later gate is unreachable unless the earlier ones
# passed on the deployed tree.
#
#   PHASE 1  PREFLIGHT (Gate A runtime half) — verify everything, change nothing.
#   PHASE 2  FETCH + VERIFY the target commit's CONTENT before checkout.
#   PHASE 3  DEPLOY the pinned SHA + import/config smoke.        [MUTATION]
#   PHASE 4  RESTART Mission Control onto the deployed SHA.      [MUTATION]
#   PHASE 5  GATE B — health, providers, live preflight, Telegram, timers.
#   PHASE 6  GATE C — SGOV funding + Tech batch proven on the DEPLOYED tree.
#   PHASE 7  GATE D — intraday market-data smoke, then enable.   [MUTATION]
#   PHASE 8  GATE E — adversarial end-to-end acceptance.
#   PHASE 9  FINISH LINE.
#
# FAIL-CLOSED AND SELF-CONVERGING. A deployment-state machine tracks how far the
# mutation has progressed. Any failure, unexpected non-zero exit, or INT/TERM/HUP
# after PHASE 3 converges production back to the baseline — checkout, config AND
# the Mission Control process — and says explicitly whether convergence
# succeeded. Convergence is recursion-safe and idempotent.
#
# The one abort it cannot handle is SIGKILL, which no process can trap. If that
# happens, the transcript below shows the last completed step, and re-running is
# refused with the exact convergence commands rather than stacking a second
# rollout on top of a half-finished one.
#
# WHAT IT DOES NOT DO — it never places, cancels or modifies an order, never
# invokes a trading mode, never runs main.py, never creates a service, timer,
# daemon, database or proxy, never installs a package, never changes firewall,
# network or account configuration, never modifies OneCLI, never reads, writes,
# prints or moves a secret value, and never sends a Telegram message.
#
set -Eeuo pipefail

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
API_BASE="http://127.0.0.1:8800"
API_HEALTH="${API_BASE}/health"
ONECLI_API="http://127.0.0.1:10254/api"
TG_PLACEHOLDER="ONECLI-INJECTS-THIS-PLACEHOLDER"
REQUIRED_TIMERS=7            # asserted from live unit files, never assumed
GATE_C_EXPECTED_TESTS=246    # exact; anything else is under- or over-collection
PRIVATE_PORTS="8800 10254 10255 10256"   # must never bind 0.0.0.0 / [::]

# ── Output helpers (never used to print secret material) ─────────────────────
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   [ OK ] %s\n' "$*"; }
note() { printf '   [NOTE] %s\n' "$*"; }
err()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

[[ "$(id -u)" -eq 0 ]] || { err "run this as root"; exit 1; }

# ── Self-integrity: root-owned, exactly 0700, in a root-owned safe directory ──
SELF="$(readlink -f "$0")"
read -r SELF_OWNER SELF_GROUP SELF_MODE <<< "$(stat -c '%U %G %a' "$SELF")"
[[ "$SELF_OWNER" == "root" && "$SELF_GROUP" == "root" ]] \
  || { err "this script is owned by $SELF_OWNER:$SELF_GROUP, not root:root. Refusing.
       Use the provenance-verified operator command in
       ops/review/README-finish-line-rollout.md — root must never execute a file
       an unprivileged account can rewrite between review and run."; exit 1; }
# Exactly 0700. Not "no group/write bits" — exactly the intended mode, so a
# readable-or-executable-by-others copy is refused too.
[[ "$SELF_MODE" == "700" ]] \
  || { err "this script's mode is $SELF_MODE, require exactly 700. Refusing.
       Re-install with: install -o root -g root -m 0700 ..."; exit 1; }
SELF_DIR="$(dirname "$SELF")"
read -r DIR_OWNER DIR_MODE <<< "$(stat -c '%U %a' "$SELF_DIR")"
[[ "$DIR_OWNER" == "root" ]] \
  || { err "$SELF_DIR is owned by $DIR_OWNER, not root — a 0700 file in a directory
       someone else owns can be replaced wholesale. Refusing."; exit 1; }
[[ "${DIR_MODE: -2:1}" =~ ^[0-5]$ && "${DIR_MODE: -1:1}" =~ ^[0-5]$ ]] \
  || { err "$SELF_DIR is group/world writable (mode $DIR_MODE) — this script could be
       replaced between verification and execution. Refusing."; exit 1; }

# ── Self-logging ─────────────────────────────────────────────────────────────
# The complete transcript is written to a root-only file as it happens, so an
# SSH disconnect cannot lose the evidence of a run that mutated production.
LOG="${QAMC_ROLLOUT_LOG:-/root/qamc-rollout-$(date -u +%Y%m%dT%H%M%SZ).log}"
: > "$LOG"; chmod 0600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
# A dead terminal must not kill this process part-way through a mutation.
trap '' PIPE

as_qamc() { sudo -u "$QAMC_USER" -H bash -c "$1"; }
qgit()    { as_qamc "cd '$QAMC_REPO' && git $1"; }

QAMC_UID="$(id -u "$QAMC_USER")"
SYSTEMD_ENV="export XDG_RUNTIME_DIR=/run/user/${QAMC_UID}; export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${QAMC_UID}/bus;"
sysctl_user() { as_qamc "$SYSTEMD_ENV systemctl --user $1"; }

TMPD="$(mktemp -d /tmp/qamc-rollout.XXXXXX)"
# 0711: root-owned, traversable (not listable) by qamc so the Gate C export
# subdir below — which qamc owns at 0700 — is reachable by that account.
chmod 0711 "$TMPD"

health_json() { curl -sS --max-time 15 "$API_HEALTH" 2>/dev/null || true; }
health_field() {
  health_json | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1]))
except Exception:
    print("")
' "$1" 2>/dev/null || true
}
wait_healthy() {
  local deadline=$(( SECONDS + ${1:-60} ))
  while (( SECONDS < deadline )); do
    [[ "$(health_field status)" == "ok" ]] && return 0
    sleep 2
  done
  return 1
}

# ═════════════════════════════════════════════════════════════════════════════
# Deployment-state machine + convergent rollback
# ═════════════════════════════════════════════════════════════════════════════
#
# DEPLOY_STATE records how far the mutation has progressed, so an abort knows
# what has to be undone:
#
#   pristine   nothing has been changed — an abort needs no convergence
#   deployed   the checkout is at TARGET_SHA; the API still runs baseline code
#   restarted  the API has been restarted onto TARGET_SHA
#   enabled    config/settings.yaml carries the intraday delta
#
# Convergence is the same operation in all three mutated states — restore
# config, detach to BASELINE_SHA, restart the API, prove health — so there is
# one code path, and it is safe to run when only part of it is strictly needed.
DEPLOY_STATE="pristine"
CONVERGED=""          # "", "yes" or "no" once convergence has been attempted
IN_CONVERGE=0         # recursion guard

converge() {  # $1 = why. Idempotent, recursion-safe, never raises.
  # Never converge from inside a subshell. Empirically bash does not fire the
  # ERR trap inside the `$( ... ) || handler` substitutions used throughout
  # this script, but if a future edit ever introduced an unguarded one, a
  # subshell rolling production back while the parent carried on believing all
  # was well would be the worst possible failure. One comparison closes the
  # class outright.
  [[ "$BASHPID" == "$$" ]] || return 0
  if (( IN_CONVERGE )); then return 0; fi
  # Already converged successfully: do not roll back a second time. A FAILED
  # convergence (CONVERGED="no") is deliberately still retryable, because a
  # second attempt may succeed where the first did not.
  [[ "$CONVERGED" != "yes" ]] || return 0
  IN_CONVERGE=1
  # Inside convergence, nothing may re-enter a trap: a failing command here
  # must be reported, not turned into another abort.
  trap - ERR INT TERM HUP
  set +e

  err ""
  err "[ROLLBACK] $1"
  err "[ROLLBACK] deployment state at abort: $DEPLOY_STATE"

  if [[ "$DEPLOY_STATE" == "pristine" ]]; then
    err "[ROLLBACK] nothing was changed — production is untouched at $BASELINE_SHA"
    CONVERGED="yes"
    IN_CONVERGE=0
    return 0
  fi

  local converged=1 head_after dirty_after pid_after

  qgit "checkout -- config/settings.yaml" >/dev/null 2>&1
  if qgit "checkout --detach '$BASELINE_SHA'" >/dev/null 2>&1; then
    head_after="$(qgit 'rev-parse HEAD' 2>/dev/null)"
    dirty_after="$(qgit 'status --porcelain' 2>/dev/null)"
    if [[ "$head_after" == "$BASELINE_SHA" && -z "$dirty_after" ]]; then
      err "[ROLLBACK] checkout + config restored to $BASELINE_SHA (tree clean)"
    else
      converged=0
      err "[ROLLBACK] tree NOT clean — HEAD=${head_after:-unknown} status=${dirty_after:-clean}"
    fi
  else
    converged=0
    err "[ROLLBACK] git rollback FAILED"
  fi

  # Converge the PROCESS, not just the files: Mission Control caches its
  # configuration at startup and holds imported modules in memory, so a
  # file-only rollback would leave the API executing target code.
  if sysctl_user "restart $API_UNIT" >/dev/null 2>&1 && wait_healthy 90; then
    pid_after="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]')"
    err "[ROLLBACK] $API_UNIT restarted on baseline code and healthy (pid ${pid_after:-?})"
  else
    converged=0
    err "[ROLLBACK] $API_UNIT did NOT come back healthy"
  fi

  if (( converged )); then
    CONVERGED="yes"
    err "[ROLLBACK] PRODUCTION CONVERGED: checkout $BASELINE_SHA, clean tree, API healthy, intraday NOT enabled"
  else
    CONVERGED="no"
    err "[ROLLBACK] CONVERGENCE INCOMPLETE — FINISH BY HAND:"
    err "  sudo -u $QAMC_USER -H bash -c \"cd $QAMC_REPO && git checkout -- config/settings.yaml && git checkout --detach $BASELINE_SHA\""
    err "  sudo -u $QAMC_USER -H bash -c \"$SYSTEMD_ENV systemctl --user restart $API_UNIT\""
    err "  curl -s $API_HEALTH"
  fi
  IN_CONVERGE=0
  return 0
}

# Hard stop BEFORE any mutation: nothing to converge, just report.
die() { err ""; err "[FAIL] $*"; err ""; exit 1; }

# Hard stop AFTER a mutation: converge first, then report.
abort() { converge "$*"; err ""; err "[FAIL] $*"; err ""; exit 1; }

on_err() {
  local rc=$?
  converge "unexpected non-zero exit (status $rc) at line ${BASH_LINENO[0]}"
  exit "$rc"
}
on_signal() {
  local sig="$1"
  converge "received SIG${sig} — aborting mid-run"
  exit $(( 128 + $2 ))
}
on_exit() {
  local rc=$?
  # Same reasoning as converge(): a subshell must never tear down the shared
  # temp directory or claim the run finished.
  [[ "$BASHPID" == "$$" ]] || return 0
  # Belt and braces: any exit path that mutated production but never ran
  # convergence gets it here.
  if [[ "$DEPLOY_STATE" != "pristine" && -z "$CONVERGED" && "$rc" -ne 0 ]]; then
    converge "script exited (status $rc) without converging"
  fi
  [[ -n "${TMPD:-}" && -d "$TMPD" ]] && rm -rf "$TMPD"
  printf '\n   full transcript: %s\n' "$LOG"
  sleep 0.3   # let the tee child flush before the shell goes away
  return 0
}
trap on_err ERR
trap 'on_signal INT 2'  INT
trap 'on_signal TERM 15' TERM
trap 'on_signal HUP 1'  HUP
trap on_exit EXIT

printf '\n\033[1mQAMC finish-line rollout\033[0m  %s UTC\n' "$(date -u '+%Y-%m-%d %H:%M:%S')"
printf 'transcript: %s\n' "$LOG"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 1 — PREFLIGHT / Gate A runtime half (nothing is changed in this phase)"
# ═════════════════════════════════════════════════════════════════════════════

id "$QAMC_USER" >/dev/null 2>&1 || die "user '$QAMC_USER' does not exist."
[[ -d "$QAMC_REPO/.git" ]] || die "$QAMC_REPO is not a git checkout."

# ── 1a. code position ────────────────────────────────────────────────────────
HEAD_NOW="$(qgit 'rev-parse HEAD')"
if [[ "$HEAD_NOW" == "$TARGET_SHA" ]]; then
  die "production HEAD is ALREADY $TARGET_SHA.
       A previous run deployed but did not complete — most likely it was
       SIGKILLed (which no script can trap) or the host went away. Do not
       re-run this script on top of a half-finished rollout.

       Converge back to the baseline first, then re-run:
         sudo -u $QAMC_USER -H bash -c \"cd $QAMC_REPO && git checkout -- config/settings.yaml && git checkout --detach $BASELINE_SHA\"
         sudo -u $QAMC_USER -H bash -c \"$SYSTEMD_ENV systemctl --user restart $API_UNIT\"
         curl -s $API_HEALTH

       The transcript of the earlier run is in /root/qamc-rollout-*.log."
fi
[[ "$HEAD_NOW" == "$BASELINE_SHA" ]] || die "production HEAD is $HEAD_NOW, expected the
       reviewed baseline $BASELINE_SHA. Production is neither where this rollout was
       reviewed against nor at the target. STOP and reconcile by hand before
       deploying anything."
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
       TELEGRAM_BOT_TOKEN. The real bot token must live ONLY in OneCLI. STOP."
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
       $QAMC_USER systemd --user unit for the restart/rollback path to work. STOP."
ok "$API_UNIT running (pid $API_PID_BEFORE)"

# ── 1e. scheduled surface (fail-closed) ──────────────────────────────────────
timer_snapshot() {   # "<unit> <state>" lines, sorted
  sysctl_user "list-unit-files 'quant-agent-*.timer' --no-legend" 2>/dev/null \
    | awk 'NF {print $1, $2}' | sort
}
unit_snapshot() {    # every quant-agent unit file, sorted — catches a NEW unit
  sysctl_user "list-unit-files 'quant-agent-*' --no-legend" 2>/dev/null \
    | awk 'NF {print $1, $2}' | sort
}
assert_timers_healthy() {  # $1 = context label; uses ENABLED_TIMERS
  local t state failed
  while read -r t; do
    [[ -n "$t" ]] || continue
    state="$(sysctl_user "is-active $t" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$state" == "active" ]] || { echo "$1: timer $t is '$state', expected 'active'"; return 1; }
  done <<< "$ENABLED_TIMERS"
  failed="$(sysctl_user "list-units --failed --no-legend 'quant-agent-*'" 2>/dev/null || true)"
  [[ -z "${failed//[[:space:]]/}" ]] || { echo "$1: quant-agent unit(s) FAILED: $failed"; return 1; }
  return 0
}

TIMERS_BEFORE="$(timer_snapshot)"
UNITS_BEFORE="$(unit_snapshot)"
[[ -n "${TIMERS_BEFORE//[[:space:]]/}" ]] || die "no quant-agent-*.timer unit files are visible
       to $QAMC_USER — the scheduled paper-trading surface cannot be verified. STOP."
ENABLED_TIMERS="$(printf '%s\n' "$TIMERS_BEFORE" | awk '$2 ~ /^enabled/ {print $1}')"
ENABLED_COUNT="$(printf '%s\n' "$ENABLED_TIMERS" | grep -c . || true)"
[[ "$ENABLED_COUNT" == "$REQUIRED_TIMERS" ]] || die "found $ENABLED_COUNT enabled quant-agent
       timer(s), require exactly $REQUIRED_TIMERS. Current unit files:
$(printf '%s\n' "$TIMERS_BEFORE" | sed 's/^/         /')
       The scheduled paper-trading surface is not the reviewed one. STOP."
TIMER_ERR="$(assert_timers_healthy 'preflight')" || die "$TIMER_ERR. STOP."
ok "$ENABLED_COUNT enabled quant-agent timers, all active, none failed (verified)"
printf '%s\n' "$ENABLED_TIMERS" | sed 's/^/          /'

# ── 1f. wrappers + intra_check cadence (fail-closed) ────────────────────────
check_wrappers() {
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
INTRA_SERVICE="$(printf '%s\n' "$UNITS_CAT" | awk '
  /^# \// { unit = $2; sub(/.*\//, "", unit); next }
  /intra_check/ { if (unit != "") { print unit; exit } }
')"
[[ -n "$INTRA_SERVICE" ]] || die "no quant-agent service unit invokes the intra_check mode —
       the intraday scanner this rollout enables would never be reached. STOP."
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
  ok "production venv python present, pytest available (Gate C re-runs the focused suites)"
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
# `fetch origin main` — still safe, because nothing below reads a branch: the
# checkout is by exact SHA and the commit's TREE HASH is asserted first.

qgit "fetch --no-tags origin '+${TARGET_SHA}:refs/qamc/finish-line-target'" >/dev/null 2>&1 \
  || qgit "fetch --no-tags origin main" >/dev/null 2>&1 \
  || die "could not fetch the target commit from origin. STOP."
qgit "cat-file -e ${TARGET_SHA}^{commit}" || die "target commit $TARGET_SHA is not present after fetch. STOP."
ok "target commit $TARGET_SHA fetched"

FETCHED_TREE="$(qgit "rev-parse ${TARGET_SHA}^{tree}")"
[[ "$FETCHED_TREE" == "$TARGET_TREE" ]] \
  || die "target tree is $FETCHED_TREE, expected $TARGET_TREE. The commit's CONTENT is
       not what was reviewed. STOP."
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
  || die "cash-sweep confirmed-funding fix absent from the target commit. STOP."
qgit "grep -qF '_MAX_MISSING_RETRIES' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || die "Tech batch-completeness fix absent from the target commit. STOP."
qgit "grep -qF 'self._redact(exc)' ${TARGET_SHA} -- src/notifier.py" \
  || die "Telegram token redaction absent from the target commit. STOP."
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
say "PHASE 3 — deploy exact $TARGET_SHA (detached; main is never followed)  [MUTATION]"
# ═════════════════════════════════════════════════════════════════════════════
#
# The checkout is the first mutation. It is performed with the state machine
# already armed: if it fails part-way (a signal mid-checkout, a half-written
# index), DEPLOY_STATE is set BEFORE the command runs, so any abort path
# converges the tree back to the baseline rather than leaving it mid-transition.
DEPLOY_STATE="deployed"
qgit "checkout --detach '$TARGET_SHA'" >/dev/null 2>&1 \
  || abort "checkout of $TARGET_SHA failed — production may have been left mid-transition"

NEW_SHA="$(qgit 'rev-parse HEAD')"
[[ "$NEW_SHA" == "$TARGET_SHA" ]] || abort "post-checkout HEAD is $NEW_SHA, expected $TARGET_SHA"
[[ -z "$(qgit 'status --porcelain')" ]] || abort "working tree is dirty right after checkout"
DEPLOYED_TREE="$(qgit 'rev-parse HEAD^{tree}')"
[[ "$DEPLOYED_TREE" == "$TARGET_TREE" ]] || abort "deployed tree is $DEPLOYED_TREE, expected $TARGET_TREE"
ok "production HEAD == $TARGET_SHA, tree == $TARGET_TREE, working tree clean"

ENV_SHA_AFTER="$(sha256sum "$QAMC_ENV" | cut -d' ' -f1)"
[[ "$ENV_SHA_AFTER" == "$ENV_SHA_BEFORE" ]] \
  || abort "the runtime env file changed during checkout — a deploy must never touch it"
ok "runtime env file byte-identical (deploy touched no secret material)"

REPO_OWNER_AFTER="$(stat -c '%U:%G' "$QAMC_REPO/main.py")"
[[ "$REPO_OWNER_AFTER" == "${QAMC_USER}:${QAMC_USER}" ]] || abort "checkout ownership became $REPO_OWNER_AFTER"
ok "checkout still owned by ${QAMC_USER}:${QAMC_USER}"

WRAP_ERR="$(check_wrappers)" || abort "post-deploy wrapper check failed: $WRAP_ERR"
ok "wrappers in the deployed tree still source the runtime env; intra_check case present"

# ── import + config smoke under the PRODUCTION venv, before any restart ──────
say "PHASE 3b — production-venv import/config smoke (no trading action)"
SMOKE="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' - <<'PY'
import importlib
for m in ('src.config', 'src.pipeline', 'src.pipeline_stages', 'src.execution.cash_sweep',
          'src.execution.broker', 'src.agents.tech_analyst', 'src.notifier', 'src.api'):
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
" 2>&1)" || { printf '%s\n' "$SMOKE" | sed 's/^/         /' >&2
              abort "the deployed code does not import/load under the production venv"; }
printf '%s\n' "$SMOKE" | sed 's/^/         /'
grep -q '^paper=True$'                   <<< "$SMOKE" || abort "loaded config is not paper-only"
grep -q '^intraday_enabled=False$'       <<< "$SMOKE" || abort "intraday is not disabled at cutover"
grep -q '^sweep_enabled=True$'           <<< "$SMOKE" || abort "cash sweep is not enabled in the deployed config"
grep -q '^sweep_symbol=SGOV$'            <<< "$SMOKE" || abort "sweep symbol is not SGOV"
grep -q '^intraday_threshold=3.0$'       <<< "$SMOKE" || abort "intraday move threshold is not the accepted 3.0%"
grep -q '^intraday_cooldown=3.0$'        <<< "$SMOKE" || abort "intraday cooldown is not the accepted 3.0h"
grep -q '^intraday_cap=5$'               <<< "$SMOKE" || abort "intraday candidate cap is not the accepted 5"
grep -q '^inverse_etfs=SH,SDS,PSQ,SQQQ$' <<< "$SMOKE" \
  || abort "the approved inverse ETFs (SH, SDS, PSQ, SQQQ) are not all in the trading universe — bearish expression would be unavailable"
ok "deployed code imports cleanly; paper-only, SGOV sweep on, accepted intraday parameters, all four inverse ETFs present, intraday OFF"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 4 — restart Mission Control onto the deployed code  [MUTATION]"
# ═════════════════════════════════════════════════════════════════════════════
DEPLOY_STATE="restarted"
sysctl_user "restart $API_UNIT" || abort "systemctl restart $API_UNIT failed"
wait_healthy 90 || abort "Mission Control did not return healthy within 90s of restarting on the target code"
API_PID_AFTER="$(sysctl_user "show -p MainPID --value $API_UNIT" 2>/dev/null | tr -d '[:space:]' || true)"
ok "$API_UNIT restarted (pid $API_PID_BEFORE -> ${API_PID_AFTER:-?}) and healthy"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 5 — GATE B: production healthy on the target, intraday still OFF"
# ═════════════════════════════════════════════════════════════════════════════

for f in db_reachable broker_reachable paper; do
  [[ "$(health_field "$f")" == "True" ]] || abort "post-deploy /health.$f is not true"
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
' || abort "post-deploy /account read failed or is not a paper account"

# ── B1. free commissioning groups ───────────────────────────────────────────
#
# docs/WORK.md Gate B requires OpenRouter/model routing, provider, database and
# FRED wiring to be verified. This uses the ACCEPTED commissioning verifier
# rather than ad-hoc probes:
#
#   config          pins the per-seat model-routing policy from the loaded
#                   configuration and re-asserts alpaca paper-only
#   gateway/wiring  OneCLI up, loopback-bound, runtime proxy/CA resolves
#   providers       credential INJECTION end to end through the real gateway
#                   for OpenRouter, Alpaca trading, Alpaca market data and
#                   FRED, using each provider's free metadata endpoint
#   mission-control the API is up, private and read-only
#
# EXCLUDED here, with cause (not silent skips):
#   safety     its "trading timers disabled" assertion inverts by design once
#              timers are enabled, which they have been since the authorized
#              soak began 2026-08-14. Its "no secrets committed" half is
#              covered by the explicit canary below, and the timer surface is
#              verified far more strictly in PHASE 1e and again at Gates D/E.
#   isolation  the known off-account check, resolved by the already-green `dev`
#              commissioning run recorded in docs/STATE.md.
#   preflight  NOT excluded — it runs immediately below, with --live.
VERIFY_ARGS="--group config --group gateway --group wiring --group providers --group mission-control"
run_verifier() {  # $1 = args, $2 = stderr file
  as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' \
    ops/commissioning/verify_commissioning.py $1 --json" 2>"$2" || true
}
parse_verifier() {  # stdin = json; $1 = required groups csv; $2 = min providers
  python3 -c '
import json, sys
required = set(x for x in sys.argv[1].split(",") if x)
min_providers = int(sys.argv[2])
try:
    d = json.load(sys.stdin)
except Exception as exc:
    sys.exit("verifier produced no parsable JSON (%s)" % exc)
results = d.get("results") or []
if not results:
    sys.exit("verifier returned no results at all")
bad, seen, providers = [], set(), 0
for r in results:
    group, name, status = r.get("group"), r.get("name"), r.get("status")
    detail = (r.get("detail") or "").strip()
    seen.add(group)
    if group == "providers":
        providers += 1
    print("   [%4s] %s / %s" % (status, group, name))
    if detail:
        print("          " + detail.splitlines()[0][:150])
    if status != "PASS":
        bad.append("%s/%s is %s: %s" % (group, name, status,
                                        detail.splitlines()[0] if detail else ""))
missing = required - seen
if missing:
    sys.exit("group(s) produced no result: %s" % ", ".join(sorted(missing)))
if providers < min_providers:
    sys.exit("only %d provider probe(s) ran, expected at least %d" % (providers, min_providers))
if bad:
    sys.exit("non-PASS result(s): " + " | ".join(bad))
print("   [ OK ] %d checks, all PASS across %s" % (len(results), ", ".join(sorted(seen))))
' "$1" "$2"
}

VERIFY_JSON="$(run_verifier "$VERIFY_ARGS" "$TMPD/verify.err")"
[[ -n "${VERIFY_JSON//[[:space:]]/}" ]] || { [[ -s "$TMPD/verify.err" ]] && sed 's/^/         /' "$TMPD/verify.err" >&2
  abort "the commissioning verifier produced no output on the deployed code"; }
printf '%s' "$VERIFY_JSON" | parse_verifier "config,gateway,wiring,providers,mission-control" 4 \
  || { [[ -s "$TMPD/verify.err" ]] && sed 's/^/         /' "$TMPD/verify.err" >&2
       abort "commissioning verification failed on the deployed code (see output above)"; }
ok "B1 provider credential injection proven for OpenRouter, Alpaca trading, Alpaca market data and FRED"

# ── B2. accepted LIVE provider preflight, on the deployed code ──────────────
#
# The `providers` group above proves the gateway INJECTS a credential at the
# HTTP level. That is necessary but not sufficient: it would still pass with a
# retired model id, a token budget that yields no content, or a market-data
# host QAMC can reach but not parse. The accepted verifier's `preflight` group
# answers the operator's actual question by constructing the same openai,
# alpaca-py and fredapi clients the trading engine builds and completing one
# real read with each.
#
# This is the only paid step in the whole script — one completion per distinct
# accepted policy model, a trivial amount, explicitly authorized for finish-line
# acceptance. It is run AFTER deployment so it exercises the deployed code, and
# it matters here because this rollout's delta DOES touch src/execution/broker.py
# (it adds the read-only bulk `get_intraday_snapshots` method and one extra
# account field). Every call it makes is read-only; no order is ever submitted.
PREFLIGHT_JSON="$(run_verifier "--group preflight --live" "$TMPD/preflight.err")"
[[ -n "${PREFLIGHT_JSON//[[:space:]]/}" ]] || { [[ -s "$TMPD/preflight.err" ]] && sed 's/^/         /' "$TMPD/preflight.err" >&2
  abort "the commissioning live preflight produced no output"; }
printf '%s' "$PREFLIGHT_JSON" | parse_verifier "preflight" 0 \
  || { [[ -s "$TMPD/preflight.err" ]] && sed 's/^/         /' "$TMPD/preflight.err" >&2
       abort "the accepted LIVE provider preflight failed on the deployed code"; }
ok "B2 live preflight PASS through QAMC's own clients (OpenRouter completion, Alpaca, FRED)"

# ── B3. Telegram: prove the credential path. getMe sends NO message. ────────
GETME="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && \
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
  'https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe'" 2>/dev/null || true)"
[[ "$GETME" == "200" ]] || abort "Telegram getMe returned '${GETME:-no response}', expected 200
       (401 = token wrong/revoked, 404 = gateway did not substitute, 000 = egress blocked)"
ok "B3 Telegram healthy — OneCLI injected the real token (getMe 200; no message sent)"

TOKEN_LINES_AFTER="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN=' '$QAMC_ENV'" || true)"
[[ "$TOKEN_LINES_AFTER" == "1" ]] || abort "TELEGRAM_BOT_TOKEN line count changed to '${TOKEN_LINES_AFTER:-0}'"
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" \
  || abort "the runtime env no longer holds only the Telegram placeholder"
ok "runtime env still holds ONLY the placeholder; real token remains only in OneCLI"

if qgit "grep -nE '^[[:space:]]*(ALPACA|OPENROUTER|TELEGRAM|FRED)[A-Z_]*=[A-Za-z0-9_-]{16,}' ${TARGET_SHA}" 2>/dev/null; then
  abort "credential-looking material found in the deployed commit"
fi
ok "no credential-looking material committed in the deployed tree"

# ── B4. scheduled surface unchanged by the deploy ───────────────────────────
TIMERS_AFTER="$(timer_snapshot)"
[[ "$TIMERS_AFTER" == "$TIMERS_BEFORE" ]] || {
  printf '   BEFORE:\n%s\n   AFTER:\n%s\n' \
    "$(printf '%s\n' "$TIMERS_BEFORE" | sed 's/^/     /')" \
    "$(printf '%s\n' "$TIMERS_AFTER" | sed 's/^/     /')" >&2
  abort "the set of quant-agent timer units changed across the deploy"
}
TIMER_ERR="$(assert_timers_healthy 'gate B')" || abort "$TIMER_ERR"
ok "B4 $ENABLED_COUNT quant-agent timers unchanged, all still active, none failed"

grep -q '^intraday_enabled=False$' <<< "$SMOKE" || abort "intraday is not disabled at the end of Gate B"
ok "intraday_scan still DISABLED (enablement is Gate D, after Gate C)"

say "GATE B PASSED — production healthy on $TARGET_SHA, intraday still OFF"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 6 — GATE C: prove PR #48 behaviour on the DEPLOYED tree"
# ═════════════════════════════════════════════════════════════════════════════
#
# docs/WORK.md Stage C, run AFTER deployment and BEFORE enablement, from
# deterministic evidence plus safe read-only production verification. No trade
# is forced, no order is placed.

# ── C1. deterministic evidence transfer ─────────────────────────────────────
[[ "$DEPLOYED_TREE" == "$TARGET_TREE" ]] || abort "C1: deployed tree no longer matches the reviewed tree"
ok "C1 deployed tree == reviewed tree $TARGET_TREE (reviewed full suite: 1925 passed, 0 failed)"

# ── C2. re-run the focused PR #48 suites on the deployed commit ─────────────
# Exported with `git archive` to a throwaway directory, so the production
# checkout is never written to, and run WITHOUT the runtime env sourced, so no
# real credential is in scope for a test process. storage.db_path is relative,
# so the export is fully self-contained.
if [[ "$HAVE_PYTEST" -eq 1 ]]; then
  EXPORTDIR="$TMPD/gate-c-export"
  install -d -o "$QAMC_USER" -g "$QAMC_USER" -m 0700 "$EXPORTDIR"
  as_qamc "cd '$QAMC_REPO' && git archive '$TARGET_SHA' | tar -x -C '$EXPORTDIR'" \
    || abort "C2: could not export the deployed commit for testing"
  set +e
  GATEC_OUT="$(as_qamc "cd '$EXPORTDIR' && '$VENV_PY' -m pytest \
      tests/test_cash_sweep.py tests/test_tech_analyst.py tests/test_pipeline_stages.py \
      tests/test_invariants.py tests/test_intraday_scan.py tests/test_broker.py \
      tests/test_broker_market_data.py tests/test_agents_audit_round2.py \
      -q -p no:randomly" 2>&1)"
  GATEC_RC=$?
  set -e
  GATEC_SUMMARY="$(printf '%s\n' "$GATEC_OUT" | grep -E '[0-9]+ (passed|failed|error)' | tail -1)"
  if [[ "$GATEC_RC" -ne 0 ]]; then
    printf '%s\n' "$GATEC_OUT" | tail -40 | sed 's/^/         /' >&2
    abort "C2: the PR #48 focused suites FAILED on the deployed commit under the production venv"
  fi
  # Exactly the expected count, and no other outcome at all: a skip, xfail,
  # xpass, error or deselect means the deployed tree is not the tree that was
  # reviewed green, even if pytest exited 0.
  printf '%s\n' "$GATEC_SUMMARY" | python3 -c '
import re, sys
line = sys.stdin.read().strip()
expected = int(sys.argv[1])
counts = {k: int(v) for v, k in re.findall(
    r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings)", line)}
if counts.get("passed", 0) != expected:
    sys.exit("expected exactly %d passed, summary said %r" % (expected, line))
for bad in ("failed", "error", "errors", "skipped", "xfailed", "xpassed", "deselected"):
    if counts.get(bad):
        sys.exit("summary reports %d %s — expected none: %r" % (counts[bad], bad, line))
print("   [ OK ] C2 exactly %d passed, 0 failed / 0 error / 0 skipped / 0 xfailed" % expected)
' "$GATE_C_EXPECTED_TESTS" || abort "C2: focused-suite outcome is not exactly $GATE_C_EXPECTED_TESTS passed and nothing else"
  rm -rf "$EXPORTDIR"
  ok "C2 focused PR #48 suites on the deployed commit, production venv: $GATEC_SUMMARY"
else
  note "C2 DEFERRED, with cause: pytest is not installed in the production venv, which is"
  note "     correct for a runtime-only account. The deterministic evidence is inherited"
  note "     through C1 — the identical tree hash proves this checkout is the artifact the"
  note "     reviewed run executed against, including"
  note "     test_deployable_cash_never_uses_margin_buying_power_fields,"
  note "     test_fund_buys_reports_only_confirmed_proceeds,"
  note "     test_fund_buys_fails_closed_when_it_cannot_confirm and"
  note "     test_symbols_unresolved_after_retry_are_explicit_none_not_absent."
fi

# ── C3. SGOV funding semantics, live and read-only ──────────────────────────
# Both reads are taken back to back so the reconciliation compares one
# consistent snapshot.
ACC_C3="$(curl -sS --max-time 20 "$API_BASE/account" 2>/dev/null || true)"
POS_C3="$(curl -sS --max-time 20 "$API_BASE/positions" 2>/dev/null || true)"
[[ -n "${ACC_C3//[[:space:]]/}" && -n "${POS_C3//[[:space:]]/}" ]] \
  || abort "C3: /account or /positions returned nothing — Mission Control cannot be verified"
python3 - "$ACC_C3" "$POS_C3" <<'PY' || abort "C3: live SGOV/liquidity verification failed"
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

assert pos.get("error") is None, "positions read reported an error: %r" % pos.get("error")
positions = pos.get("positions") or []
sweep_rows = [p for p in positions if p.get("symbol") == sym]
cash_equiv = [p for p in positions if p.get("is_cash_equivalent")]

# Every cash-equivalent row must BE the sweep vehicle and be labelled as such:
# SGOV must never present as ordinary risk capital.
for p in cash_equiv:
    assert p["symbol"] == sym, "%s is flagged cash-equivalent but is not the sweep vehicle" % p["symbol"]
    assert p.get("direction") == "cash_equivalent", \
        "%s is cash-equivalent but direction=%r" % (p["symbol"], p.get("direction"))
for p in sweep_rows:
    assert p.get("is_cash_equivalent") is True, "%s is held but not flagged cash-equivalent" % sym
    assert p.get("direction") == "cash_equivalent", "%s direction=%r" % (sym, p.get("direction"))

# A positive parked value MUST be backed by a real sweep position row that
# reconciles exactly — "parked money with nothing holding it" is precisely the
# false-liquidity failure this gate exists to catch.
if parked > 0:
    assert sweep_rows, \
        "liquidity reports $%.2f parked in %s but no %s position row exists" % (parked, sym, sym)
    mv = sum(p.get("market_value") or 0.0 for p in sweep_rows)
    assert abs(mv - parked) < 0.01, \
        "%s position market value %.2f != reported parked %.2f" % (sym, mv, parked)
else:
    held = sum(p.get("market_value") or 0.0 for p in sweep_rows)
    assert held < 0.01, \
        "liquidity reports $0 parked but a %s position worth %.2f is held" % (sym, held)

risk_rows = [p for p in positions if not p.get("is_cash_equivalent")]
print("   [ OK ] C3 %s parked $%.2f (backed by %d position row(s), reconciles exactly); "
      "raw cash $%.2f; reserve $%.2f; %d non-sweep risk position(s)"
      % (sym, parked, len(sweep_rows), raw, liq["reserve_usd"], len(risk_rows)))
PY

# ── C4/C5. funding + batch invariants on the deployed commit ────────────────
# The margin-field exclusion is proven by
# test_deployable_cash_never_uses_margin_buying_power_fields (inspect.getsource
# over the real function), which runs in C2 / is inherited via C1 — a raw grep
# would false-positive on the docstrings explaining why those fields are unused.
qgit "grep -qF 'return confirmed' ${TARGET_SHA} -- src/execution/cash_sweep.py" \
  || abort "C4: fund_buys does not return the confirmed raw-cash rise"
qgit "grep -qF 'estimated_cost > available_cash' ${TARGET_SHA} -- src/pipeline_stages.py" \
  || abort "C4: ExecutionStage's final raw-cash gate is missing"
qgit "grep -qF '_compute_deployable_cash' ${TARGET_SHA} -- src/execution/cash_sweep.py" \
  || abort "C4: fund_buys does not refresh deployable cash from the broker"
ok "C4 confirmed-only funding + ExecutionStage raw-cash gate present in the deployed commit"

qgit "grep -qE 'analyses\.get\(sym\) for sym in submitted' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || abort "C5: the every-submitted-symbol-is-a-key guarantee is missing"
qgit "grep -qE '_MAX_MISSING_RETRIES[[:space:]]*=[[:space:]]*1' ${TARGET_SHA} -- src/agents/tech_analyst.py" \
  || abort "C5: the bounded single retry is missing or not bounded at 1"
qgit "grep -qF 'a for a in analyses_map.values() if a is not None' ${TARGET_SHA} -- src/pipeline_stages.py" \
  || abort "C5: the call site does not filter explicit None outcomes"
qgit "grep -qF 'data_status[\"tech\"] = \"partial\"' ${TARGET_SHA} -- src/pipeline_stages.py" \
  || abort "C5: partial batch outcomes are not surfaced in data_status"
ok "C5 batch completeness: every symbol keyed, one bounded retry, partial/failed surfaced"
note "C5 the first LIVE batch log line (\"Batch: N/M symbols analyzed\") can only appear after"
note "     the next scheduled research run. Forcing a run to manufacture it is prohibited by"
note "     docs/WORK.md, so that observation is post-rollout soak evidence, not a gate."

# ── C6. observability truthfulness (read-only) ─────────────────────────────
curl -sS --max-time 20 "$API_BASE/runs?limit=1" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
runs = d.get("runs", d if isinstance(d, list) else [])
print("   [ OK ] C6 Mission Control run history readable (%d recent run(s) listed)" % len(runs))
' || abort "C6: Mission Control run history is not readable"
[[ "$(health_field status)" == "ok" ]] || abort "C6: Mission Control health degraded during Gate C"

say "GATE C PASSED — SGOV funding and Tech batch completeness verified on the deployed tree"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 7 — GATE D: enable intraday opportunity discovery  [MUTATION]"
# ═════════════════════════════════════════════════════════════════════════════

# ── D0. the scanner's market-data path, live and read-only ─────────────────
# One bulk snapshot for a single symbol through the deployed AlpacaBroker,
# built exactly as TradingPipeline builds it. Read-only market data: it places
# no order, submits nothing, and touches no position. Enabling a scanner whose
# own data source cannot return usable pricing would be enabling a no-op at
# best and a blind trigger at worst.
SNAP="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' - <<'PY'
from src.config import load_config
from src.execution.broker import AlpacaBroker
c = load_config('config/settings.yaml')
b = AlpacaBroker(api_key=c.api_keys.alpaca_key, secret_key=c.api_keys.alpaca_secret,
                 paper=c.alpaca.paper)
snaps = b.get_intraday_snapshots(['SPY'])
s = snaps.get('SPY') or {}
last, prev = s.get('last_price'), s.get('prev_close')
print('symbols=%d' % len(snaps))
print('last_price=%s' % last)
print('prev_close=%s' % prev)
print('session_open=%s' % s.get('session_open'))
print('session_volume=%s' % s.get('session_volume'))
ok = (isinstance(last, (int, float)) and last > 0
      and isinstance(prev, (int, float)) and prev > 0)
print('usable=%s' % ok)
if ok:
    print('move_pct=%.3f' % ((last - prev) / prev * 100.0))
PY
" 2>&1)" || { printf '%s\n' "$SNAP" | sed 's/^/         /' >&2
              abort "D0: the intraday snapshot smoke crashed"; }
printf '%s\n' "$SNAP" | sed 's/^/         /'
grep -q '^usable=True$' <<< "$SNAP" \
  || abort "D0: get_intraday_snapshots(['SPY']) did not return usable previous/current pricing —
       the scanner's own market-data path is not working, so it must not be enabled"
ok "D0 intraday market-data path verified live and read-only (no order placed)"

# ── D1. enable, in place, scoped to the intraday_scan block ────────────────
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
DEPLOY_STATE="enabled"
EDIT_RESULT="$(sudo -u "$QAMC_USER" -H python3 -c "$EDITOR_PY" "$QAMC_REPO/config/settings.yaml")" \
  || abort "GATE D: failed to enable intraday_scan in the deployed config"
ok "config/settings.yaml intraday_scan: $EDIT_RESULT"

NUMSTAT="$(qgit 'diff --numstat -- config/settings.yaml' | tr -s '[:space:]' ' ' | sed 's/ $//')"
[[ "$NUMSTAT" == "1 1 config/settings.yaml" ]] || abort "GATE D: the config edit produced an unexpected diff ('$NUMSTAT')"
STATUS_LINES="$(qgit 'status --porcelain')"
[[ "$STATUS_LINES" == " M config/settings.yaml" ]] || abort "GATE D: unexpected working-tree changes ('$STATUS_LINES')"
ok "exactly one line changed, in exactly one file — the single expected config delta"
qgit 'diff -- config/settings.yaml' | sed 's/^/          /'

SMOKE2="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && '$VENV_PY' - <<'PY'
from src.config import load_config
c = load_config('config/settings.yaml')
print('paper=%s' % c.alpaca.paper)
print('intraday_enabled=%s' % c.intraday_scan.enabled)
print('intraday_threshold=%s' % c.intraday_scan.move_threshold_pct)
print('intraday_cooldown=%s' % c.intraday_scan.cooldown_hours)
print('intraday_cap=%s' % c.intraday_scan.max_candidates_per_scan)
print('sweep_enabled=%s' % c.cash_sweep.enabled)
print('sweep_symbol=%s' % c.cash_sweep.symbol)
print('inverse_etfs=%s' % ','.join(s for s in ('SH','SDS','PSQ','SQQQ') if s in c.trading.universe))
PY
" 2>&1)" || abort "GATE D: the runtime configuration failed to load after enabling intraday"
printf '%s\n' "$SMOKE2" | sed 's/^/         /'
grep -q '^intraday_enabled=True$'  <<< "$SMOKE2" || abort "GATE D: the runtime config still reports intraday disabled"
grep -q '^paper=True$'             <<< "$SMOKE2" || abort "GATE D: the runtime config is no longer paper-only"
grep -q '^sweep_enabled=True$'     <<< "$SMOKE2" || abort "GATE D: the cash sweep was disabled by the edit"
grep -q '^intraday_threshold=3.0$' <<< "$SMOKE2" || abort "GATE D: the move threshold changed"
grep -q '^intraday_cooldown=3.0$'  <<< "$SMOKE2" || abort "GATE D: the cooldown changed"
grep -q '^intraday_cap=5$'         <<< "$SMOKE2" || abort "GATE D: the candidate cap changed"
grep -q '^inverse_etfs=SH,SDS,PSQ,SQQQ$' <<< "$SMOKE2" \
  || abort "GATE D: the approved inverse ETFs are no longer all in the live universe — bearish expression would be unavailable"
ok "RUNTIME configuration: intraday enabled, paper-only, sweep on, accepted 3.0% / 3.0h / 5 parameters, all four inverse ETFs present"

TIMERS_D="$(timer_snapshot)"
[[ "$TIMERS_D" == "$TIMERS_BEFORE" ]] || abort "GATE D: the timer set changed while enabling intraday"
TIMER_ERR="$(assert_timers_healthy 'gate D')" || abort "$TIMER_ERR"
ok "GATE D $ENABLED_COUNT timers unchanged, all active, none failed — no new schedule introduced"
[[ "$(health_field status)" == "ok" ]] || abort "GATE D: Mission Control health degraded after the config edit"
ok "Mission Control still healthy"

say "GATE D PASSED — intraday opportunity discovery ENABLED on the existing cadence"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 8 — GATE E: adversarial end-to-end acceptance"
# ═════════════════════════════════════════════════════════════════════════════
#
# docs/WORK.md Stage E, run in the same session so the finish line is reached
# rather than assumed. Everything here is read-only. No trade is forced, and no
# check waits for a market opportunity: the completion condition is verified
# wiring and operational readiness.

# E1 — exact deployed SHA / tree, and only the expected config delta
E_SHA="$(qgit 'rev-parse HEAD')"
E_TREE="$(qgit 'rev-parse HEAD^{tree}')"
[[ "$E_SHA" == "$TARGET_SHA" && "$E_TREE" == "$TARGET_TREE" ]] || abort "E1: deployed SHA/tree drifted"
[[ "$(qgit 'status --porcelain')" == " M config/settings.yaml" ]] || abort "E1: production has changes beyond the one expected config delta"
[[ "$(qgit 'diff --numstat -- config/settings.yaml' | tr -s '[:space:]' ' ' | sed 's/ $//')" == "1 1 config/settings.yaml" ]] \
  || abort "E1: the config delta is not exactly one line"
ok "E1 deployed $E_SHA (tree $E_TREE); exactly one intraday config line differs; nothing else"

# E2 — Alpaca Paper only, everywhere it is asserted
grep -q '^paper=True$' <<< "$SMOKE2" || abort "E2: runtime config is not paper-only"
[[ "$(health_field paper)" == "True" ]] || abort "E2: /health.paper is not true"
if qgit "grep -nE '^[[:space:]]*paper:[[:space:]]*false' HEAD -- config/settings.yaml" 2>/dev/null; then
  abort "E2: paper: false present in the deployed config"
fi
ok "E2 Alpaca Paper only — runtime config, /health and deployed config all agree"

# E3 — no live-trading, margin, options or direct-shorting path in the deployed commit
for pat in 'sell_short' 'SELL_SHORT' 'OptionLegRequest' 'OptionsOrderRequest' 'enable_margin'; do
  if qgit "grep -qF '$pat' HEAD -- src" 2>/dev/null; then
    abort "E3: '$pat' appears in the deployed source — an unauthorized trading path"
  fi
done
ok "E3 no direct-shorting, options or margin path in the deployed source"

# E4 — OneCLI healthy and private; nothing QAMC-facing bound to a public address
[[ "$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$ONECLI_API/agents" || true)" == "200" ]] \
  || abort "E4: OneCLI gateway is not healthy"
# `ss` output is required evidence, not optional: if it is missing or does not
# even show the Mission Control port listening, this check has proven nothing
# and must fail rather than pass vacuously.
SS_OUT="$(ss -ltn 2>/dev/null || true)"
[[ -n "${SS_OUT//[[:space:]]/}" ]] || abort "E4: 'ss -ltn' produced no output — public-exposure cannot be verified"
grep -q ':8800' <<< "$SS_OUT" || abort "E4: port 8800 is not listening according to 'ss' — the listener inventory is not trustworthy"
PUBLIC_BINDS="$(printf '%s\n' "$SS_OUT" | awk -v ports="$PRIVATE_PORTS" '
  BEGIN { n = split(ports, want, " ") }
  NR == 1 && $1 == "State" { next }
  {
    addr = $4
    p = addr; sub(/.*:/, "", p)
    host = addr; sub(/:[^:]*$/, "", host)
    for (i = 1; i <= n; i++)
      if (p == want[i] && (host == "0.0.0.0" || host == "*" || host == "[::]" || host == "::"))
        print addr
  }')"
[[ -z "${PUBLIC_BINDS//[[:space:]]/}" ]] \
  || abort "E4: QAMC/OneCLI port(s) are bound to a PUBLIC address: $PUBLIC_BINDS"
ok "E4 OneCLI healthy; none of ports $PRIVATE_PORTS is bound to a public address"
note "E4 tailnet-scoped listeners (Tailscale Serve) are private by design and expected."

# E5 — provider/model-routing/broker/market-data/FRED/DB still green after enablement
VERIFY_JSON_E="$(run_verifier "$VERIFY_ARGS" "$TMPD/verify-e.err")"
[[ -n "${VERIFY_JSON_E//[[:space:]]/}" ]] || { [[ -s "$TMPD/verify-e.err" ]] && sed 's/^/         /' "$TMPD/verify-e.err" >&2
  abort "E5: the commissioning verifier produced no output"; }
printf '%s' "$VERIFY_JSON_E" | parse_verifier "config,gateway,wiring,providers,mission-control" 4 \
  || { [[ -s "$TMPD/verify-e.err" ]] && sed 's/^/         /' "$TMPD/verify-e.err" >&2
       abort "E5: commissioning verification failed after enablement"; }
for f in db_reachable broker_reachable; do
  [[ "$(health_field "$f")" == "True" ]] || abort "E5: /health.$f is not true"
done
ok "E5 model routing, OpenRouter, Alpaca trading + market data, FRED and the database all verified after enablement"

# E6 — scheduled surface: exactly seven, all active, none failed, nothing new
TIMERS_E="$(timer_snapshot)"
UNITS_E="$(unit_snapshot)"
[[ "$TIMERS_E" == "$TIMERS_BEFORE" ]] || abort "E6: the timer set changed during this run"
[[ "$UNITS_E" == "$UNITS_BEFORE" ]] || abort "E6: the set of quant-agent units changed — a new unit was introduced"
E_ENABLED_COUNT="$(printf '%s\n' "$TIMERS_E" | awk '$2 ~ /^enabled/ {print $1}' | grep -c . || true)"
[[ "$E_ENABLED_COUNT" == "$REQUIRED_TIMERS" ]] || abort "E6: $E_ENABLED_COUNT enabled timers, require exactly $REQUIRED_TIMERS"
TIMER_ERR="$(assert_timers_healthy 'gate E')" || abort "$TIMER_ERR"
ok "E6 exactly $E_ENABLED_COUNT quant-agent timers, all active, zero failed units, no unit added or removed"

# E7 — wrappers and cadence
WRAP_ERR="$(check_wrappers)" || abort "E7: $WRAP_ERR"
UNITS_CAT_E="$(sysctl_user "cat 'quant-agent-*.service'" 2>/dev/null || true)"
grep -q 'intra_check' <<< "$UNITS_CAT_E" || abort "E7: no unit invokes intra_check any more"
grep -qx "$INTRA_TIMER" <<< "$(printf '%s\n' "$TIMERS_E" | awk '$2 ~ /^enabled/ {print $1}')" \
  || abort "E7: $INTRA_TIMER is no longer enabled"
ok "E7 wrappers source the runtime env; $INTRA_SERVICE still scheduled by $INTRA_TIMER"

# E8 — Telegram healthy, token not exposed anywhere readable
GETME_E="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && \
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
  'https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe'" 2>/dev/null || true)"
[[ "$GETME_E" == "200" ]] || abort "E8: Telegram getMe returned '${GETME_E:-no response}'"
as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" \
  || abort "E8: the runtime env no longer holds only the placeholder"
# A real bot token is `<digits>:<35-ish url-safe chars>`. Its shape is
# searchable without knowing the value, so a leak into the runtime log is
# detectable here.
if as_qamc "grep -qE '/bot[0-9]{6,}:[A-Za-z0-9_-]{20,}' '$QAMC_REPO/quant_agent.log'" 2>/dev/null; then
  abort "E8: a Telegram-bot-token-shaped string appears in the runtime log"
fi
ok "E8 Telegram healthy (getMe 200, no message sent); token only in OneCLI; no token-shaped string in the runtime log"

# E9 — Mission Control healthy, read-only, both front ends serving
for path in /cockpit/ /ui/ /health; do
  code="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -L "${API_BASE}${path}" || true)"
  [[ "$code" == "200" ]] || abort "E9: ${path} returned HTTP ${code:-no response}, expected 200"
  ok "E9 ${path} -> 200"
done
for m in POST PUT DELETE PATCH; do
  code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -X "$m" "$API_BASE/positions" || true)"
  [[ "$code" =~ ^(404|405)$ ]] || abort "E9: $m /positions returned $code — Mission Control must be GET-only"
done
ok "E9 Mission Control is GET-only (POST/PUT/DELETE/PATCH all rejected)"

# E10 — decision chain and intraday guardrails intact in the deployed commit
qgit "grep -qF 'self.decision_stage.run(ctx)' HEAD -- src/pipeline.py" || abort "E10: DecisionStage is not invoked from the pipeline"
qgit "grep -qF 'self.risk_stage.run(ctx)' HEAD -- src/pipeline.py"     || abort "E10: RiskStage is not invoked from the pipeline"
qgit "grep -qF 'self.execution_stage.run(ctx)' HEAD -- src/pipeline.py" || abort "E10: ExecutionStage is not invoked from the pipeline"
qgit "grep -qF '_intraday_scan_process_lock' HEAD -- src/pipeline.py"   || abort "E10: the intraday process lock is missing"
qgit "grep -qF '_another_session_recently_active' HEAD -- src/pipeline.py" || abort "E10: the concurrent-session guard is missing"
qgit "grep -qF '_recently_intraday_evaluated' HEAD -- src/pipeline.py"  || abort "E10: the per-symbol cooldown guard is missing"
qgit "grep -qF 'max_candidates_per_scan' HEAD -- src/pipeline.py"       || abort "E10: the candidate cap is not applied in the scan"
qgit "grep -qF 'CURRENT SESSION (TODAY, INCOMPLETE' HEAD -- src/agents/tech_analyst.py" \
  || abort "E10: the incomplete-session labelling for Tech is missing"
ok "E10 Specialists -> PM -> AI Risk -> deterministic execution intact; scan guarded by process lock, session guard, cooldown and candidate cap; current-session data labelled INCOMPLETE"

# E11 — account boundaries unchanged
[[ "$(stat -c '%U:%G' "$QAMC_REPO/main.py")" == "${QAMC_USER}:${QAMC_USER}" ]] || abort "E11: the production checkout is no longer owned by $QAMC_USER"
[[ "$(stat -c '%a %U:%G' "$QAMC_ENV")" == "600 ${QAMC_USER}:${QAMC_USER}" ]] || abort "E11: runtime env permissions changed"
if id -nG "$QAMC_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  abort "E11: $QAMC_USER is in the docker group — that is root-equivalent and collapses the account boundary"
fi
if id -nG dev 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  abort "E11: the dev account is in the docker group — account boundary collapsed"
fi
ok "E11 qamc/dev/ubuntu boundaries intact (runtime files qamc-owned, no docker-group escalation)"

# E12 — the PR #48 fixes are still present and still truthful AFTER enablement
ACC_E="$(curl -sS --max-time 20 "$API_BASE/account" 2>/dev/null || true)"
POS_E="$(curl -sS --max-time 20 "$API_BASE/positions" 2>/dev/null || true)"
[[ -n "${ACC_E//[[:space:]]/}" && -n "${POS_E//[[:space:]]/}" ]] || abort "E12: /account or /positions returned nothing"
python3 - "$ACC_E" "$POS_E" <<'PY' || abort "E12: SGOV liquidity is no longer truthful after enablement"
import json, sys
acct = json.loads(sys.argv[1]); pos = json.loads(sys.argv[2])
liq = acct.get("liquidity") or {}
sym = liq.get("sweep_symbol")
raw, parked, total = liq.get("raw_cash"), liq.get("sweep_parked_value"), liq.get("total_liquidity")
assert None not in (raw, parked, total), "a liquidity component is missing"
assert abs((raw + parked) - total) < 0.01, "liquidity no longer reconciles"
rows = [p for p in (pos.get("positions") or []) if p.get("symbol") == sym]
if parked > 0:
    assert rows, "parked value with no backing position"
    assert all(p.get("is_cash_equivalent") and p.get("direction") == "cash_equivalent" for p in rows), \
        "the sweep vehicle is no longer labelled cash-equivalent"
print("   [ OK ] E12 %s still reported as cash-equivalent sweep parking, liquidity reconciles" % sym)
PY
qgit "grep -qF 'return confirmed' HEAD -- src/execution/cash_sweep.py" || abort "E12: the confirmed-only funding fix is not in the deployed tree"
qgit "grep -qE 'analyses\.get\(sym\) for sym in submitted' HEAD -- src/agents/tech_analyst.py" || abort "E12: the Tech batch-completeness fix is not in the deployed tree"
ok "E12 SGOV funding fix and Tech batch-completeness fix both present and truthful after enablement"

# E13 — final health
[[ "$(health_field status)" == "ok" ]] || abort "E13: Mission Control /health is not ok at the finish line"
[[ "$(health_field session_lock_active)" == "False" ]] || note "E13 a trading session started during this run — that is normal scheduled activity."
ok "E13 Mission Control healthy at the finish line"

say "GATE E PASSED"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 9 — FINISH LINE"
# ═════════════════════════════════════════════════════════════════════════════

cat <<EOF

  ===============================
  GATE E / FINISH LINE PASSED
  ===============================

  Production SHA        : $E_SHA
  Production tree       : $E_TREE
  Was                   : $BASELINE_SHA
  Config delta          : config/settings.yaml — intraday_scan.enabled false -> true
                          (the ONE expected production-vs-commit delta)
  Gate order            : A -> B -> C -> D -> E, each fail-closed on the deployed tree
  Alpaca                : Paper only
  Intraday scan         : ENABLED — $INTRA_SERVICE on $INTRA_TIMER, no new timer
                          threshold 3.0% | cooldown 3.0h | cap 5 per tick
  Telegram              : getMe 200, no message sent, token only in OneCLI
  Providers             : injection + LIVE preflight through QAMC's own clients
  Mission Control       : /cockpit, /ui, /health all 200; GET-only
  Timers                : $E_ENABLED_COUNT quant-agent timers, all active, zero failed
  Orders placed         : NONE — this script never submits, cancels or modifies an order

  POST-ROLLOUT SOAK EVIDENCE (not gates, deliberately not manufactured here):
    * the first live Tech batch log line ("Batch: N/M symbols analyzed")
    * the first live intraday tick on the next weekday inside 09:30-16:00 ET

  ROLLBACK (code + config + process, converged):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout -- config/settings.yaml && git checkout --detach $BASELINE_SHA" \\
        && sudo -u $QAMC_USER -H bash -c "$SYSTEMD_ENV systemctl --user restart $API_UNIT" \\
        && sleep 5 && curl -s $API_HEALTH

  DISABLE INTRADAY ONLY (keep the deployed code):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout -- config/settings.yaml"

  The checkout is intentionally DETACHED at a pinned SHA. Do not "tidy" it with
  \`git checkout main\` — that would follow a moving branch into production.

EOF
say "DONE — return the full transcript ($LOG) for closeout"
