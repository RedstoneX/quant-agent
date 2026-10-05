"""Self-proof and ratchet for the boundary harness (conversion step 2)."""
import ast, sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import (  # noqa: E402
    ROOT, check_boundary, test_files_referencing_pipeline, trunk_test_files_referencing_pipeline,
)
from scripts.guard_reference import ReferenceUnavailable, TRUNK, added_sites  # noqa: E402

# No stored baseline. This file used to carry `TRADING_PIPELINE_TEST_FILE_BASELINE = 79`,
# a count measured on 2026-10-01 -- a cached measurement every change that dropped a
# file had to edit, and a TOTAL, so removing one coupled test and adding a different
# one netted to zero. Per docs/GUARDS_WITHOUT_STORED_STATE.md the ratchet now names
# every coupled test file in the working tree, names them again on origin/main at
# check time, and fails on any IDENTITY (path) the tree holds that the trunk does not.
# Removals never fail; the trunk cannot be read -> the check REFUSES, never passes.


def _composed_mixin_modules():
    tree = ast.parse((ROOT / "src/pipeline.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TradingPipeline")
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


def _shim_only(module):
    """True when every method of every mixin class in the module is a thin shim (docstring + one return): all bodies lifted."""
    tree = ast.parse((ROOT / (module.replace(".", "/") + ".py")).read_text())
    return all(len(f.body) <= 2 and isinstance(f.body[-1], ast.Return) for c in tree.body if isinstance(c, ast.ClassDef) and c.name.endswith("Mixin") for f in c.body if isinstance(f, ast.FunctionDef))


def test_harness_passes_pipeline_sizing():
    v = check_boundary("src.pipeline_sizing")
    assert v.passed, v.failures


@pytest.mark.parametrize("name,module", sorted(MIXINS.items()))
def test_harness_fails_every_composed_mixin(name, module):
    v = check_boundary(module)
    assert not v.passed, f"{name} must not be a boundary"
    assert 1 in v.failures  # no __init__
    assert 2 in v.failures or _shim_only(module), f"{name} keeps bodies yet reads no foreign self attrs"


def test_eight_mixins_are_covered(): assert len(MIXINS) == 8  # noqa: E704


def test_clause_2_catches_foreign_self_reads(tmp_path, monkeypatch):
    import boundary_harness as h
    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.py").write_text(
        "class M:\n    def __init__(self, a):\n        self.a = a\n"
        "    def go(self):\n        return self.a + self.other()\n")
    monkeypatch.setattr(h, "ROOT", tmp_path)
    v = h.check_boundary("src.m", tests_dir=tmp_path)
    assert 2 in v.failures and 1 not in v.failures


def pipeline_coupling_violations(now=None, before=None):
    """Every test file newly naming TradingPipeline against the trunk."""
    now = test_files_referencing_pipeline() if now is None else now
    before = trunk_test_files_referencing_pipeline() if before is None else before
    return [path for path, _n, _was in added_sites(now, before)]


def test_ratchet_no_new_test_file_couples_to_trading_pipeline():
    try:
        bad = pipeline_coupling_violations()
    except ReferenceUnavailable as exc:
        pytest.fail(f"pipeline-import ratchet refused: {exc}")
    assert not bad, (
        f"{len(bad)} test file(s) name TradingPipeline and did not on {TRUNK}: {bad}. "
        "Build the pipeline through tests/pipeline_factory.build_pipeline or test the "
        "service in isolation; the set of coupled test files may only shrink."
    )


def test_ratchet_fails_a_new_offender_and_the_net_zero_swap():
    before = {"tests/test_a.py", "tests/test_b.py"}
    assert pipeline_coupling_violations(before, before) == []
    assert pipeline_coupling_violations(set(), before) == []  # removals never fail
    assert pipeline_coupling_violations(before | {"tests/test_c.py"}, before) == ["tests/test_c.py"]
    # Net-zero swap: a count of 2 vs 2 would pass this; the identity does not.
    assert pipeline_coupling_violations({"tests/test_a.py", "tests/test_c.py"}, before) == ["tests/test_c.py"]


def test_ratchet_refuses_without_the_trunk(monkeypatch):
    import boundary_harness as h
    monkeypatch.setattr(h, "trunk_paths", None, raising=False)
    import scripts.guard_reference as g
    def gone(*a, **k):
        raise ReferenceUnavailable("no origin/main")
    monkeypatch.setattr(g, "require_trunk", gone)
    with pytest.raises(ReferenceUnavailable):
        trunk_test_files_referencing_pipeline()


def test_pipeline_holds_the_review_part_and_inherits_no_review_mixin():
    """The last owed boundary (docs/SPLIT_DEFERRED_FINDINGS.md): `TradingPipeline`'s MRO carries no
    `PromptFactsReviewMixin`; the seat builds one `PromptFactsReview` PER CALL from its own
    collaborators (no host is handed to the part, nothing is cached on the seat), so a collaborator
    swapped on the seat between calls is the one the next part is built from."""
    import src.pipeline_prompt_facts_review as shim_module
    from src.pipeline import TradingPipeline
    from src.pipeline_prompt_facts_review import DELEGATED, HOST_COLLABORATORS, review_of
    from src.prompt_facts.review.held import COLLABORATORS, PromptFactsReview
    assert not hasattr(shim_module, "PromptFactsReviewMixin") and not hasattr(shim_module, "HOLDER_ATTR")
    assert not any("Review" in c.__name__ for c in TradingPipeline.__mro__)
    assert PromptFactsReview not in TradingPipeline.__mro__
    assert tuple(HOST_COLLABORATORS.values()) == COLLABORATORS  # every seat name maps onto a part keyword
    from src.pipeline_prompt_facts import PromptFactsMixin
    pipe = object.__new__(PromptFactsMixin)  # the seat that holds the part; no pipeline is built
    first, second = review_of(pipe), review_of(pipe)
    assert isinstance(first, PromptFactsReview) and first is not second  # rebuilt per call, never cached
    assert not hasattr(first, "_host") and "_prompt_facts_review" not in vars(pipe)
    assert all(getattr(TradingPipeline, name)._held_delegate == name for name in DELEGATED)
    pipe._build_post_exit_reality = lambda *a, **k: "SWAPPED ON THE SEAT"
    assert pipe._review_grading()._build_post_exit_reality() == "SWAPPED ON THE SEAT"
    pipe.db = object()
    assert pipe._review_blocked().db is pipe.db  # the next call is built from the seat's current db
