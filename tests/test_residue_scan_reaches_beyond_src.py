"""The residue scan must bite in scripts, ops and the repo root."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("where", ["scripts", "ops/research", "root"])
def test_residue_planted_outside_src_is_caught(where, monkeypatch, tmp_path) -> None:
    """The scan must bite in scripts, ops and the repo root, not only src."""
    import tests.test_deleted_mechanisms_leave_no_residue as mod

    for d in ("src", "config/prompts", "scripts", "ops"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    target = tmp_path / ("planted.py" if where == "root" else f"{where}/planted.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# STOP_SANITY_FLOOR_FRACTION still applies\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "SEARCH_ROOTS", tuple(tmp_path / d for d in ("src", "config/prompts", "scripts", "ops")))
    assert target in mod._files()
    mech = mod._DELETED[1]
    with pytest.raises(AssertionError):
        mod.test_a_deleted_mechanism_is_not_described_as_live(mech)
    target.unlink()
    mod.test_a_deleted_mechanism_is_not_described_as_live(mech)
