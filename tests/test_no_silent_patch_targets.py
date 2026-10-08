"""Guard for item 210 criterion 3: no test may patch a name on
``src.pipeline`` / ``src.pipeline_stages`` that the split has turned into a
bare re-export, because such a patch silently no-ops (the real function runs
and the assertion still passes).  Derived mechanically by
``scripts/audit_moved_patch_targets.py``; see its docstring for the buckets.

**Fixed list, no trunk.**  The unreachable call sites that exist today are
pinned by identity (``<module>.<name> | <module that bypasses the patch>``) in
config/check_allowlists/struct_silent_patch_targets.txt (shrink-only, sorted).
Nothing is compared with any trunk.  A site not in the list fails, and so does a
listed site that no longer occurs.
"""
from __future__ import annotations

from scripts import struct_allowlist
from scripts.audit_moved_patch_targets import run
from scripts.struct_allowlist import ROOT


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


def found_leaks(report: dict | None = None) -> list[str]:
    """Every unreachable call site, as ``<module>.<name> | <bypassing module>``."""
    report = _working_report() if report is None else report
    return [f"{key} | {leak}" for key, leak in sorted(_leaks_of(report))]


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
    """The scanner must still SEE the patches here; a count of zero means the
    scanner broke, not that the tests stopped patching."""
    here = _working_report()["counts"]
    assert here["LIVE"] > 0, f"scanner saw no patch targets in this tree: {here}"


FIX = (
    "A patch of that name cannot reach that call site, so the real code runs and "
    "the assertion still passes; patch the owning module instead, or stop "
    "importing the name inside the function."
)


def test_unreachable_call_sites_match_the_fixed_list():
    bad = struct_allowlist.problems("silent_patch_targets", found_leaks(), FIX)
    assert not bad, "\n".join(bad)


def _report_with(*leaks: tuple[str, str]) -> dict:
    module, name = "src.pipeline", "x"
    return {"findings": [{"module": module, "name": name, "leaks_to": [f"{m}:1" for _, m in leaks]}]}


def test_guard_bites_new_listed_and_stale(tmp_path):
    entry = "src.pipeline.x | src.fake_module"
    report = _report_with(("", "src.fake_module"))
    assert found_leaks(report) == [entry]
    struct_allowlist.write("silent_patch_targets", [], tmp_path)
    assert any("NEW" in b for b in struct_allowlist.problems("silent_patch_targets", found_leaks(report), FIX, tmp_path))
    struct_allowlist.write("silent_patch_targets", [entry], tmp_path)
    assert struct_allowlist.problems("silent_patch_targets", found_leaks(report), FIX, tmp_path) == []
    stale = struct_allowlist.problems("silent_patch_targets", found_leaks({"findings": []}), FIX, tmp_path)
    assert len(stale) == 1 and "STALE" in stale[0]
