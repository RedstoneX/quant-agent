#!/usr/bin/env bash
# One-time, per-clone: register BOTH custom merge drivers named in
# .gitattributes (docsmerge, baselinemerge). Safe to re-run. `.gitattributes`
# alone does nothing; see README.md "### Install". tests/test_baseline_merge_driver.py
# warns loudly when this has not been run in the clone running the tests.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git config merge.docsmerge.name   "item-aware doc conflict resolver"
git config merge.docsmerge.driver "scripts/git_merge_driver_docs.sh %O %A %B %P"
git config merge.baselinemerge.name   "shrink-only baseline resolver"
git config merge.baselinemerge.driver "scripts/git_merge_driver_baselines.sh %O %A %B %P"
echo "registered merge drivers: docsmerge, baselinemerge"
