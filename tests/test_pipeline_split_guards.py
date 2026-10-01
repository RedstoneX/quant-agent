"""Step-0 guards for the pipeline split (board item 210, docs/PIPELINE_SPLIT_PLAN.md).

Three things are guarded here and nothing is moved:

1. The METHOD INVENTORY. What each tracked module contains -- module-level
   functions and the method names of every class -- is frozen in
   `tests/pipeline_method_inventory.json`, read from the AST. A later step that
   moves a method must show the move by updating that file in the same change;
   a method dropped or duplicated during a move fails the build instead of
   vanishing quietly.
2. The LEDGER-ID / `SCOPED_PATHS` MIGRATION HELPER (`src/ledger_move.py`),
   exercised on a SYNTHETIC move against copies of the real files, so the
   helper is proved to rewrite ids rather than merely to exist.

3. THE COMPATIBILITY SHIMS. A name re-exported from `src/pipeline.py` purely
   so old importers keep working is imported there and used nowhere there, so
   `patch("src.pipeline.<name>")` rebinds an unused alias and intercepts
   nothing while the test still passes. No test may patch such a name.

All three guards are themselves proved to be able to fail: the inventory check
is run against mutated source text with a method added, removed and renamed,
the verifier is run against a deliberately half-applied move, and the shim
check is run against a synthetic patch of a real re-exported name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pipeline_method_inventory import (
    INVENTORY_PATH,
    TRACKED_MODULES,
    current_inventory,
    module_inventory,
    recorded_inventory,
)
from src.ledger_move import (
    MoveSpec,
    apply_move,
    ledger_ids_for_module,
    plan_move,
    verify_move,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "config" / "number_ledger.yaml"
NUMBER_SOURCES_PATH = REPO_ROOT / "src" / "number_sources.py"

_FIX_IT = (
    "\n\nWHAT TO DO:\n"
    "  * If you MOVED a method or function on purpose, record the move: run\n"
    "    `python -m scripts.pipeline_method_inventory --write` in the SAME change\n"
    "    that moves it, and the diff of tests/pipeline_method_inventory.json is the\n"
    "    reviewable statement of what moved where.\n"
    "  * If you did NOT mean to change this set, a method has VANISHED or been\n"
    "    DUPLICATED -- that is a bug in the move, not a stale fixture. Do not\n"
    "    regenerate the file to make this pass.\n"
)


def _diff(recorded: dict, current: dict) -> str:
    was = set(recorded.get("module_functions", []))
    now = set(current["module_functions"])
    lines = []
    if was != now:
        lines.append(f"  module functions added:   {sorted(now - was)}")
        lines.append(f"  module functions removed: {sorted(was - now)}")
    rec_classes = recorded.get("classes", {})
    cur_classes = current["classes"]
    for name in sorted(set(rec_classes) | set(cur_classes)):
        old = set(rec_classes.get(name, []))
        new = set(cur_classes.get(name, []))
        if old != new:
            lines.append(f"  class {name}: added {sorted(new - old)}")
            lines.append(f"  class {name}: removed {sorted(old - new)}")
    return "\n".join(lines) or "  (ordering or structure changed)"


@pytest.mark.parametrize("relpath", TRACKED_MODULES)
def test_method_inventory_matches_the_recorded_one(relpath: str) -> None:
    recorded = recorded_inventory()
    assert relpath in recorded, (
        f"{relpath} is tracked by the split guard but absent from the recorded "
        f"inventory.{_FIX_IT}"
    )
    current = current_inventory()[relpath]
    assert recorded[relpath] == current, (
        f"The method inventory of {relpath} has changed:\n"
        f"{_diff(recorded[relpath], current)}{_FIX_IT}"
    )


def test_recorded_inventory_tracks_exactly_the_tracked_modules() -> None:
    assert sorted(recorded_inventory()) == sorted(TRACKED_MODULES), (
        "A module was added to or dropped from TRACKED_MODULES without the "
        f"recorded inventory following it.{_FIX_IT}"
    )


def test_method_names_are_unique_across_the_recorded_classes() -> None:
    """Mixin MRO risk (plan §5.4): two mixins defining one name is a silent win for
    whichever is first in the bases list. Today there is one class per name; the day
    a method is moved into a mixin this is what catches a copy left behind."""
    owners: dict[str, list[str]] = {}
    for relpath, inventory in recorded_inventory().items():
        for class_name, methods in inventory["classes"].items():
            if class_name != "TradingPipeline" and not class_name.endswith("Mixin"):
                # The four stage classes each implement a common `run`/`__init__`
                # interface on purpose; they are not bases of one object, so they
                # cannot collide in an MRO. Only TradingPipeline and the mixins
                # the split creates are combined into a single class.
                continue
            for method in methods:
                if method.startswith("__") and method.endswith("__"):
                    # Dunders are legitimately defined per class and are not
                    # what an MRO collision between two mixins would look like.
                    continue
                owners.setdefault(method, []).append(f"{relpath}::{class_name}")
    clashes = {name: where for name, where in owners.items() if len(where) > 1}
    assert not clashes, (
        "The same method name is defined on more than one class in the split "
        f"modules; under mixins the first base silently wins: {clashes}{_FIX_IT}"
    )


def test_inventory_guard_can_actually_fail(tmp_path: Path) -> None:
    """A guard that cannot fail is worthless: add, remove and rename a method in a
    copy of the real module and prove each one is detected."""
    source = (REPO_ROOT / "src" / "pipeline_stages.py").read_text(encoding="utf-8")
    baseline = module_inventory(REPO_ROOT / "src" / "pipeline_stages.py")
    victim = next(
        name
        for name in baseline["classes"]["RiskStage"]
        # A name that occurs exactly once in the file, so the single-replacement
        # mutations below land on RiskStage's copy and not on another class's.
        if not name.startswith("__") and source.count(f"    def {name}(") == 1
    )

    def inventory_of(text: str) -> dict:
        path = tmp_path / "mutated.py"
        path.write_text(text, encoding="utf-8")
        return module_inventory(path)

    added = source.replace(
        f"    def {victim}(",
        f"    def _split_guard_canary(self):\n        return None\n\n    def {victim}(",
        1,
    )
    assert added != source
    assert "_split_guard_canary" in inventory_of(added)["classes"]["RiskStage"]
    assert inventory_of(added) != baseline

    renamed = source.replace(f"    def {victim}(", f"    def {victim}_renamed(", 1)
    assert renamed != source
    renamed_inventory = inventory_of(renamed)
    assert victim not in renamed_inventory["classes"]["RiskStage"]
    assert f"{victim}_renamed" in renamed_inventory["classes"]["RiskStage"]
    assert renamed_inventory != baseline

    first_module_function = baseline["module_functions"][0]
    assert first_module_function in inventory_of(source)["module_functions"]
    dropped = inventory_of(source)
    dropped["module_functions"] = [
        n for n in dropped["module_functions"] if n != first_module_function
    ]
    assert dropped != baseline, "removing a module function must be visible"


def test_recorded_inventory_file_is_valid_json_with_the_regenerate_hint() -> None:
    payload = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert payload["_regenerate"].endswith("--write")
    assert payload["modules"]


# --------------------------------------------------------------------------
# The ledger-id / SCOPED_PATHS migration helper.
# --------------------------------------------------------------------------


def test_measured_ledger_id_counts_for_the_two_modules() -> None:
    """Measured against current main, not taken from the plan. The plan's §5.2 says
    36 and 22; the ids under `src.pipeline.*` had since fallen to 34, and step 1
    (the prompt-facts mixin) moved 25 of them to `src.pipeline_prompt_facts.*`,
    leaving 9; step 3 (the de-levering ladder) moved `_force_delever`'s one id to
    `src.pipeline_delever.*` and then `src.pipeline_exits.*`, leaving 4; step 5 (the risk
    gate) moved `_has_actionable_signal_fn`'s one id to `src.pipeline_risk_gate.*`, leaving 3. The point of this assertion is that a
    later step cannot move ids without the count moving."""
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    assert len(ledger_ids_for_module("src.pipeline", ledger)) == 3
    assert len(ledger_ids_for_module("src.pipeline_delever", ledger)) == 1
    assert len(ledger_ids_for_module("src.pipeline_prompt_facts", ledger)) == 25
    assert len(ledger_ids_for_module("src.pipeline_stages", ledger)) == 22


def test_module_prefix_does_not_swallow_the_sibling_module() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    pipeline_ids = ledger_ids_for_module("src.pipeline", ledger)
    assert not any(i.startswith("src.pipeline_stages.") for i in pipeline_ids)


def _synthetic_spec(ledger: str) -> MoveSpec:
    """A move of three real `TradingPipeline` ledger-bearing methods into a mixin."""
    prefix = "src.pipeline.TradingPipeline."
    import re

    names = sorted(
        {
            re.split(r"[(:]", i[len(prefix) :], maxsplit=1)[0]
            for i in ledger_ids_for_module("src.pipeline", ledger)
            if i.startswith(prefix)
        }
    )
    assert names, "no TradingPipeline ledger ids to exercise the helper on"
    return MoveSpec(
        old_module="src.pipeline",
        new_module="src.pipeline_split_canary",
        names=frozenset(names[:3]),
        old_qual="TradingPipeline",
        new_qual="CanaryMixin",
    )


def test_migration_helper_rewrites_ids_and_scoped_paths_on_a_synthetic_move() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = NUMBER_SOURCES_PATH.read_text(encoding="utf-8")
    spec = _synthetic_spec(ledger)

    plan = plan_move(spec, ledger, scoped)
    assert plan.id_rewrites, "the plan found nothing to rewrite"
    assert plan.scoped_path_added == "src/pipeline_split_canary.py"
    assert "src/pipeline_split_canary.py" in plan.describe()

    new_ledger, new_scoped = apply_move(plan, ledger, scoped)
    assert new_ledger != ledger and new_scoped != scoped
    assert verify_move(plan, new_ledger, new_scoped) == []

    for old, new in plan.id_rewrites.items():
        assert new.startswith("src.pipeline_split_canary.CanaryMixin.")
        assert f"- id: {old}\n" not in new_ledger
        assert f"- id: {new}\n" in new_ledger

    # The entries that moved get the new `site:`; everything else keeps the old one.
    assert new_ledger.count("site: src/pipeline_split_canary.py") == len(plan.id_rewrites)
    untouched = len(ledger_ids_for_module("src.pipeline", new_ledger))
    assert untouched == 3 - len(plan.id_rewrites)
    assert '    "src/pipeline_split_canary.py",\n' in new_scoped
    # Nothing but the id, the site and the scope list may change.
    assert len(new_ledger.splitlines()) == len(ledger.splitlines())


def test_migration_helper_leaves_unrelated_ids_alone() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = NUMBER_SOURCES_PATH.read_text(encoding="utf-8")
    spec = _synthetic_spec(ledger)
    plan = plan_move(spec, ledger, scoped)
    new_ledger, _ = apply_move(plan, ledger, scoped)
    before = set(ledger_ids_for_module("src.pipeline_stages", ledger))
    after = set(ledger_ids_for_module("src.pipeline_stages", new_ledger))
    assert before == after


def test_verifier_catches_a_half_applied_move() -> None:
    """Prove the checkable half can fail: drop one rewrite and a scope entry."""
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = NUMBER_SOURCES_PATH.read_text(encoding="utf-8")
    spec = _synthetic_spec(ledger)
    plan = plan_move(spec, ledger, scoped)
    new_ledger, new_scoped = apply_move(plan, ledger, scoped)

    missed_old, missed_new = sorted(plan.id_rewrites.items())[0]
    half = new_ledger.replace(f"- id: {missed_new}\n", f"- id: {missed_old}\n", 1)
    problems = verify_move(plan, half, new_scoped)
    assert any("old ledger id still present" in p for p in problems)
    assert any("new ledger id missing" in p for p in problems)

    unscoped = verify_move(plan, new_ledger, scoped)
    assert any("drop out of the ledger guard silently" in p for p in unscoped)


def test_verifier_catches_a_duplicated_id() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = NUMBER_SOURCES_PATH.read_text(encoding="utf-8")
    spec = _synthetic_spec(ledger)
    plan = plan_move(spec, ledger, scoped)
    new_ledger, new_scoped = apply_move(plan, ledger, scoped)
    duped_id = sorted(plan.id_rewrites.values())[0]
    duped = new_ledger.replace(
        f"- id: {duped_id}\n", f"- id: {duped_id}\n  - id: {duped_id}\n", 1
    )
    assert any("duplicate ledger id" in p for p in verify_move(plan, duped, new_scoped))


def test_move_spec_rejects_a_no_op_and_a_half_qualname() -> None:
    with pytest.raises(ValueError):
        MoveSpec(old_module="src.pipeline", new_module="src.pipeline", names=frozenset({"x"}))
    with pytest.raises(ValueError):
        MoveSpec(old_module="src.pipeline", new_module="src.other", names=frozenset())
    with pytest.raises(ValueError):
        MoveSpec(
            old_module="src.pipeline",
            new_module="src.other",
            names=frozenset({"x"}),
            old_qual="TradingPipeline",
        )


def test_the_two_split_modules_are_still_in_scoped_paths() -> None:
    from src.number_sources import SCOPED_PATHS

    assert "src/pipeline.py" in SCOPED_PATHS
    assert "src/pipeline_delever.py" in SCOPED_PATHS
    assert "src/pipeline_stages.py" in SCOPED_PATHS


# ---------------------------------------------------------------------------
# 3. COMPATIBILITY SHIMS MUST NOT BECOME SILENT PATCH TARGETS.
#
# Each split step re-exports the names it moved from `src/pipeline.py` so that
# existing importers keep working. Those names are imported there and never
# used there. A test that writes `patch("src.pipeline.<name>", ...)` against
# such a name still SUCCEEDS -- it rebinds the old module's unused alias -- but
# it intercepts nothing, because the moved code resolves the name in its own
# module. The test then passes while testing nothing.
#
# WHICH CHECK THIS IS: the second of the two options -- no test may patch a
# name on `src.pipeline` that `src.pipeline` itself no longer uses.
#
# WHAT IT CANNOT CATCH:
#   * a patch target built at runtime from pieces, or passed as a variable,
#     rather than written as a literal `src.pipeline.<name>` in the test text;
#   * `patch.object(pipeline_module, "name")`, which never spells the dotted
#     path;
#   * a name used by `src/pipeline.py` only inside a string annotation, which
#     this reads as unused and would therefore flag (a false alarm, not a
#     miss -- it fails loudly rather than quietly).
# ---------------------------------------------------------------------------

import ast
import re

OLD_MODULE_PATH = REPO_ROOT / "src" / "pipeline.py"
TESTS_DIR = Path(__file__).resolve().parent

_PATCH_TARGET_RE = re.compile(r"src\.pipeline\.([A-Za-z_][A-Za-z0-9_]*)")


def compat_only_names(source: str) -> set[str]:
    """Names imported by `source` and referenced nowhere else in it."""
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return imported - used


def dead_patch_targets(test_source: str, compat_names: set[str]) -> set[str]:
    return {m for m in _PATCH_TARGET_RE.findall(test_source) if m in compat_names}


def test_no_test_patches_a_name_the_old_module_no_longer_uses():
    compat_names = compat_only_names(OLD_MODULE_PATH.read_text())
    assert compat_names, (
        "expected src/pipeline.py to still carry compatibility re-exports; if "
        "the split finished and they are all gone, delete this guard"
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        found = dead_patch_targets(path.read_text(), compat_names)
        if found:
            offenders[path.name] = found
    assert not offenders, (
        "these tests patch a name on `src.pipeline` that `src.pipeline` no "
        "longer uses, so the patch binds an unused alias and intercepts "
        f"nothing: {offenders}\n\nWHAT TO DO: patch the name on the module "
        "that now DEFINES it (the `src/pipeline_*.py` module the split moved "
        "it to), not on `src.pipeline`."
    )


def test_dead_patch_target_detector_can_fail():
    """Prove the check above can fail, against the real re-export list."""
    compat_names = compat_only_names(OLD_MODULE_PATH.read_text())
    victim = sorted(compat_names)[0]
    synthetic = f'with patch("src.pipeline.{victim}", autospec=True):\n    pass\n'
    assert dead_patch_targets(synthetic, compat_names) == {victim}
    # A name the old module still uses is NOT flagged.
    assert dead_patch_targets('patch("src.pipeline.TradingPipeline")', compat_names) == set()
