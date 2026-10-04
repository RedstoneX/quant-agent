#!/usr/bin/env bash
# One-time, per-clone: register the custom merge driver named in
# .gitattributes (docsmerge). Safe to re-run. `.gitattributes` alone does
# nothing; see README.md "### Install".
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git config merge.docsmerge.name   "item-aware doc conflict resolver"
git config merge.docsmerge.driver "scripts/git_merge_driver_docs.sh %O %A %B %P"
echo "registered merge drivers: docsmerge"
