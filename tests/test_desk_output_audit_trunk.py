"""The desk-output audit measures the allow-listed files against `origin/main`.

Two properties every trunk-comparing guard must hold (docs/GUARDS_WITHOUT_STORED_STATE.md):
it REFUSES when the trunk cannot be read, and it compares identities, never totals.
"""

from __future__ import annotations

import pytest

from tests import desk_output_audit as audit_mod
from tests import desk_output_guard as guard

PROJECT_ROOT = guard.PROJECT_ROOT


def test_the_audit_refuses_when_the_trunk_cannot_be_read(monkeypatch) -> None:
    """No trunk, no verdict: an unreadable origin/main is never read as 'empty'."""
    from scripts.guard_reference import ReferenceUnavailable

    def gone(paths):
        raise ReferenceUnavailable("origin/main unreadable (test)")

    monkeypatch.setattr(audit_mod, "trunk_blobs", gone)
    with pytest.raises(ReferenceUnavailable):
        audit_mod.audit_repo()


def test_swapping_one_real_value_for_another_is_growth(monkeypatch) -> None:
    """Same count, different content, still fails: identities, never totals."""
    path = "tests/fixtures/holding_why_rsg_20260917.json"
    text = guard.read_text(PROJECT_ROOT / path)
    assert text is not None
    live = guard.scan_text(text, path)
    assert live, "the specimen file no longer trips the guard"
    swapped = text.replace(live[0].excerpt[:40], live[0].excerpt[:40].swapcase(), 1)
    assert swapped != text

    real = audit_mod.trunk_blobs

    def trunk_with_swap(paths):
        blobs = real(paths)
        blobs[path] = swapped
        return blobs

    monkeypatch.setattr(audit_mod, "trunk_blobs", trunk_with_swap)
    audit = audit_mod.audit_repo()
    assert path in audit.grown, audit.grown
    assert any(f.path == path for f in audit.grown_findings)


