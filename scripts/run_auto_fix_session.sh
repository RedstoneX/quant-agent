#!/usr/bin/env bash
# Auto-fix session runner — systemd entry point for quant-agent-auto-fix.service.
#
# WHAT THIS IS. When a desk health report finishes, this starts a single
# bounded, non-interactive Claude session that reads the report and the board,
# and — only if it finds one concrete, provable, bounded defect — fixes it and
# opens a pull request. Finding nothing and exiting is the expected outcome of
# most runs. The owner's standing ruling authorising it:
# "the part of plugging you into the logs that's fine, you can use my cloud
# allowance, make sure that you're running everything with adversary."
#
# WHY IT IS A SEPARATE UNIT AND NOT PART OF THE HEALTH REPORT.
# The health report runs as `qamc`, the trading service account: read-only, no
# git, no model call, no network beyond one Telegram push. This job is the
# opposite of all four. Bolting it onto that service would hand the account
# that trades a credential that can change the code that trades it, and it
# would make a code-changing failure look like a failed health report. They run
# as different users for the same reason the desk and the engineer are
# different users: `docs/WORK.md` — "Work as `ubuntu`, never as `qamc`".
#
# WHAT BOUNDS IT.
#   * TIME — `timeout` here at AUTO_FIX_TIMEOUT_SEC (default 1200s, the same
#     outer kill the trading sessions use), with systemd's TimeoutStartSec set
#     higher so OUR kill lands first and the cleanup trap below actually runs.
#     This CLI (2.1.275) has no `--max-turns`, so the clock is the only cap on
#     the loop and it has to be real.
#   * SCOPE — one fix per run, stated in the prompt.
#   * PERMISSION — config/auto_fix_permissions.json, deny-wins, loaded with
#     `--setting-sources local` so no other settings file can widen it.
#   * BLAST RADIUS — a throwaway worktree, removed on exit whatever happens.
#     It opens pull requests; it never deploys. A merge to main is not a
#     deploy on this desk (scripts/merge_and_deploy.sh is a separate, manual
#     step), so nothing here can reach the live desk on its own.
#
# HOW IT IS TRIGGERED. Not by a clock. `scripts/run_log_health_report.sh`
# writes a completion marker as its last step and `quant-agent-auto-fix.path`
# reacts to it, so this cannot start before the report it reads has actually
# finished (owner instruction 2026-09-20: event-driven, never a guessed delay).
#
# Usage:
#   run_auto_fix_session.sh               # the real thing
#   run_auto_fix_session.sh --rehearse      # read, reason, report; change nothing
set -euo pipefail

REHEARSE=0
if [[ "${1:-}" == "--rehearse" ]]; then
    REHEARSE=1
    shift
fi
if [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--rehearse]" >&2
    exit 2
fi

ENGINEERING_ROOT="${AUTO_FIX_ENGINEERING_ROOT:-/home/ubuntu/projects/quant-agent}"
LIVE_ROOT="${AUTO_FIX_LIVE_ROOT:-/home/qamc/quant-agent}"
CLAUDE_BIN="${AUTO_FIX_CLAUDE_BIN:-/home/ubuntu/.claude/remote/ccd-cli/2.1.275}"
WT_BASE="${AUTO_FIX_WT_BASE:-/tmp/claude-1000/wt}"
TIMEOUT_SEC="${AUTO_FIX_TIMEOUT_SEC:-1200}"
SNAPSHOT_HOURS="${AUTO_FIX_SNAPSHOT_HOURS:-12}"
MODEL="${AUTO_FIX_MODEL:-opus}"
# The base the throwaway worktree is cut from. Always `origin/main` in
# production — an unattended session fixes what is shipped, never what is on
# somebody's branch. Overridable only so this wrapper can be rehearsed against
# the branch that introduces it, before it is on main and can rehearse itself.
BASE_REF="${AUTO_FIX_BASE_REF:-origin/main}"
LOG_PREFIX="[auto-fix]"

log() { echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S %Z') $*"; }

# --- credential -------------------------------------------------------------
# The OAuth token lives in ubuntu's ~/.bashrc, and a systemd user service does
# not source it (non-interactive shells return early from .bashrc long before
# line 125). Pull just that one export line and eval it. The value is never
# printed, never copied to a file, and never passed on a command line.
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    _token_line="$(grep -m1 '^export CLAUDE_CODE_OAUTH_TOKEN=' "${HOME}/.bashrc" 2>/dev/null || true)"
    if [[ -n "$_token_line" ]]; then
        eval "$_token_line"
    fi
    unset _token_line
fi
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    log "no CLAUDE_CODE_OAUTH_TOKEN available; cannot start a session. Exiting."
    exit 1
fi

# --- health-report snapshot -------------------------------------------------
# A read-only re-render of the report the owner just received. `--no-telegram`
# sends nothing and does not advance the report's watermark, so this cannot
# steal the window from the next real report — which is the one way a read-only
# job like this could still break the thing it is reading.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/auto-fix-XXXXXX")"
SNAPSHOT="${WORKDIR}/health_snapshot.txt"
WT=""

cleanup() {
    local status=$?
    if [[ -n "$WT" && -d "$WT" ]]; then
        git -C "$ENGINEERING_ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
        git -C "$ENGINEERING_ROOT" worktree prune >/dev/null 2>&1 || true
    fi
    rm -rf "$WORKDIR"
    return $status
}
trap cleanup EXIT INT TERM

if sudo -n -u qamc bash -c \
        "cd '${LIVE_ROOT}' && timeout 200 ./.venv/bin/python scripts/log_health_report.py --no-telegram --since-hours ${SNAPSHOT_HOURS}" \
        > "$SNAPSHOT" 2>/dev/null; then
    log "captured health snapshot ($(wc -l < "$SNAPSHOT") lines)"
else
    log "health snapshot unavailable; continuing with the board only"
    echo "(health report snapshot unavailable for this run — read the board only)" > "$SNAPSHOT"
fi

# The marker the report wrote on its way out, if this run was triggered by one.
# It carries the report's own exit status, which the snapshot above cannot: a
# report that FAILED TO DELIVER is a different situation from a healthy desk,
# and the session should be able to tell them apart.
HANDOFF_MARKER="${AUTO_FIX_HANDOFF_DIR:-/var/lib/quant-agent/handoff}/health-report-complete"
if [[ -r "$HANDOFF_MARKER" ]]; then
    {
        echo ""
        echo "--- health report handoff ---"
        cat "$HANDOFF_MARKER"
    } >> "$SNAPSHOT"
fi

# --- fresh worktree off a freshly-fetched origin/main -----------------------
git -C "$ENGINEERING_ROOT" fetch origin --quiet
BRANCH="autofix/$(TZ=America/New_York date +%Y-%m-%d)-$$"
WT="${WT_BASE}/${BRANCH//\//-}"
mkdir -p "$WT_BASE"
git -C "$ENGINEERING_ROOT" worktree add --quiet -b "$BRANCH" "$WT" "$BASE_REF"
log "worktree ${WT} on ${BRANCH} at $(git -C "$WT" rev-parse --short HEAD)"

# --- the session ------------------------------------------------------------
PROMPT_FILE="${WT}/config/prompts/auto_fix_session.md"
SETTINGS_FILE="${WT}/config/auto_fix_permissions.json"
for f in "$PROMPT_FILE" "$SETTINGS_FILE"; do
    [[ -f "$f" ]] || { log "missing ${f}; refusing to run"; exit 1; }
done

# --- the adversary ----------------------------------------------------------
# The owner's ruling authorising this loop attaches one condition — "make sure
# that you're running everything with adversary" — so a session that cannot
# reach `qamc-adversary` is not a cheaper version of this job, it is a
# different and unauthorised one.
#
# It has to be injected explicitly. `.claude/agents/qamc-adversary.md` is a
# PROJECT-scoped agent, and we deliberately pass `--setting-sources local` to
# keep the project's Stop hook and the user's standing permission grants out of
# an unattended run — which drops the project's agents with them. The first
# rehearsal (2026-09-20) found exactly this: the session reported the adversary
# missing from its roster and ruled out every rule/threshold/gate/exit item on
# the board for that reason alone. Silently correct behaviour, and silently the
# wrong job.
#
# So the agent is read from its own file and handed in with `--agents`. One
# definition, still the file on disk, no second copy to drift.
ADVERSARY_FILE="${WT}/.claude/agents/qamc-adversary.md"
AGENTS_JSON=""
if [[ -f "$ADVERSARY_FILE" ]]; then
    AGENTS_JSON="$(python3 - "$ADVERSARY_FILE" <<'PY'
import json, sys, re
text = open(sys.argv[1], encoding="utf-8").read()
m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
front, body = (m.group(1), m.group(2)) if m else ("", text)
def field(name):
    hit = re.search(rf"^{name}:\s*(.+)$", front, re.M)
    return hit.group(1).strip() if hit else ""
agent = {"description": field("description") or "Argues against a QAMC proposal.",
         "prompt": body.strip()}
tools = field("tools")
if tools:
    agent["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
model = field("model")
if model:
    agent["model"] = model
print(json.dumps({"qamc-adversary": agent}))
PY
)" || AGENTS_JSON=""
fi
if [[ -z "$AGENTS_JSON" ]]; then
    log "could not build the qamc-adversary definition; refusing to run"
    log "a session that cannot run the adversary is not the job the owner authorised"
    exit 1
fi

PROMPT="$(cat "$PROMPT_FILE")"
if [[ "$REHEARSE" -eq 1 ]]; then
    PROMPT="${PROMPT}

---

## REHEARSAL NOTICE — THIS RUN

This is a supervised rehearsal. Read everything, reason all the way to a
conclusion, then STOP. Change no file, run no test that writes, make no commit,
push no branch, open no pull request, invoke no agent that would. Report what
you WOULD have done and why, and end with either \`AUTO-FIX: NO ACTION\` or
\`AUTO-FIX: WOULD HAVE FIXED\` followed by the one defect you would have taken."
fi

cd "$WT"
export AUTO_FIX_HEALTH_SNAPSHOT="$SNAPSHOT"

# Rehearsal is enforced, not requested. The notice appended to the prompt tells
# the session to change nothing; this makes it true regardless of whether the
# session agrees, which is the difference between a rule that holds and a rule
# that is written down. A supervised trial run whose safety rests on the thing
# being trialled agreeing to be safe is not a trial.
REHEARSE_ARGS=()
if [[ "$REHEARSE" -eq 1 ]]; then
    REHEARSE_ARGS=(--disallowedTools
        "Edit" "Write" "NotebookEdit"
        "Bash(git commit:*)" "Bash(git push:*)" "Bash(git add:*)"
        "Bash(gh pr create:*)" "Bash(gh pr merge:*)" "Bash(gh api:*)")
fi

log "starting session (model=${MODEL}, cap=${TIMEOUT_SEC}s, rehearse=${REHEARSE})"
# `--add-dir "$WORKDIR"` is what lets the session read the health snapshot,
# which deliberately lives OUTSIDE the worktree so it can never be committed.
#
# THE PROMPT GOES IN ON STDIN, NOT AS AN ARGUMENT. `--disallowedTools` is
# variadic, so a positional prompt after it is swallowed as more tool names —
# the first rehearsal run (2026-09-20) parsed the entire standing prompt into
# about two hundred nonexistent deny rules, one per word, and then died with
# "Input must be provided either through stdin or as a prompt argument". Stdin
# has no such ambiguity, and it also keeps several thousand words out of the
# process table where `ps` would show them.
set +e
timeout --kill-after=30 "$TIMEOUT_SEC" \
    "$CLAUDE_BIN" \
    --print \
    --model "$MODEL" \
    --permission-mode acceptEdits \
    --settings "$SETTINGS_FILE" \
    --setting-sources local \
    --agents "$AGENTS_JSON" \
    --add-dir "$WORKDIR" \
    ${REHEARSE_ARGS[@]+"${REHEARSE_ARGS[@]}"} \
    <<< "$PROMPT"
STATUS=$?
set -e

case "$STATUS" in
    0)   log "session finished cleanly" ;;
    124|137) log "session hit the ${TIMEOUT_SEC}s cap and was killed; worktree discarded" ;;
    *)   log "session exited ${STATUS}" ;;
esac

exit "$STATUS"
