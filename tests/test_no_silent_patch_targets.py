"""Guard for item 210 criterion 3: no test may patch a name on
``src.pipeline`` / ``src.pipeline_stages`` that the split has turned into a
bare re-export, because such a patch silently no-ops (the real function runs
and the assertion still passes).  Derived mechanically by
``scripts/audit_moved_patch_targets.py``; see its docstring for the buckets.

**This guard stores nothing** (docs/GUARDS_WITHOUT_STORED_STATE.md).  It used
to carry a hand-written ``KNOWN_LEAKS`` map of the offenders that existed on
the day it was seeded.  That list was a cached measurement: the same collision
engine as the deleted JSON baselines, only written in Python, so every change
that touched a leaking call site had to edit the one shared dict.

Instead the leak set is measured twice at check time — once against this
working tree, once against ``origin/main`` materialised into a scratch
directory — and only the DELTA is reported.  If ``origin/main`` cannot be read
the guard REFUSES with an error; it never passes by default.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from scripts.audit_moved_patch_targets import run
from scripts.guard_reference import (
    ROOT,
    TRUNK,
    ReferenceUnavailable,
    added_sites,
    trunk_blobs,
    trunk_paths,
)


def _leaks_of(report: dict) -> set[tuple[str, str]]:
    """Identity of every unreachable call site: ``("<module>.<name>", <module whose
    own binding or function-local import bypasses the patch>)``. Line numbers
    are dropped so the identity survives line shifts."""
    return {
        (f"{finding['module']}.{finding['name']}", leak.split(":")[0])
        for finding in report["findings"]
        for leak in finding["leaks_to"]
    }


def _working_report() -> dict:
    return run(ROOT)


def _trunk_report() -> dict:
    """Run THIS tree's scanner over ``origin/main``'s Python sources.

    The trunk's ``.py`` blobs are written to a throwaway directory in one
    ``git cat-file --batch``; nothing is cached and no worktree is added, so
    two agents running this at once cannot collide.
    """
    paths = trunk_paths(".py")
    if not paths:
        raise ReferenceUnavailable(f"{TRUNK} has no .py files; refusing to compare.")
    blobs = trunk_blobs(paths)
    if not blobs:
        raise ReferenceUnavailable(f"could not read any .py blob from {TRUNK}.")
    with tempfile.TemporaryDirectory(prefix="trunk-patch-audit-") as tmp:
        root = Path(tmp)
        for path, text in blobs.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        if not (root / "tests").is_dir():
            raise ReferenceUnavailable(f"{TRUNK} has no tests/ directory to scan.")
        return run(root)


def test_no_reexport_or_missing_patch_targets():
    report = _working_report()
    bad = [
        f"{f['test_file']}:{f['lineno']} {f['module']}.{f['name']} [{f['classification']}]"
        for f in report["findings"]
        if f["classification"] in {"REEXPORT", "MISSING"}
    ]
    assert not bad, (
        "patch targets that silently no-op (REEXPORT) or raise (MISSING); either "
        "patch the module that now owns the name, or extend the write-through "
        "mirror in src/pipeline_stages.py to cover it:\n  " + "\n  ".join(bad)
    )
    assert report["counts"]["REEXPORT"] == 0
    assert report["counts"]["MISSING"] == 0


def test_patch_target_census_is_nonempty():
    """The scanner must still SEE the patches here and on the trunk; a count of
    zero means the scanner broke, not that the tests stopped patching."""
    here = _working_report()["counts"]
    there = _trunk_report()["counts"]
    assert there["LIVE"] > 0, f"scanner saw no patch targets on {TRUNK}: {there}"
    assert here["LIVE"] > 0, f"scanner saw no patch targets in this tree: {here}"


def test_unreachable_call_sites_do_not_grow():
    """Report only what THIS change made worse against ``origin/main``."""
    # Identity comparison, never a total: a change that removes one leak and
    # adds a different one must still fail (scripts/guard_reference.py).
    new = [f"{key} -> {leak}" for (key, leak), _, _ in added_sites(
        _leaks_of(_working_report()), _leaks_of(_trunk_report())
    )]
    assert not new, (
        f"this change adds unreachable call sites that are not on {TRUNK}: {new!r}"
        " -- a patch of that name cannot reach those call sites, so the real code"
        " runs and the assertion still passes; patch the owning module instead,"
        " or stop importing the name inside the function."
    )


def test_unreadable_trunk_refuses_instead_of_passing(monkeypatch):
    """No readable ``origin/main`` means REFUSE, never pass by default."""
    import pytest
    from scripts import guard_reference

    monkeypatch.setattr(guard_reference, "TRUNK", "origin/no-such-branch-for-test")
    with pytest.raises(ReferenceUnavailable):
        _trunk_report()


def test_guard_bites_on_a_new_leak_and_clears_when_removed():
    """Real leak set vs itself is clean; one genuinely new offender goes red;
    taking it away goes green again."""
    real = _leaks_of(_working_report())
    assert added_sites(real, _leaks_of(_trunk_report())) == []
    injected = real | {("src.pipeline.brand_new_name", "src.fake_module")}
    assert [k for k, _, _ in added_sites(injected, real)] == [
        ("src.pipeline.brand_new_name", "src.fake_module")
    ]
    assert added_sites(injected - {("src.pipeline.brand_new_name", "src.fake_module")}, real) == []
