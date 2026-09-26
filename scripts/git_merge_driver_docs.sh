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
#   exit !=0 = git treats the path as still conflicted. Git marks the INDEX;
#          it does NOT write conflict markers into the file, and it does not
#          restore anything. Whatever is in %A when the driver exits is what
#          sits in the worktree.
#
# THE CORRECTION OF 2026-09-23. This comment used to say that on a non-zero
# exit "we never touch %A ... a human resolves it by hand, same as any other
# merge conflict", and tests/test_git_merge_driver_docs.py asserted it. Both
# were wrong, and the error was load-bearing: because %A arrives holding the
# OURS copy, refusing without writing left a file that was valid markdown,
# carried no conflict marker, and was MISSING everything that existed only on
# the other side. Three agents hit it in one night and one nearly committed a
# silent revert of other people's board entries. `git checkout
# --conflict=diff3` does not recover it either — that re-invokes this driver,
# which refuses again [reproduced 2026-09-23].
#
# So the resolver now WRITES on refusal: an obviously-unresolved document with
# minimal diff3 conflict regions, both sides preserved, at least one marker
# always present, and the plain-English reason in a gitignored sidecar file
# rather than in the document (see "What a refusal leaves on disk" in
# scripts/resolve_doc_conflict.py). The exit code is still non-zero, because a
# refusal must never read to git as success.
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
  --out "${OURS}" --tree-path "${TREE_PATH}"
