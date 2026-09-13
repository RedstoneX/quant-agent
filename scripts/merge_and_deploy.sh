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

echo "==> merging PR #${PR}"
gh pr merge "${PR}" --squash --delete-branch

# Verify rather than assume. A squash-merge can be refused (branch behind,
# a check still pending) and gh's exit status is not a reliable proxy.
state="$(gh pr view "${PR}" --json state -q .state)"
if [[ "${state}" != "MERGED" ]]; then
  echo "PR #${PR} is ${state}, not MERGED — nothing deployed." >&2
  exit 1
fi

deploy
echo "==> PR #${PR} merged AND deployed"
