#!/usr/bin/env bash
#
# QAMC Telegram production activation — run ONCE, as root, from a root-owned copy.
#
#   sudo install -o root -g root -m 0700 <this file> /root/qamc-telegram-activate.sh \
#     && sudo bash /root/qamc-telegram-activate.sh
#
# The copy is not ceremony: /home/dev is writable by the Claude Code account, and
# root must not execute a file that account can rewrite between review and run.
# This script refuses to run unless it is owned by root and not group/world
# writable.
#
# ORDER OF OPERATIONS — the important property of this script:
#
#   PHASE 1  verify everything. No change anywhere.
#   PHASE 2  configure the Telegram secret + grant in OneCLI. Still no change to
#            production: no deploy, no edit of the runtime env file.
#   PHASE 3  PREFLIGHT. Prove URL-path injection and api.telegram.org egress work
#            end to end, through the real gateway, using a TRANSIENT environment
#            and a harmless placeholder URL. Non-message probe (getMe). If this
#            fails the script aborts with PRODUCTION COMPLETELY UNCHANGED —
#            still exactly the baseline SHA, runtime env file untouched.
#   PHASE 4  only now: deploy the exact hotfix SHA and verify what landed.
#   PHASE 5  write the harmless placeholder + chat id into the runtime env file.
#   PHASE 6  exactly ONE non-trading Telegram message.
#   PHASE 7  confirm the scheduled wrappers read the same env path (nothing fired).
#
# The REAL bot token is entered non-echoing and goes ONLY to the OneCLI API over
# loopback. It is never written to the runtime env file, never placed in argv,
# never exported, never logged, and never written to any file — API responses are
# streamed through pipes rather than saved, because a create response echoes a
# short preview of the stored value.
#
# This script contains NO real secret values.
#
set -euo pipefail

# ── Pinned, externally approved constants ────────────────────────────────────
BASELINE_SHA="766877109b60026c94c00b38dbfb0e0c9630d236"
HOTFIX_SHA="9c736c158fec84129765c25a9429254d3602ad6b"
HOTFIX_BRANCH="fix/telegram-restoration-prod-baseline"

QAMC_USER="qamc"
QAMC_REPO="/home/qamc/quant-agent"
QAMC_ENV="${QAMC_REPO}/.env"

ONECLI_API="http://127.0.0.1:10254/api"
SECRET_NAME="Telegram - QAMC Bot Token"
SECRET_HOST="api.telegram.org"
PATH_TEMPLATE='/bot{value}'

# Non-empty so the notifier enables; no "/" so it occupies exactly the one path
# segment the template rewrites; obviously not a credential if it ever surfaces.
TG_PLACEHOLDER="ONECLI-INJECTS-THIS-PLACEHOLDER"

# ── Output helpers (never used to print secret material) ─────────────────────
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   [ OK ] %s\n' "$*"; }
info() { printf '   [ .. ] %s\n' "$*"; }
warn() { printf '   \033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m[FAIL]\033[0m %s\n\n' "$*" >&2; exit 1; }

WORKDIR=""
cleanup() { if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then rm -rf "$WORKDIR"; fi; }
# A bare `trap cleanup INT` cleans up and then CONTINUES to the next statement,
# which would run later phases against a deleted WORKDIR. Exit explicitly.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

as_qamc() { sudo -u "$QAMC_USER" -H bash -c "$1"; }
qgit() { as_qamc "cd '$QAMC_REPO' && git $1"; }

# ── OneCLI HTTP helpers ──────────────────────────────────────────────────────
# Responses are returned on stdout, never written to disk: GET /agents carries a
# live gateway access token and POST /secrets echoes a preview of the stored
# value. Neither should outlive the pipe.
onecli_get() {  # $1 = path
  local resp code
  resp="$(curl -sS --max-time 15 -w $'\n%{http_code}' "$ONECLI_API$1" || true)"
  code="${resp##*$'\n'}"
  [[ "$code" == "200" ]] || die "GET $ONECLI_API$1 returned http ${code:-no response}. STOP."
  printf '%s' "${resp%$'\n'*}"
}

# `error` in a OneCLI 4xx body is a zod validation message, never a credential —
# surfacing it turns "http 400" into an actionable failure.
onecli_write() {  # $1 = METHOD, $2 = path, $3 = expected code; body on stdin
  local resp code body msg
  resp="$(curl -sS --max-time 20 -w $'\n%{http_code}' -X "$1" "$ONECLI_API$2" \
            -H 'Content-Type: application/json' --data-binary @- || true)"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  if [[ "$code" != "$3" ]]; then
    msg="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("error", ""))
except Exception:
    print("")
' 2>/dev/null || true)"
    die "$1 $ONECLI_API$2 returned http ${code:-no response}${msg:+ — $msg}"
  fi
  printf '%s' "$body"
}

# Re-assertable invariant: production is untouched.
assert_production_untouched() {
  local head tok rc
  head="$(qgit 'rev-parse HEAD')"
  [[ "$head" == "$BASELINE_SHA" ]] \
    || die "production HEAD is $head, expected $BASELINE_SHA. Internal ordering error — STOP."
  [[ -z "$(qgit 'status --porcelain')" ]] \
    || die "production working tree is dirty at a point where it must be clean — STOP."

  # grep exit 1 = absent (good). Exit >= 2 = could not read the file, which must
  # NOT be silently read as "absent" — that would make the invariant vacuous.
  set +e
  tok="$(as_qamc "grep -E '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN=' '$QAMC_ENV'")"
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then
    # Tolerate exactly our own placeholder: that is the state a previous run of
    # this script leaves behind, so a re-run after the documented code rollback
    # still works. Anything else is a real credential where none belongs.
    if [[ "$tok" == "TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}" ]]; then
      info "runtime env file already holds this script's placeholder (earlier run) — fine"
    else
      die "the runtime env file contains a TELEGRAM_BOT_TOKEN that is not this
       script's placeholder, at a point where it must not. Inspect it as $QAMC_USER
       and remove the line before re-running. STOP."
    fi
  elif [[ "$rc" -ge 2 ]]; then
    die "could not read $QAMC_ENV (grep exit $rc) — the untouched-production
       invariant cannot be proven. STOP."
  fi
  return 0
}

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 1 — verification only (nothing is modified anywhere)"
# ═════════════════════════════════════════════════════════════════════════════

[[ "${EUID}" -eq 0 ]] || die "Must run as root. See the header for the exact command."
ok "running as root"

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SELF_OWNER="$(stat -c '%U' "$SELF")"
SELF_PERMS="$(stat -c '%a' "$SELF")"
[[ "$SELF_OWNER" == "root" ]] || die "This script is owned by '$SELF_OWNER', not root ($SELF).
       Root must not execute a file another account can rewrite. Copy it first:
         sudo install -o root -g root -m 0700 $SELF /root/qamc-telegram-activate.sh
       then run the root-owned copy."
[[ "${SELF_PERMS: -2}" =~ ^[0-5][0-5]$ ]] || die "This script is group/world writable (mode $SELF_PERMS). Fix with: chmod 0700 $SELF"
ok "running from a root-owned, non-world-writable file"

id -u "$QAMC_USER" >/dev/null 2>&1 || die "runtime account '$QAMC_USER' not found"
[[ -d "$QAMC_REPO/.git" ]] || die "production checkout not found at $QAMC_REPO"
[[ -f "$QAMC_ENV" ]]      || die "runtime env file not found at $QAMC_ENV"
[[ -x "$QAMC_REPO/.venv/bin/python" ]] || die "runtime virtualenv missing: $QAMC_REPO/.venv/bin/python"
as_qamc "test -r '$QAMC_ENV'" \
  || die "$QAMC_ENV is not readable by $QAMC_USER. Fix that first — every check
       below would otherwise be evaluating an unreadable file."
ok "production checkout, env file (readable by $QAMC_USER) and virtualenv present"

ENV_PERM="$(stat -c '%a %U:%G' "$QAMC_ENV")"
[[ "$ENV_PERM" == "600 ${QAMC_USER}:${QAMC_USER}" ]] \
  || die "runtime env file is '$ENV_PERM', expected '600 ${QAMC_USER}:${QAMC_USER}'. Fix before continuing."
ok "runtime env file is 600 ${QAMC_USER}:${QAMC_USER}"

REPO_OWNER="$(stat -c '%U:%G' "$QAMC_REPO/src/notifier.py")"
[[ "$REPO_OWNER" == "${QAMC_USER}:${QAMC_USER}" ]] \
  || die "checkout ownership is $REPO_OWNER, expected ${QAMC_USER}:${QAMC_USER}."
ok "checkout owned by ${QAMC_USER}:${QAMC_USER}"

WORKDIR="$(mktemp -d)"; chmod 700 "$WORKDIR"

# ── 1a. OneCLI must be live and new enough for URL-path injection ────────────
ONECLI_VERSION="$(onecli_get /health | python3 -c 'import json,sys;print(json.load(sys.stdin).get("version",""))')"
[[ -n "$ONECLI_VERSION" ]] || die "OneCLI health returned no version"
ok "OneCLI live, version $ONECLI_VERSION"

python3 -c '
import sys
v = tuple(int(x) for x in sys.argv[1].split(".")[:3])
sys.exit(0 if v >= (1, 40, 0) else 1)
' "$ONECLI_VERSION" \
  || die "OneCLI < 1.40.0 — URL-path injection unavailable. STOP and escalate;
       do NOT put the real token in the runtime env file as a workaround."
ok "OneCLI supports URL-path injection (>= 1.40.0)"

# ── 1b. Production must be exactly at the approved baseline ──────────────────
CURRENT_SHA="$(qgit 'rev-parse HEAD')"
[[ "$CURRENT_SHA" == "$BASELINE_SHA" ]] \
  || die "production HEAD is $CURRENT_SHA, expected baseline $BASELINE_SHA.
       Assumptions differ — STOP. Do not force this script."
ok "production HEAD == approved baseline $BASELINE_SHA"

[[ -z "$(qgit 'status --porcelain')" ]] || die "production checkout has local modifications — STOP and investigate."
ok "production working tree clean"

set +e
as_qamc "grep -Eq '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_DISABLED=' '$QAMC_ENV'"
DISABLED_RC=$?
set -e
case "$DISABLED_RC" in
  0) die "TELEGRAM_DISABLED is already present in the runtime env file.
       Remove that line first (or mute intentionally and skip this script) —
       otherwise the delivery probe would report MUTED and prove nothing." ;;
  1) ok "TELEGRAM_DISABLED not set (pushes will not be muted)" ;;
  *) die "could not read $QAMC_ENV while checking TELEGRAM_DISABLED (grep exit $DISABLED_RC). STOP." ;;
esac

WIRING_BEFORE="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?(HTTPS_PROXY|SSL_CERT_FILE|REQUESTS_CA_BUNDLE)=' '$QAMC_ENV'" || true)"
[[ "$WIRING_BEFORE" == "3" ]] \
  || die "expected 3 OneCLI wiring lines (HTTPS_PROXY, SSL_CERT_FILE, REQUESTS_CA_BUNDLE)
       in the runtime env file, found '${WIRING_BEFORE:-0}'. STOP."
ok "OneCLI proxy/CA wiring present (3 lines) — this script will not touch them"

# ── 1c. Fetch the approved commit — objects only, HEAD and worktree untouched ─
# An explicit refspec, so origin/main is never updated. This adds objects (and
# FETCH_HEAD) inside .git; it moves no ref the running code reads and alters no
# deployed file. Re-asserted immediately below. It has to happen before the
# OneCLI writes because the parent==baseline check gates everything.
info "fetching only $HOTFIX_BRANCH (main is deliberately not fetched or merged)"
qgit "fetch --no-tags origin '$HOTFIX_BRANCH'" >/dev/null 2>&1 \
  || die "could not fetch $HOTFIX_BRANCH from origin"
qgit "cat-file -e ${HOTFIX_SHA}^{commit}" 2>/dev/null \
  || die "approved hotfix commit $HOTFIX_SHA not present after fetch"

HOTFIX_PARENT="$(qgit "rev-parse ${HOTFIX_SHA}^")"
[[ "$HOTFIX_PARENT" == "$BASELINE_SHA" ]] \
  || die "hotfix parent is $HOTFIX_PARENT, expected $BASELINE_SHA — NOT the approved
       single-commit transition. STOP."
ok "hotfix $HOTFIX_SHA fetched; its parent == baseline (PR #48 cannot ride along)"

assert_production_untouched
ok "deployed tree still exactly $BASELINE_SHA (the fetch moved nothing)"

# ── 1d. Exactly one OneCLI agent, so the grant cannot go somewhere unintended ─
# Streamed, not saved: this response contains the agent's gateway access token.
AGENTS_JSON="$(onecli_get /agents)"

AGENT_LINE="$(printf '%s' "$AGENTS_JSON" | python3 -c '
import json, sys
agents = json.load(sys.stdin)
if not isinstance(agents, list) or len(agents) != 1:
    sys.exit(1)                      # ambiguity -> fail closed, never guess
print(agents[0]["id"] + " " + agents[0]["name"])
')" || die "expected exactly one OneCLI agent; found a different number.
       Resolve manually — do NOT let this script pick which agent gets the token."
AGENT_ID="${AGENT_LINE%% *}"
AGENT_NAME="${AGENT_LINE#* }"
unset AGENTS_JSON AGENT_LINE
ok "single OneCLI agent resolved: '$AGENT_NAME' ($AGENT_ID)"

# ── 1e. Identify the managed secret — by IDENTITY, not merely by host ────────
#
# Host match alone is not identity: PATCHing "some secret that happens to point
# at api.telegram.org" could silently overwrite an unrelated credential. The
# managed object must match name AND host AND injection config AND have no path
# restriction AND be an inline value. Anything else in the collision set is a
# conflict, and conflicts fail closed.
SECRETS_JSON="$(onecli_get /secrets)"

MATCH_PY='
import json, sys

host, name, template = sys.argv[1], sys.argv[2], sys.argv[3]
secrets = json.load(sys.stdin)
expected_injection = {"pathTemplate": template}

# Non-secret metadata only. The list endpoint returns no value and no preview,
# but whitelist rather than blacklist so a future field cannot leak into a report.
SAFE = ("id", "name", "type", "valueSource", "hostPattern", "pathPattern",
        "injectionConfig", "scope", "createdAt")

def host_collides(pattern):
    """Mirror of the gateway matcher (apps/gateway/src/connect.rs::host_matches).

    Deliberately NOT a stricter re-implementation: the gateway is what actually
    injects, so anything IT would match on this host can fight over the same
    path segment. It splits on the FIRST "*" anywhere (not just a leading
    "*." label) and compares case-insensitively, so "API.Telegram.org",
    "api.*.org", "*telegram.org" and a bare "*" all collide. The write-time
    schema is narrower today, but an older or hand-made row need not be.
    """
    p = (pattern or "").strip()
    h = host.casefold()
    if "*" not in p:
        return p.casefold() == h
    prefix, suffix = p.split("*", 1)
    prefix, suffix = prefix.casefold(), suffix.casefold()
    return (len(h) >= len(prefix) + len(suffix)
            and h.startswith(prefix)
            and h.endswith(suffix))

def is_expected(s):
    return (s.get("name") == name
            and (s.get("hostPattern") or "").strip().casefold() == host.casefold()
            and s.get("injectionConfig") == expected_injection
            # A pathPattern would restrict where the rule applies, and a PATCH
            # that omits the field leaves it in place — so a restricted row is
            # NOT the managed object even if everything else matches.
            and not s.get("pathPattern")
            # An external-vault row must not be silently converted to inline.
            and s.get("valueSource") in (None, "inline"))

# The collision set: anything that could plausibly BE the managed object, or
# could interfere with it if left in place.
candidates = [s for s in secrets if host_collides(s.get("hostPattern")) or s.get("name") == name]

def report(rows):
    for s in rows:
        print("    - " + json.dumps({k: s.get(k) for k in SAFE if k in s}, sort_keys=True))

if not candidates:
    print("CREATE")
    sys.exit(0)

if len(candidates) == 1 and is_expected(candidates[0]):
    print("UPDATE " + candidates[0]["id"])
    sys.exit(0)

print("CONFLICT")
print("  Refusing to touch any of these. Expected exactly one secret with")
print("    name=%r  hostPattern=%r  injectionConfig=%r" % (name, host, expected_injection))
print("    and no pathPattern, valueSource inline.")
print("  Found %d colliding secret(s) (non-secret metadata only):" % len(candidates))
report(candidates)
sys.exit(0)
'

MATCH_OUT="$(printf '%s' "$SECRETS_JSON" | python3 -c "$MATCH_PY" "$SECRET_HOST" "$SECRET_NAME" "$PATH_TEMPLATE")" \
  || die "could not evaluate existing OneCLI secrets. STOP."
MATCH_VERDICT="$(printf '%s\n' "$MATCH_OUT" | head -n 1)"

case "$MATCH_VERDICT" in
  CREATE)
    EXISTING_SECRET_ID=""
    ok "no colliding $SECRET_HOST secret — one will be created"
    ;;
  UPDATE\ *)
    EXISTING_SECRET_ID="${MATCH_VERDICT#UPDATE }"
    ok "exactly one secret matches the managed identity ($EXISTING_SECRET_ID) — it will be updated in place"
    ;;
  CONFLICT)
    printf '%s\n' "$MATCH_OUT" | tail -n +2 >&2
    die "Conflicting or unrecognised Telegram/$SECRET_HOST secrets exist in OneCLI.
       Resolve them by hand — this script will not guess which one is QAMC's and
       will not overwrite an unrelated credential. Production is unchanged."
    ;;
  *)
    die "unexpected secret-matching verdict '$MATCH_VERDICT'. STOP."
    ;;
esac

say "PHASE 1 complete — all preconditions hold. Production is still $BASELINE_SHA."

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 2 — OneCLI: Telegram secret + grant (production NOT touched)"
# ═════════════════════════════════════════════════════════════════════════════

cat <<EOF

  The bot token you enter next is stored ONLY in OneCLI.
  It is read without echo, piped to the API over loopback, and is never written
  to $QAMC_ENV, never placed in a command line, and never logged.

  If this bot token was ever configured before the redaction fix, ROTATE IT in
  BotFather first and enter the NEW token here — the old one may sit in
  quant_agent.log or the journal from an earlier failed send.

EOF

TG_TOKEN=""
read -rsp "  Telegram bot token (input hidden): " TG_TOKEN; echo
[[ -n "$TG_TOKEN" ]] || die "empty bot token"

# Reject values the gateway would refuse to place in a URL path, so the failure
# surfaces here rather than as a mystery 401 from Telegram later.
printf '%s' "$TG_TOKEN" | python3 -c '
import sys
tok = sys.stdin.read().strip()
if not tok:
    sys.exit(1)
bad = [c for c in tok if c in "/?#% " or ord(c) < 32 or 0x7f <= ord(c) <= 0x9f]
sys.exit(1 if bad else 0)
' || die "bot token is empty or contains a character the gateway cannot place in a
       URL path (/, ?, #, %, space or control). Check what you pasted."

# NOTE: `python3 -c`, deliberately, NOT `python3 - <<HEREDOC`. A here-doc would
# take over stdin and the piped token would read back as the empty string —
# silently storing an empty secret. Program via -c keeps stdin the pipe.
build_body() {
  printf '%s' "$TG_TOKEN" | python3 -c '
import json, sys
token = sys.stdin.read().strip()
name, host, template = sys.argv[1], sys.argv[2], sys.argv[3]
if not token:
    sys.exit("refusing to send an empty secret value")
sys.stdout.write(json.dumps({
    "name": name,
    "type": "generic",
    "valueSource": "inline",
    "value": token,
    "hostPattern": host,
    "injectionConfig": {"pathTemplate": template},
}))
' "$SECRET_NAME" "$SECRET_HOST" "$PATH_TEMPLATE"
}

if [[ -n "$EXISTING_SECRET_ID" ]]; then
  info "updating managed secret $EXISTING_SECRET_ID"
  build_body | onecli_write PATCH "/secrets/$EXISTING_SECRET_ID" 200 >/dev/null
  SECRET_ID="$EXISTING_SECRET_ID"
  SECRET_WAS_CREATED=0
else
  info "creating the Telegram secret"
  # Piped, never saved: the create response echoes a short preview of the value.
  SECRET_ID="$(build_body | onecli_write POST /secrets 201 \
                 | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
  SECRET_WAS_CREATED=1
fi
unset TG_TOKEN
[[ -n "$SECRET_ID" ]] || die "OneCLI did not return a secret id. STOP."
ok "secret stored in OneCLI (id $SECRET_ID) — token cleared from this shell"

# Confirm the stored object is the expected managed identity, metadata only.
onecli_get /secrets | python3 -c '
import json, sys
sid, host, name, template = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
for s in json.load(sys.stdin):
    if s["id"] == sid:
        assert s.get("name") == name, "name=%r" % (s.get("name"),)
        assert (s.get("hostPattern") or "").strip().casefold() == host.casefold(), \
            "hostPattern=%r" % (s.get("hostPattern"),)
        assert s.get("injectionConfig") == {"pathTemplate": template}, \
            "injectionConfig=%r" % (s.get("injectionConfig"),)
        assert not s.get("pathPattern"), "pathPattern=%r" % (s.get("pathPattern"),)
        assert s.get("valueSource") in (None, "inline"), \
            "valueSource=%r" % (s.get("valueSource"),)
        sys.exit(0)
sys.exit("stored secret %s not found in the list" % sid)
' "$SECRET_ID" "$SECRET_HOST" "$SECRET_NAME" "$PATH_TEMPLATE" \
  || die "stored object is not the expected managed identity (see the assertion
       above — it prints the offending field, never a value). STOP."
ok "stored rule verified: name, host, injection, no pathPattern, inline value"

# ── Grant to the single QAMC agent (assign-only PUT, no body) ────────────────
GRANT_RESP="$(curl -sS --max-time 15 -w $'\n%{http_code}' \
                -X PUT "$ONECLI_API/agents/$AGENT_ID/grants/secrets/$SECRET_ID" || true)"
GRANT_CODE="${GRANT_RESP##*$'\n'}"
[[ "$GRANT_CODE" == "200" ]] || die "PUT grant returned http ${GRANT_CODE:-no response}"

printf '%s' "${GRANT_RESP%$'\n'*}" | python3 -c '
import json, sys
g = json.load(sys.stdin)
secrets = g.get("secrets", g if isinstance(g, list) else [])
sys.exit(0 if any(s.get("secretId") == sys.argv[1] for s in secrets) else 1)
' "$SECRET_ID" \
  || die "grant did not take effect — the gateway would not inject. STOP."
unset GRANT_RESP
ok "granted to agent '$AGENT_NAME' only"

assert_production_untouched
ok "production STILL exactly $BASELINE_SHA — nothing deployed, env file untouched"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 3 — PREFLIGHT: prove injection + egress BEFORE any production change"
# ═════════════════════════════════════════════════════════════════════════════
#
# This is the gate. It exercises the whole credential path for real — the OneCLI
# proxy, the gateway CA, URL-path substitution, and outbound reachability to
# api.telegram.org — using:
#   * a TRANSIENT environment: the proxy/CA variables are read from the runtime
#     env file into a throwaway subshell. The file itself is only READ.
#   * a HARMLESS PLACEHOLDER in the token position of the URL. The real token
#     exists only inside OneCLI and is substituted by the gateway.
#   * getMe — an authentication probe. It sends NO Telegram message.
#
# A 200 is only reachable if substitution actually happened: an un-injected
# placeholder path answers 404, and a wrong token answers 401.
#
# It also covers the curl path specifically, which is what run_if_et_window.sh
# uses for its violent-death "KILLED" alert; the later python probe would not
# exercise curl at all.
#
# If this fails, production has not been altered in any way and the script stops.

PREFLIGHT_URL="https://api.telegram.org/bot${TG_PLACEHOLDER}/getMe"
info "probing ${PREFLIGHT_URL} through the OneCLI gateway (no message is sent)"

set +e
GETME_CODE="$(as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && \
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}' '${PREFLIGHT_URL}'" 2>"$WORKDIR/preflight.err")"
set -e

if [[ "$GETME_CODE" != "200" ]]; then
  [[ -s "$WORKDIR/preflight.err" ]] && sed 's/^/         /' "$WORKDIR/preflight.err" >&2
  UNDO_HINT="The OneCLI secret was updated in place; restore its previous value by hand if needed."
  if [[ "${SECRET_WAS_CREATED:-0}" -eq 1 ]]; then
    UNDO_HINT="To remove the secret this run created:
         curl -X DELETE $ONECLI_API/secrets/$SECRET_ID"
  fi
  die "PREFLIGHT FAILED — getMe returned '${GETME_CODE:-no response}', expected 200.

       PRODUCTION IS COMPLETELY UNCHANGED: the deployed tree is still exactly
       $BASELINE_SHA, $QAMC_ENV untouched, nothing deployed, no message sent.

       Likely causes, in order:
         401  the bot token is wrong or revoked
         404  the gateway did not substitute — check the grant and the rule
         000  the request never left the host: gateway egress to $SECRET_HOST
              denied, or the CA/proxy wiring is not usable by curl
       If a pathPattern or a second matching rule exists on this host, that will
       also break substitution.

       $UNDO_HINT"
fi
ok "getMe returned 200 — OneCLI injected the real token into the URL path"
ok "path injection, gateway CA, proxy and $SECRET_HOST egress all proven"
ok "no Telegram message was sent by this probe"

assert_production_untouched
say "PREFLIGHT PASSED — production is still $BASELINE_SHA. Proceeding to deploy."

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 4 — deploy exact $HOTFIX_SHA (never main)"
# ═════════════════════════════════════════════════════════════════════════════

# Detached checkout of the exact approved commit. No branch is followed, no
# merge, pull or rebase — so current `main` (carrying the deferred PR #48) has
# no path into production.
qgit "checkout --detach '$HOTFIX_SHA'" >/dev/null 2>&1 \
  || die "checkout of $HOTFIX_SHA failed — production may be mid-transition.
       Roll back (see end of this script) and STOP."

NEW_SHA="$(qgit 'rev-parse HEAD')"
[[ "$NEW_SHA" == "$HOTFIX_SHA" ]] || die "post-checkout HEAD is $NEW_SHA, expected $HOTFIX_SHA. Roll back and STOP."
ok "production HEAD == $HOTFIX_SHA"

as_qamc "grep -q '_redact' '$QAMC_REPO/src/notifier.py'" \
  || die "src/notifier.py lacks the reviewed _redact helper. Roll back and STOP —
       without it a failed send writes the bot token into quant_agent.log."
as_qamc "grep -q 'self._redact(exc)' '$QAMC_REPO/src/notifier.py'" \
  || die "redaction helper present but not applied at the log call sites. Roll back and STOP."
ok "reviewed token-redaction fix present and applied at both log call sites"

as_qamc "test -f '$QAMC_REPO/scripts/telegram_test.py'" \
  || die "scripts/telegram_test.py missing from the checkout. Roll back and STOP."
ok "non-trading delivery probe present"

# `git grep <pat> HEAD` searches the commit tree, so stale __pycache__ or other
# untracked leftovers cannot trigger a false "roll back immediately".
if as_qamc "cd '$QAMC_REPO' && git grep -q 'intraday_scan' HEAD -- config src" 2>/dev/null; then
  die "intraday_scan found in the checked-out commit — PR #48 content is present.
       ROLL BACK IMMEDIATELY (see end of this script) and STOP."
fi
ok "intraday_scan absent from the checked-out commit — PR #48 is not deployed"

REPO_OWNER="$(stat -c '%U:%G' "$QAMC_REPO/src/notifier.py")"
[[ "$REPO_OWNER" == "${QAMC_USER}:${QAMC_USER}" ]] \
  || die "checkout ownership became $REPO_OWNER after checkout. Fix before continuing."
ok "checkout still owned by ${QAMC_USER}:${QAMC_USER}"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 5 — runtime env file: placeholder + chat id (idempotent)"
# ═════════════════════════════════════════════════════════════════════════════

echo
read -rp "  TELEGRAM_CHAT_ID (numeric; negative for a group): " TG_CHAT_ID
# Strict numeric guard. Not pedantry: without it, a bot token mispasted at THIS
# prompt would be echoed to the terminal and written into the runtime env file,
# defeating the one guarantee this whole scheme exists to provide.
[[ "$TG_CHAT_ID" =~ ^-?[0-9]+$ ]] \
  || die "chat id must be numeric (digits, optionally leading '-').
       If you just pasted a bot token here, it was NOT stored — re-run and enter
       the numeric chat id. Get it from:
         https://api.telegram.org/bot<TOKEN>/getUpdates  ->  .result[0].message.chat.id"

# The editor runs as qamc via `python3 -c` with values as ARGV — no nested
# `bash -c` string interpolation, so no value can break quoting and execute.
EDITOR_PY='
import os, re, sys
path, placeholder, chat_id = sys.argv[1], sys.argv[2], sys.argv[3]
updates = {"TELEGRAM_BOT_TOKEN": placeholder, "TELEGRAM_CHAT_ID": chat_id}
with open(path) as fh:
    lines = fh.read().splitlines()
seen = set()
out = []
for line in lines:
    m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
    key = m.group(1) if m else None
    if key in updates:
        if key in seen:
            continue                     # drop a pre-existing duplicate
        out.append(key + "=" + updates[key])
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(key + "=" + value)
st = os.stat(path)
tmp = path + ".tmp"
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)
except BaseException:
    # Never leave a stray .tmp behind: it is not gitignored, and a later run
    # would abort on "production checkout has local modifications".
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
'
sudo -u "$QAMC_USER" -H python3 -c "$EDITOR_PY" "$QAMC_ENV" "$TG_PLACEHOLDER" "$TG_CHAT_ID" \
  || die "failed to update $QAMC_ENV. Inspect it before re-running."
ok "TELEGRAM_BOT_TOKEN (placeholder) and TELEGRAM_CHAT_ID written exactly once each"

WIRING_AFTER="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?(HTTPS_PROXY|SSL_CERT_FILE|REQUESTS_CA_BUNDLE)=' '$QAMC_ENV'" || true)"
[[ "$WIRING_AFTER" == "3" ]] || die "OneCLI wiring lines went from 3 to '${WIRING_AFTER:-0}' — restore them before continuing."
ok "OneCLI proxy/CA wiring still intact (3 lines)"

as_qamc "grep -q '^TELEGRAM_BOT_TOKEN=${TG_PLACEHOLDER}\$' '$QAMC_ENV'" \
  || die "the runtime env file does not hold the expected placeholder. STOP and inspect."
ok "runtime holds ONLY the placeholder — the real token is not in $QAMC_ENV"

DUPES="$(as_qamc "grep -cE '^[[:space:]]*(export[[:space:]]+)?TELEGRAM_BOT_TOKEN=' '$QAMC_ENV'" || true)"
[[ "$DUPES" == "1" ]] || die "TELEGRAM_BOT_TOKEN appears '${DUPES:-0}' times — expected exactly 1."
ok "no duplicate Telegram entries"

ENV_PERM="$(stat -c '%a %U:%G' "$QAMC_ENV")"
[[ "$ENV_PERM" == "600 ${QAMC_USER}:${QAMC_USER}" ]] \
  || die "runtime env file became '$ENV_PERM'. Restore 600 ${QAMC_USER}:${QAMC_USER}."
ok "runtime env file still 600 ${QAMC_USER}:${QAMC_USER}"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 6 — exactly ONE non-trading Telegram message"
# ═════════════════════════════════════════════════════════════════════════════
#
# Sources the runtime env file the same way the scheduled wrappers do
# (`set -a; . ./.env; set +a`), because scripts/telegram_test.py deliberately
# loads only TELEGRAM_* — the OneCLI proxy/CA vars must come from the shell.
# No trading mode, no broker order, no LLM call, no market data.

set +e
as_qamc "cd '$QAMC_REPO' && set -a && . ./.env && set +a && .venv/bin/python scripts/telegram_test.py"
PROBE_RC=$?
set -e

if [[ "$PROBE_RC" -ne 0 ]]; then
  die "Telegram delivery probe FAILED (exit $PROBE_RC) even though the PHASE 3
       preflight passed — so the credential path itself works and the fault is
       in the deployed code or the runtime env file, not in OneCLI.
       Production code is at $HOTFIX_SHA. Trading is unaffected either way.
       Read the printed RESULT line, then roll back if desired (see below)."
fi
ok "delivery probe reported DELIVERED — check the chat for exactly one test message"

# ═════════════════════════════════════════════════════════════════════════════
say "PHASE 7 — scheduled wrappers read the same env path (nothing is fired)"
# ═════════════════════════════════════════════════════════════════════════════
#
# Verification only. Everything that changes production already succeeded, so
# nothing here exits non-zero — a naming surprise must not present a completed
# activation as a failure.

QAMC_UID="$(id -u "$QAMC_USER")"
SYSTEMD_ENV="export XDG_RUNTIME_DIR=/run/user/${QAMC_UID}; export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${QAMC_UID}/bus;"

# Matches the wrappers' `source "${PROJECT_ROOT}/.env"` line. Verified against
# both real wrappers, with a negative control, before this script shipped.
WRAPPER_ENV_PAT='source[[:space:]]+"?\$\{?PROJECT_ROOT\}?/\.env'

# `systemctl --user cat` only reads unit files — it starts nothing.
if as_qamc "$SYSTEMD_ENV systemctl --user cat 'quant-agent-*.service'" > "$WORKDIR/units.txt" 2>"$WORKDIR/units.err"; then
  REFERENCED=0
  for wrapper in run_if_et_window.sh run_daily_export.sh; do
    if ! grep -q "$wrapper" "$WORKDIR/units.txt"; then
      info "$wrapper is not referenced by any quant-agent-*.service"
      continue
    fi
    REFERENCED=$((REFERENCED + 1))
    # Plain root-side read: keeping the regex out of a nested double-quoted
    # bash -c string avoids an escaping bug silently turning this check into
    # one that can never match.
    if grep -Eq "$WRAPPER_ENV_PAT" "$QAMC_REPO/scripts/$wrapper"; then
      ok "$wrapper is scheduled AND sources \$PROJECT_ROOT/.env"
    else
      warn "$wrapper is scheduled but does NOT source \$PROJECT_ROOT/.env — scheduled"
      warn "sessions would get neither Telegram vars nor the OneCLI proxy/CA. Investigate."
    fi
  done
  if [[ "$REFERENCED" -eq 0 ]]; then
    warn "no quant-agent unit references either run wrapper — verify the scheduled path by hand."
  fi
  info "quant-agent user units inspected: $(grep -c '^# /' "$WORKDIR/units.txt" || true) (none started or restarted)"
else
  warn "could not read qamc's systemd --user units — verify the scheduled path by hand:"
  sed 's/^/         /' "$WORKDIR/units.err" >&2 || true
fi

# ═════════════════════════════════════════════════════════════════════════════
say "DONE — activation complete"
# ═════════════════════════════════════════════════════════════════════════════
cat <<EOF

  Production checkout : $HOTFIX_SHA  (was $BASELINE_SHA)
  Real bot token      : stored ONLY in OneCLI (secret $SECRET_ID)
  Runtime env file    : placeholder only, real token injected at the gateway
  OneCLI rule         : host $SECRET_HOST  ->  pathTemplate $PATH_TEMPLATE
  Granted to agent    : $AGENT_NAME ($AGENT_ID)
  Preflight           : getMe 200 BEFORE any production change
  Telegram message    : exactly ONE sent, no trading action taken
  PR #48 / intraday   : verified absent from the checked-out commit

  ROLLBACK (code only — leaves OneCLI and Telegram config untouched):

      sudo -u $QAMC_USER -H bash -c "cd $QAMC_REPO && git checkout --detach $BASELINE_SHA"

  The checkout is intentionally DETACHED at a pinned SHA. Do NOT "tidy" it with
  \`git checkout main\` — current main carries the deferred PR #48 and that would
  deploy it. Rolling back also reinstates a notifier without the redaction fix;
  harmless here, because the runtime env file holds only the placeholder.

  Re-running this script after that rollback is supported: it tolerates its own
  placeholder in the runtime env file and will simply rewrite it.

  To mute pushes without deleting anything, add to $QAMC_ENV as $QAMC_USER:

      TELEGRAM_DISABLED=1

  Nothing here enabled trading, deployed PR #48, or touched intraday_scan.

EOF
