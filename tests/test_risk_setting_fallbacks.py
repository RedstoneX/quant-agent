"""Every `_risk_setting` fallback in the pipeline must be the ratified default.

WHY THIS EXISTS
---------------
`TradingPipeline.__init__` wires the constructor's risk inputs with
`_risk_setting("<field>", <literal>)`. The literal is the value used when the
configured one is absent or is not a real number, and `_risk_setting`'s own
docstring calls it "the ratified default". Nothing enforced that.

On 2026-09-30 (board item 90) exactly one of the fifteen disagreed:
`min_stop_atr_multiple` still read 1.5 after the ratified base moved to 2.5
on 2026-09-10. 1.5 is not a stale-but-harmless neighbour of 2.5 — it is the
value this desk deliberately ABANDONED, a Sweeney MAE fit to its own
~two-week trade history, dropped both because that window's seat outputs
were later found to misreport confidence and data quality and because
fitting a threshold to past outcomes is barred outright
(`docs/OUTCOME.md`, "No arbitrary numbers, ever", the 2026-09-12
correction). A minimum stop distance decides both where an unbacked stop
sits and, through `shares = risk / |entry - stop|`, how large the resulting
position is.

WHAT IT DOES AND DOES NOT CLAIM
-------------------------------
The 1.5 was NOT reachable on the production path: a real `RiskConfig`
always carries the attribute and pydantic coerces the deployed YAML, so
`_risk_setting` returned the configured 2.5 every session. This test is
therefore a guard against a class of defect, not the repair of a live one,
and it is written down that way rather than oversold.

The ledger gate cannot cover this. `src/number_sources.py` names "fallback
arguments (`_risk_number(x, 25.0)`, `kwargs.get(k, 25.0)`)" among the shapes
it structurally cannot scan, and suggests hoisting each literal to a named
constant. That would work but would add a ledger row — and therefore a new
number to account for — per fallback. Pinning each fallback to the field
default it claims to mirror closes the same hole and introduces no number
at all: the assertion is an equality between two things already in the tree.

Fields whose `RiskConfig` declaration has no default (`max_position_pct` is
required and comes from `config/settings.yaml`) cannot be checked this way.
They are listed explicitly below rather than skipped silently, so a field
that stops having a default cannot quietly leave the check.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic_core import PydanticUndefined

from src.config import RiskConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = REPO_ROOT / "src" / "pipeline.py"

#: Fields wired through `_risk_setting` whose `RiskConfig` declaration is
#: REQUIRED (no default), so there is no ratified default to compare the
#: fallback against. Named, not skipped by silence.
NO_FIELD_DEFAULT = {"max_position_pct"}

#: `_risk_setting("<name>", <number>)`, the `allow_zero=` keyword ignored.
_CALL = re.compile(r"_risk_setting\(\s*\"(\w+)\",\s*([0-9]+(?:\.[0-9]+)?)")


def _fallbacks() -> list[tuple[str, float]]:
    return [
        (name, float(literal))
        for name, literal in _CALL.findall(PIPELINE.read_text())
    ]


def test_the_block_is_still_here() -> None:
    """A rename that emptied this check would otherwise pass it silently."""
    found = _fallbacks()
    assert len(found) >= 10, (
        "Fewer than ten `_risk_setting` fallbacks found in src/pipeline.py "
        f"(found {len(found)}). Either the helper was renamed or the wiring "
        "block moved; this test then asserts nothing. Update the regex."
    )
    names = {name for name, _ in found}
    assert "min_stop_atr_multiple" in names, (
        "`min_stop_atr_multiple` is no longer wired through `_risk_setting`. "
        "That is the field this test was written for (board item 90); if the "
        "wiring genuinely changed, retarget the test rather than deleting it."
    )


def test_every_fallback_equals_its_ratified_field_default() -> None:
    mismatches = []
    unchecked = []
    for name, literal in _fallbacks():
        field = RiskConfig.model_fields.get(name)
        if field is None:
            mismatches.append(
                f"{name}: wired through `_risk_setting` but not a "
                "`RiskConfig` field at all",
            )
            continue
        default = field.default
        if default is PydanticUndefined or default is None:
            unchecked.append(name)
            continue
        if float(default) != literal:
            mismatches.append(
                f"{name}: src/pipeline.py falls back to {literal} but the "
                f"ratified RiskConfig default is {default}",
            )

    assert not mismatches, (
        "A `_risk_setting` fallback disagrees with the ratified default it "
        "claims to mirror. The fallback is what the desk sizes on whenever "
        "the configured value is absent or is not a real number, so the two "
        "must not drift:\n  " + "\n  ".join(mismatches)
    )
    assert set(unchecked) == NO_FIELD_DEFAULT, (
        "The set of `_risk_setting` fields with no RiskConfig default "
        f"changed: expected {sorted(NO_FIELD_DEFAULT)}, found "
        f"{sorted(unchecked)}. A field that gains a default should be "
        "checked; one that loses a default should be named here on purpose."
    )
