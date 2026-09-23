#!/usr/bin/env bash
# Merge a PR and deploy it to the box in ONE action, because doing them as two
# steps is how the box ends up behind.
#
# WHY THIS EXISTS. On 2026-09-12 eleven PRs were merged and the production
# checkout stayed on an earlier commit for hours. Nothing was broken: merging
# had simply been reported as shipping, three separate times, because the
# second step lived only in somebody's memory. The deploy-drift check that
# would have caught it ran Mon-Fri and every one of those merges landed on a
# Saturday (that timer is now daily — see quant-agent-drift-check.timer).
#
# The safety net catching it the next morning is not the fix. The fix is that
# there is no gap to catch: one command, or the deploy did not happen.
#
# USAGE
#   scripts/merge_and_deploy.sh <pr-number>
#   scripts/merge_and_deploy.sh --deploy-only     # box is behind, no PR to merge
#
# Deliberately NOT automatic on merge (no CI deploy hook): a deploy restarts
# the live API and this desk trades real orders on a paper account. It stays an
# action a person takes knowingly. What it must not stay is an action a person
# has to REMEMBER.
set -euo pipefail

DEPLOY_ROOT=/home/qamc/quant-agent
API_UNIT=quant-agent-api.service
QAMC_UID="$(id -u qamc)"

deploy() {
  echo "==> fetching origin/main into ${DEPLOY_ROOT}"
  sudo -n git -C "${DEPLOY_ROOT}" fetch origin main
  # The production checkout runs on a DETACHED HEAD on purpose — it is pinned
  # to an exact commit, not tracking a branch. `git pull` there fails with a
  # confusing "you are not currently on a branch", which is its own small trap.
  echo "==> checking out origin/main (detached, as production always runs)"
  sudo -n git -C "${DEPLOY_ROOT}" checkout --detach origin/main
  echo "==> restarting ${API_UNIT}"
  sudo -n -u qamc "XDG_RUNTIME_DIR=/run/user/${QAMC_UID}" \
    systemctl --user restart "${API_UNIT}"

  local deployed expected
  deployed="$(sudo -n git -C "${DEPLOY_ROOT}" rev-parse HEAD)"
  expected="$(git ls-remote origin refs/heads/main | cut -f1)"
  if [[ "${deployed}" != "${expected}" ]]; then
    echo "DEPLOY DID NOT LAND: box at ${deployed}, origin/main is ${expected}" >&2
    exit 1
  fi
  echo "==> deployed ${deployed}"
}

if [[ "${1:-}" == "--deploy-only" ]]; then
  deploy
  exit 0
fi

PR="${1:?usage: merge_and_deploy.sh <pr-number> | --deploy-only}"

# Read the state BEFORE merging, so "I merged it" can be told apart from
# "somebody merged it days ago". Without this, a mistyped PR number that
# happens to name an already-merged PR sails through the check below and
# deploys whatever origin/main holds right now — including work being
# deliberately held back.
was="$(gh pr view "${PR}" --json state -q .state)"
if [[ "${was}" == "MERGED" ]]; then
  echo "PR #${PR} was ALREADY merged before this run — refusing." >&2
  echo "      Use --deploy-only if you meant to push origin/main to the box." >&2
  exit 1
fi

echo "==> merging PR #${PR}"
# NO `--delete-branch`. The repository already has `delete_branch_on_merge`
# enabled, so GitHub removes the head branch itself and the flag adds only a
# LOCAL delete — which fails whenever any worktree still has that branch
# checked out, the normal state here because agents leave worktrees behind.
# Under `set -euo pipefail` that failure killed the run AFTER the squash had
# landed on GitHub but BEFORE `deploy` ran, so origin/main moved and the box
# silently stayed behind while the output read like a failure. Dropping the
# redundant flag removes that abort at its source and keeps gh's exit status
# meaning something, rather than swallowing every gh error to tolerate one.
# Stale local branches are cleaned up separately, not from the deploy path.
gh pr merge "${PR}" --squash

# Verify rather than assume. A squash-merge can be refused (branch behind,
# a check still pending) and gh's exit status is not a reliable proxy.
state="$(gh pr view "${PR}" --json state -q .state)"
if [[ "${state}" != "MERGED" ]]; then
  echo "PR #${PR} is ${state}, not MERGED — nothing deployed." >&2
  exit 1
fi

deploy

# `deploy` pins the box to whatever origin/main is at the moment it looks,
# which is a moving target: branch protection is off and sessions merge in
# parallel, so another PR can land between this merge and that checkout.
# Assert that THIS PR's merge commit is actually an ancestor of what the box
# now runs, so the closing line cannot claim a deploy of code that did not
# reach it.
merge_sha="$(gh pr view "${PR}" --json mergeCommit -q .mergeCommit.oid)"
deployed_sha="$(sudo -n git -C "${DEPLOY_ROOT}" rev-parse HEAD)"
sudo -n git -C "${DEPLOY_ROOT}" fetch -q origin "${merge_sha}" 2>/dev/null || true
if ! sudo -n git -C "${DEPLOY_ROOT}" merge-base --is-ancestor \
       "${merge_sha}" "${deployed_sha}" 2>/dev/null; then
  echo "PR #${PR} merged as ${merge_sha}, but the box is on ${deployed_sha}," >&2
  echo "      which does not contain it. Re-run --deploy-only." >&2
  exit 1
fi
echo "==> PR #${PR} merged AND deployed"
