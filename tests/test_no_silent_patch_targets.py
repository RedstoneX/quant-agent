"""Guard for item 210 criterion 3: no test may patch a name on
``src.pipeline`` / ``src.pipeline_stages`` that the split has turned into a
bare re-export, because such a patch silently no-ops (the real function runs
and the assertion still passes).  Derived mechanically by
``scripts/audit_moved_patch_targets.py``; see its docstring for the buckets.

Seeded 2026-10-01 from the audit on main at that date.  The counts below are
a ratchet: REEXPORT and MISSING must stay at zero, and the leak list (call
sites a patch cannot reach because the moved code imports the name from
somewhere else inside a function) may shrink but never grow.
"""
from __future__ import annotations

from pathlib import Path

from scripts.audit_moved_patch_targets import run

ROOT = Path(__file__).resolve().parents[1]

# Seeded 2026-10-01: 157 LIVE, 26 MIRRORED, 0 REEXPORT, 0 MISSING.
SEED_LIVE = 157
SEED_MIRRORED = 26

# Known unreachable call sites, as "<patched module>.<name>" -> set of the
# split-out modules whose function-local import bypasses the patch.  Remove
# an entry when the call site is fixed; never add one without fixing it.
KNOWN_LEAKS = {
    "src.pipeline.compute_indicators": {
        "src.pipeline", "src.pipeline_exits", "src.pipeline_intraday",
    },
    "src.pipeline._get_sector": {"src.pipeline_prompt_facts"},
    "src.pipeline.AlpacaBroker": {"src.pipeline_protection"},
    "src.pipeline.PortfolioManagerAgent": {"src.pipeline_stages"},
    "src.pipeline_stages.compute_indicators": {"src.stage_execution"},
}


def _report():
    return run(ROOT)


def test_no_reexport_or_missing_patch_targets():
    report = _report()
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
    """The scanner must still SEE the patches; a count of zero means the
    scanner broke, not that the tests stopped patching."""
    counts = _report()["counts"]
    assert counts["LIVE"] >= 100, counts
    assert counts["MIRRORED"] >= 1, counts


def test_unreachable_call_sites_do_not_grow():
    leaks: dict[str, set[str]] = {}
    for f in _report()["findings"]:
        for leak in f["leaks_to"]:
            leaks.setdefault(f"{f['module']}.{f['name']}", set()).add(leak.split(":")[0])
    new = {k: v - KNOWN_LEAKS.get(k, set()) for k, v in leaks.items()}
    new = {k: v for k, v in new.items() if v}
    assert not new, (
        "a patched name is now ALSO loaded, unreachably, by: " + repr(new)
        + " -- the patch cannot reach those call sites; patch the owning module instead"
    )
    stale = {k: v - leaks.get(k, set()) for k, v in KNOWN_LEAKS.items()}
    stale = {k: v for k, v in stale.items() if v}
    assert not stale, f"KNOWN_LEAKS lists fixed sites, delete them: {stale}"
