"""Self-proof and ratchet for the boundary harness (conversion step 2)."""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import ROOT, check_boundary, count_test_files_importing_pipeline  # noqa: E402

# Measured 2026-10-01 by AST (code references only). May only go DOWN.
TRADING_PIPELINE_TEST_FILE_BASELINE = 89


def _composed_mixin_modules():
    tree = ast.parse((ROOT / "src/pipeline.py").read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "TradingPipeline")
    bases = {b.id for b in cls.bases if isinstance(b, ast.Name)}
    mods = {}
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("src.pipeline_"):
            for a in n.names:
                if a.name in bases:
                    mods[a.name] = n.module
    assert set(mods) == bases, "could not locate every composed mixin"
    return mods


MIXINS = _composed_mixin_modules()


def test_harness_passes_pipeline_sizing():
    v = check_boundary("src.pipeline_sizing")
    assert v.passed, v.failures


@pytest.mark.parametrize("name,module", sorted(MIXINS.items()))
def test_harness_fails_every_composed_mixin(name, module):
    v = check_boundary(module)
    assert not v.passed, f"{name} must not be a boundary"
    assert 1 in v.failures and 2 in v.failures  # no __init__, foreign self attrs


def test_eight_mixins_are_covered():
    assert len(MIXINS) == 8


def test_clause_2_catches_foreign_self_reads(tmp_path, monkeypatch):
    import boundary_harness as h
    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.py").write_text(
        "class M:\n    def __init__(self, a):\n        self.a = a\n"
        "    def go(self):\n        return self.a + self.other()\n")
    monkeypatch.setattr(h, "ROOT", tmp_path)
    v = h.check_boundary("src.m", tests_dir=tmp_path)
    assert 2 in v.failures and 1 not in v.failures


def test_ratchet_trading_pipeline_test_files_may_only_go_down():
    n = count_test_files_importing_pipeline()
    assert n <= TRADING_PIPELINE_TEST_FILE_BASELINE, (
        f"{n} test files import TradingPipeline, baseline "
        f"{TRADING_PIPELINE_TEST_FILE_BASELINE}: the count may only go down")
    if n < TRADING_PIPELINE_TEST_FILE_BASELINE:
        pytest.fail(f"count fell to {n}: lower the baseline to {n} to lock it in")
