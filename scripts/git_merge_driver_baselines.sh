#!/usr/bin/env bash
# Git merge-driver front end for scripts/resolve_baseline_conflict.py.
#
# WHY THIS EXISTS. Fifteen open changes each edited the same shrink-only
# baselines under tests/, so every landing made the rest conflict. This is what
# git calls for the five files named `merge=baselinemerge` in .gitattributes.
#
# THIS IS NOT SELF-ACTIVATING. `.gitattributes` names a driver by the label
# `baselinemerge`; git only wires that label to this script after the clone has
# run, once (scripts/install_git_merge_drivers.sh does both drivers):
#
#   git config merge.baselinemerge.name   "shrink-only baseline resolver"
#   git config merge.baselinemerge.driver "scripts/git_merge_driver_baselines.sh %O %A %B %P"
#
# (see README.md "### Install"). A clone that skips it falls back to git's
# ordinary text merge, conflict markers and all: fail-closed, same as docsmerge.
#
# GIT'S CONTRACT: argv %O base, %A ours (the result is read from HERE), %B
# theirs, %P tree path. Exit 0 = resolved; non-zero = conflicted. On refusal
# (exit 2) the resolver writes diff3 conflict markers into %A so the file is
# loudly invalid rather than silently missing the other side's content.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "git_merge_driver_baselines.sh: expected %O %A %B %P, got $#" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

exec "${PY}" "${REPO_ROOT}/scripts/resolve_baseline_conflict.py" \
  --base "$1" --ours "$2" --theirs "$3" --out "$2" --tree-path "$4"
