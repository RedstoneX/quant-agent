#!/usr/bin/env bash
# One-time, per-clone: register the custom merge driver named in
# .gitattributes (docsmerge). Safe to re-run. `.gitattributes` alone does
# nothing; see README.md "### Install".
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git config merge.docsmerge.name   "item-aware doc conflict resolver"
git config merge.docsmerge.driver "scripts/git_merge_driver_docs.sh %O %A %B %P"
echo "registered merge drivers: docsmerge"

# `baselinemerge` was registered by an earlier version of this script for
# scripts/git_merge_driver_baselines.sh. That script and the stored baselines
# it arbitrated were deleted with the stored-bookkeeping rip-out, and nothing
# in .gitattributes routes to the label any more — but the per-clone git
# config survives a `git pull`, so a clone that ever ran the old installer
# still points a live merge driver at a script that no longer exists. Clear
# it here rather than leaving each clone to discover it mid-merge.
git config --unset-all merge.baselinemerge.name   2>/dev/null || true
git config --unset-all merge.baselinemerge.driver 2>/dev/null || true
echo "cleared stale merge driver: baselinemerge"
