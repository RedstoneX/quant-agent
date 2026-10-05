# Doc-merge unjam toolkit

GitHub cannot run this repo's item-aware doc merge driver, so it marks nearly
every open branch CONFLICTING on `docs/WORK.md` as soon as any other branch
merges. These scripts unjam a branch locally and correctly.

**The rule these exist to enforce:** resolve a doc conflict THREE-WAY through
`scripts/resolve_doc_conflict.py`'s own resolvers, with `ours` = the branch's
pre-merge copy. Never `git checkout origin/main -- docs/WORK.md`. Taking one
side wholesale silently discards the branch's own board edits; on 2026-10-01
that discarded six branches' edits in one sweep and nearly un-retired two
items.

Register the driver once per clone:

    git config merge.docsmerge.name "item-aware doc conflict resolver"
    git config merge.docsmerge.driver "scripts/git_merge_driver_docs.sh %O %A %B %P"

Then, from a spare worktree:

    scripts/docmerge/safe_unjam.sh <branch-name>

- `safe_unjam.sh` — merge `origin/main` into a branch, resolving `docs/WORK.md`
  three-way. Pushes nothing; review
  and push yourself.
- `union_exempt.py` — resolves the grandfathered exemption lists in
  `tests/test_status_board.py` by INTERSECTION, because those lists may only
  ever shrink. Handles both the set and the `{"63": 3431}` dict forms.
- `restore_work.py` — three-way restore of a branch's `docs/WORK.md` after a
  bad resolution, for recovery.
