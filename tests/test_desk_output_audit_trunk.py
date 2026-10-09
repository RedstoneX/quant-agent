"""The desk-output audit measures allow-listed files against a FIXED per-file list.

config/check_allowlists/struct_desk_output.txt names every finding the allow-listed
files carry (`path | signal | hash of the excerpt`). Nothing is read from any trunk.
A finding not in the list fails (identity, never a total), and a listed finding that
no longer occurs is a stale entry. Detection itself is unchanged.
"""

from __future__ import annotations

from scripts import struct_allowlist
from tests import desk_output_audit as audit_mod
from tests import desk_output_guard as guard

PROJECT_ROOT = guard.PROJECT_ROOT
SPECIMEN = "tests/fixtures/holding_why_rsg_20260917.json"


def _specimen_keys() -> list[str]:
    text = guard.read_text(PROJECT_ROOT / SPECIMEN)
    assert text is not None
    live = guard.scan_text(text[: audit_mod.SCAN_BYTE_CAP], SPECIMEN)
    assert live, "the specimen file no longer trips the guard"
    return [audit_mod.finding_key(f) for f in live]


def _write(tmp_path, entries):
    struct_allowlist.write("desk_output", entries, tmp_path)
    return tmp_path


def _all_keys_but(drop_one_from: str | None = None) -> list[str]:
    keys = struct_allowlist.load("desk_output")
    if drop_one_from:
        keys.remove(next(k for k in keys if k.startswith(drop_one_from + " | ")))
    return keys


def test_todays_trunk_matches_the_fixed_list() -> None:
    audit = audit_mod.audit_repo()
    assert not audit.grown and not audit.stale_entries, (audit.grown, audit.stale_entries)


def test_a_finding_missing_from_the_list_is_growth(tmp_path) -> None:
    """Same file, one finding the list does not name: still fails."""
    audit = audit_mod.audit_repo(directory=_write(tmp_path, _all_keys_but(SPECIMEN)))
    assert SPECIMEN in audit.grown, audit.grown
    assert any(f.path == SPECIMEN for f in audit.grown_findings)


def test_swapping_one_real_value_for_another_is_growth(tmp_path) -> None:
    """Same count, different content, still fails: identities, never totals."""
    keys = _all_keys_but()
    victim = next(k for k in keys if k.startswith(SPECIMEN + " | "))
    keys[keys.index(victim)] = victim.rsplit(" | ", 1)[0] + " | " + "0" * 12
    audit = audit_mod.audit_repo(directory=_write(tmp_path, keys))
    assert SPECIMEN in audit.grown, audit.grown
    assert any("0" * 12 in k for k in audit.stale_entries), audit.stale_entries


def test_a_listed_finding_that_no_longer_occurs_is_stale(tmp_path) -> None:
    extra = f"{SPECIMEN} | broker-order-id | {'f' * 12}"
    audit = audit_mod.audit_repo(directory=_write(tmp_path, [*_all_keys_but(), extra]))
    assert extra in audit.stale_entries, audit.stale_entries
    assert not audit.grown


def test_the_list_never_carries_the_raw_excerpt() -> None:
    """The list is committed to a PUBLIC repo: it holds hashes, not the values."""
    excerpts = {
        f.excerpt
        for k in _specimen_keys()
        for f in guard.scan_text(guard.read_text(PROJECT_ROOT / SPECIMEN) or "", SPECIMEN)
    }
    body = "\n".join(struct_allowlist.load("desk_output"))
    assert not any(e and e in body for e in excerpts)
