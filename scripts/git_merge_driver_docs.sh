#!/usr/bin/env bash
# Git merge-driver front end for scripts/resolve_doc_conflict.py.
#
# WHY THIS EXISTS (docs/WORK.md item 68's follow-up). The item-aware resolver
# only helps a merge if a human remembers to run it by hand. This script is
# what git calls automatically instead, for the three board documents named
# in .gitattributes.
#
# THIS IS NOT SELF-ACTIVATING. `.gitattributes` names a driver by the label
# `docsmerge`; git only wires that label to this script if the clone has also
# run, once:
#
#   git config merge.docsmerge.name   "item-aware doc conflict resolver"
#   git config merge.docsmerge.driver "scripts/git_merge_driver_docs.sh %O %A %B %P"
#
# (see README.md "### Install"). A clone that skips that step is UNAFFECTED —
# .gitattributes with no matching merge.<name>.driver falls back to git's
# normal 3-way text merge, conflict markers and all. That fallback is the
# fail-closed behaviour this file depends on: this script runs the ordinary
# case, never the safety net.
#
# GIT'S CONTRACT (gitattributes(5), "Defining a custom merge driver"):
#   argv: %O %A %B %P
#     %O = common ancestor (base), a temp file
#     %A = "our" version, a temp file — git reads the MERGE RESULT from HERE
#     %B = "their" version, a temp file
#     %P = the path in the tree, used to pick which of the three documents
#          this is (the resolver has one entry point per document kind)
#   exit 0  = clean merge, %A holds the result git will use.
#   exit !=0 = git treats the path as still conflicted, the same as if no
#          driver had run at all. We never touch %A on that path, so it is
#          left exactly as git initialised it (the "ours" content, no
#          markers) and `git status` reports the file unmerged — a human
#          resolves it by hand, same as any other merge conflict.
#
# The resolver's own exit code 2 ("REFUSING TO WRITE — this merge needs a
# human") is exactly the "still conflicted" case above: a refusal must read to
# git as an unresolved merge, never as success, so the deliberate stop for a
# human is never mistaken for a machine having agreed.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "git_merge_driver_docs.sh: expected %O %A %B %P, got $#" >&2
  exit 1
fi

BASE=$1
OURS=$2
THEIRS=$3
TREE_PATH=$4

case "${TREE_PATH}" in
  docs/WORK.md) KIND=work ;;
  docs/BOARD_NOTES.md) KIND=notes ;;
  docs/INCIDENT_HISTORY.md) KIND=history ;;
  *)
    echo "git_merge_driver_docs.sh: no resolver kind for '${TREE_PATH}'" >&2
    exit 1
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

exec "${PY}" "${REPO_ROOT}/scripts/resolve_doc_conflict.py" \
  --kind "${KIND}" --base "${BASE}" --ours "${OURS}" --theirs "${THEIRS}" \
  --out "${OURS}"
