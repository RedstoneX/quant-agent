"""A recording's run id may not arrive through a silent default getattr.

The settlement-fill guard's supplier check, pointed at `run_id`, must find
no three-argument getattr in production code, and a loud read must raise on
a missing attribute while tolerating an absent source object.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.settlement_fill_guard import _is_subject, offending_sites
from src.recording_accessors import pinned_evidence

ROOT = Path(__file__).resolve().parent.parent


def _silent_getattr_sites():
    found = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if not _is_subject(rel) or rel.startswith((".venv/", "src/storage/")):
            continue
        found += [
            s for s in offending_sites(path.read_text(encoding="utf-8"), rel, {"run_id"})
            if s[3] == "silent_default_getattr"
        ]
    return found


def test_no_run_id_recording_uses_a_silent_default_getattr():
    assert _silent_getattr_sites() == []


def test_the_check_goes_red_on_the_unsafe_shape():
    bad = "f(run_id=getattr(ctx, 'run_id', None))\n"
    assert offending_sites(bad, "x.py", {"run_id"})


def test_loud_read_raises_on_missing_attribute_and_tolerates_absent_source():
    assert pinned_evidence(None, "run_id") is None
    assert pinned_evidence(SimpleNamespace(run_id="synthetic-1"), "run_id") == "synthetic-1"
    with pytest.raises(AttributeError):
        pinned_evidence(SimpleNamespace(), "run_id")
