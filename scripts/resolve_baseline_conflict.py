#!/usr/bin/env python3
"""Shrink-only three-way resolver for the machine-maintained baseline JSON files.

**Why this exists.** Every change that retires a file-size excess, an import
cycle, a silent swallow or a `TradingPipeline.__new__` test edits the same few
ratchet baselines under tests/, so every landing made every other open change
conflict or go stale. These files are shrink-only ratchets, so the right merge
is mechanical and never needs a human.

**The rule** (one entry point, shape chosen by the file's name):

  * `tests/file_size_baseline.json` (path -> line count). Start from the merge
    base. Key on both sides: the MINIMUM (tighter number wins). Key on one side
    only: dropped if the other side deleted it (a deliberate shrink), and kept
    only if it is a NEW key whose file still exists in the merged tree. A key
    naming a deleted or renamed file is always dropped.
  * `tests/silent_swallow_baseline.json` (`keys`), `tests/pipeline_new_baseline.json`
    (`files`), `tests/import_cycle_baseline.json` (`edges`) are frozen sets that
    may only shrink: the result is the intersection with the base, so an entry
    either side removed stays removed. An entry on one side that the base did
    not have is a baseline being GROWN and is refused. Path-shaped entries
    (`keys`, `files`) are also dropped when their file no longer exists.
  * `tests/import_layers.json`: the allowlist is each rule's `exact_importers`
    (`scripts/import_graph.py` fails when a listed module stops importing the
    target, and the set must never widen). It is merged as a frozen set exactly
    like the lists above, matched by rule name. Every other rule field is an
    ordinary three-way value: the side that changed it wins, two different
    changes refuse. A rule only one side has is kept if the base lacked it (a
    new rule) and dropped if the base had it and the other side deleted it,
    unless the surviving side also changed it, which refuses.

**After resolving**, the result is asserted never larger than the base: no
count above its base value, no set member the base lacked. Anything that fails
is a baseline being LOOSENED behind someone's back, and the resolver REFUSES.

**What a refusal leaves on disk.** Like the docs resolver: the output is the
three-way text with diff3 conflict markers (invalid JSON, so every guard and
`json.load` fails loudly), never the markerless ours copy, and the exit code
is 2 with the reason on stderr.

**File existence** is decided on the MERGED tree, not the working tree: git
runs a driver before it has applied a deletion made on the other side
[reproduced with git 2.43]. During a merge/cherry-pick/rebase the other side is
the ref named in GIT_REFLOG_ACTION (git has not written MERGE_HEAD yet when a
driver runs) or MERGE_HEAD/CHERRY_PICK_HEAD/REBASE_HEAD/REVERT_HEAD; with none
of those, e.g. some rebases, the working tree is used.

Usage (git calls it through scripts/git_merge_driver_baselines.sh):
    resolve_baseline_conflict.py --base B --ours A --theirs C --out A --tree-path P
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

REFUSE = 2


class Refusal(Exception):
    """The merge would loosen a baseline or cannot be resolved by the rule."""


KIND_BY_NAME = {
    "file_size_baseline.json": "counts",
    "silent_swallow_baseline.json": "keys",
    "pipeline_new_baseline.json": "files",
    "import_cycle_baseline.json": "edges",
    "import_layers.json": "layers",
}
# Which list-valued field is the frozen set, and whether its entries name files.
SET_FIELD = {"keys": ("keys", True), "files": ("files", True), "edges": ("edges", False)}


def _entry_path(entry) -> str:
    return str(entry).split("::", 1)[0]


def _freeze(x):
    return json.dumps(x, sort_keys=True)


def merge_counts(base: dict, ours: dict, theirs: dict, exists: Callable[[str], bool]) -> dict:
    out = {}
    for key in list(dict.fromkeys([*base, *ours, *theirs])):
        in_o, in_t = key in ours, key in theirs
        if key.startswith("_"):
            vals = [d[key] for d in (ours, theirs) if key in d]
            changed = {_freeze(v) for v in vals if key not in base or v != base[key]}
            if len(changed) > 1:
                raise Refusal(f"both sides changed metadata key {key!r} differently")
            if vals:
                out[key] = next((v for v in vals if _freeze(v) in changed), vals[0])
            continue
        if in_o and in_t:
            val = min(ours[key], theirs[key])
        elif in_o or in_t:
            if key in base:
                continue  # the other side removed it: a deliberate shrink
            val = (ours if in_o else theirs)[key]
        else:
            continue
        if not exists(key):
            continue  # names a deleted or renamed file
        out[key] = val
    for key, val in out.items():
        if key in base and not key.startswith("_") and val > base[key]:
            raise Refusal(f"{key}: resolved {val} is LARGER than {base[key]} on main (loosened baseline)")
    return dict(sorted(out.items()))


def merge_set(base: list, ours: list, theirs: list, path_like: bool, exists: Callable[[str], bool]) -> list:
    b, o, t = ({_freeze(x) for x in lst} for lst in (base, ours, theirs))
    grown = (o | t) - b
    if grown:
        raise Refusal(f"entries not in the base were added (baseline grown): {sorted(grown)[:5]}")
    keep = b & o & t
    by_frozen = {_freeze(x): x for x in [*base, *ours, *theirs]}
    items = [by_frozen[k] for k in keep]
    if path_like:
        items = [x for x in items if exists(_entry_path(x))]
    return sorted(items)


def merge_layers(base: dict, ours: dict, theirs: dict) -> dict:
    def rules(d):
        return {r["name"]: r for r in d.get("rules", [])}

    rb, ro, rt = rules(base), rules(ours), rules(theirs)
    out_rules = []
    for name in list(dict.fromkeys([*ro, *rt, *rb])):
        b, o, t = rb.get(name), ro.get(name), rt.get(name)
        if o is not None and t is not None:
            out_rules.append(_merge_rule(name, b or {}, o, t))
        elif o is not None or t is not None:
            only = o if o is not None else t
            if b is None:
                out_rules.append(only)
            elif only != b:
                raise Refusal(f"rule {name!r} was deleted on one side and edited on the other")
        # else: deleted on both sides, or absent everywhere
    result = {k: v for k, v in ours.items() if k != "rules"}
    for k, v in theirs.items():
        if k != "rules" and k not in base and k not in result:
            result[k] = v
    for k in {*base, *ours, *theirs} - {"rules"}:
        if k in ours and k in theirs and ours[k] != theirs[k]:
            if ours[k] == base.get(k):
                result[k] = theirs[k]
            elif theirs[k] != base.get(k):
                raise Refusal(f"both sides changed top-level {k!r} differently")
    result["rules"] = out_rules
    return result


def _merge_rule(name: str, b: dict, o: dict, t: dict) -> dict:
    out = {}
    for k in list(dict.fromkeys([*o, *t])):
        if k == "exact_importers":
            out[k] = merge_set(b.get(k, []), o.get(k, []), t.get(k, []), False, lambda _p: True)
            continue
        if k in o and k in t:
            if o[k] == t[k] or t[k] == b.get(k):
                out[k] = o[k]
            elif o[k] == b.get(k):
                out[k] = t[k]
            else:
                raise Refusal(f"rule {name!r}: both sides changed {k!r} differently")
        else:
            out[k] = (o if k in o else t)[k]
    return out


def resolve(kind: str, base: dict, ours: dict, theirs: dict, exists: Callable[[str], bool]) -> dict:
    if kind == "counts":
        return merge_counts(base, ours, theirs, exists)
    if kind == "layers":
        return merge_layers(base, ours, theirs)
    field, path_like = SET_FIELD[kind]
    out = {k: v for k, v in ours.items() if k != field}
    for k, v in theirs.items():
        if k != field and k not in out:
            out[k] = v
    for k in out:
        if k != field and k in theirs and theirs[k] != ours[k] and ours[k] == base.get(k):
            out[k] = theirs[k]
    out[field] = merge_set(base.get(field, []), ours.get(field, []), theirs.get(field, []), path_like, exists)
    return out


# --- file existence on the merged tree --------------------------------------

def _git(*args: str) -> str | None:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _other_side() -> str | None:
    """The commit being merged in, or None when it cannot be told.

    git has NOT yet written MERGE_HEAD when it runs a driver [reproduced with
    git 2.43], so for a plain `git merge <ref>` the only signal is the
    GIT_REFLOG_ACTION git exports ("merge <ref>").
    """
    for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "REVERT_HEAD"):
        if _git("rev-parse", "-q", "--verify", ref):
            return ref
    words = os.environ.get("GIT_REFLOG_ACTION", "").split()
    if words[:1] == ["merge"]:
        for w in words[1:]:
            if not w.startswith("-") and _git("rev-parse", "-q", "--verify", f"{w}^{{commit}}"):
                return w
    return None


def make_exists() -> Callable[[str], bool]:
    other = _other_side()
    base = _git("merge-base", "HEAD", other) if other else None
    if not other or not base:
        return lambda p: Path(p).exists()

    def in_tree(rev: str, p: str) -> bool:
        return subprocess.run(["git", "cat-file", "-e", f"{rev}:{p}"], capture_output=True).returncode == 0

    def exists(p: str) -> bool:
        o, t, b = in_tree("HEAD", p), in_tree(other, p), in_tree(base, p)
        return (o and t) or (not b and (o or t))

    return exists


def _write(path: Path, data: dict, like_text: str) -> None:
    indent = 2 if "\n  \"" in like_text else 1
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    for a in ("base", "ours", "theirs", "out", "tree-path"):
        ap.add_argument(f"--{a}", required=True)
    ns = ap.parse_args(argv)
    kind = KIND_BY_NAME.get(Path(ns.tree_path).name)
    if kind is None:
        print(f"resolve_baseline_conflict: no rule for {ns.tree_path!r}", file=sys.stderr)
        return 1
    base_txt, ours_txt, theirs_txt = (Path(p).read_text() for p in (ns.base, ns.ours, ns.theirs))
    try:
        result = resolve(kind, *(json.loads(t) for t in (base_txt, ours_txt, theirs_txt)), make_exists())
    except Refusal as e:
        merged = subprocess.run(["git", "merge-file", "-p", "--diff3", ns.ours, ns.base, ns.theirs],
                                capture_output=True, text=True).stdout
        if "<<<<<<<" not in merged:
            merged = f"<<<<<<< ours\n{ours_txt}||||||| base\n{base_txt}=======\n{theirs_txt}>>>>>>> theirs\n"
        Path(ns.out).write_text(merged)
        print(f"REFUSING to resolve {ns.tree_path}: {e}", file=sys.stderr)
        return REFUSE
    _write(Path(ns.out), result, ours_txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
