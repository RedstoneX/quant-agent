"""The status board must never report a wrong status confidently.

The board exists because every hand-maintained status document in this repo
went stale — five wrong claims inside two days. Its value therefore rests
entirely on one property: **it would rather say `unknown` than say something
false.** These tests pin that property, and they pin the specific bug the very
first live run exposed, where a malformed rule masqueraded as documentation rot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "status_board.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_board", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules; register before exec
    sys.modules["status_board"] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load()


# --------------------------------------------------------------------------
# a bent ruler is not a broken system
# --------------------------------------------------------------------------

def test_prose_where_a_test_name_belongs_is_unknown_not_failure():
    """The bug the first real run found.

    `docs/phases.yaml` carried a `test_exists` rule whose `test` field held a
    sentence rather than a test identifier. The file it pointed at was
    perfectly fine, but the sentence was obviously not inside it, so the rule
    failed and the board announced Phase 1 as CONTRADICTED — documentation rot
    that did not exist.

    A rule the board cannot evaluate is a broken instrument. It reports
    `unknown` and says why. It never reports rot.
    """
    rule = {
        "kind": "test_exists",
        "path": "tests/test_status_board.py",
        "test": "test_status_board.py exists as a dedicated module (27 tests per the spec)",
    }
    result = sb.check_rule(rule, {})
    assert result.verdict == sb.UNKNOWN
    assert "malformed" in result.detail


def test_a_real_test_name_that_is_present_passes():
    rule = {
        "kind": "test_exists",
        "path": "tests/test_status_board.py",
        "test": "test_a_real_test_name_that_is_present_passes",
    }
    assert sb.check_rule(rule, {}).verdict == sb.PASS


def test_a_real_test_name_that_is_absent_fails():
    """A genuine absence must still fail — the malformed-rule guard must not
    become a blanket excuse that swallows real regressions.

    The absent name is assembled at runtime on purpose. Written as a literal it
    would appear in this very file, and the substring search would find it and
    pass — which is how this test failed the first time it ran.
    """
    absent = "test_" + "absent" + "_sentinel_" + "name"
    rule = {
        "kind": "test_exists",
        "path": "tests/test_status_board.py",
        "test": absent,
    }
    assert sb.check_rule(rule, {}).verdict == sb.FAIL


def test_an_unrecognised_rule_kind_is_unknown():
    assert sb.check_rule({"kind": "haruspicy"}, {}).verdict == sb.UNKNOWN


def test_a_manual_rule_is_unknown_never_pass():
    """`manual` means a human must look. It must never silently count as proof."""
    r = sb.check_rule({"kind": "manual", "note": "someone check the box"}, {})
    assert r.verdict == sb.UNKNOWN


# --------------------------------------------------------------------------
# settings rules
# --------------------------------------------------------------------------

def test_setting_equals_reads_nested_keys():
    cfg = {"risk": {"max_position_pct": 20}}
    r = sb.check_rule(
        {"kind": "setting_equals", "key": "risk.max_position_pct", "value": 20}, cfg
    )
    assert r.verdict == sb.PASS


def test_setting_equals_fails_on_a_changed_value():
    cfg = {"risk": {"max_position_pct": 35}}
    r = sb.check_rule(
        {"kind": "setting_equals", "key": "risk.max_position_pct", "value": 20}, cfg
    )
    assert r.verdict == sb.FAIL
    assert "35" in r.detail


def test_setting_equals_fails_when_the_key_is_gone():
    r = sb.check_rule(
        {"kind": "setting_equals", "key": "risk.max_position_pct", "value": 20}, {}
    )
    assert r.verdict == sb.FAIL


def test_missing_file_fails_rather_than_erroring():
    r = sb.check_rule({"kind": "file_exists", "path": "src/definitely_not_here.py"}, {})
    assert r.verdict == sb.FAIL


# --------------------------------------------------------------------------
# the verdict, which is the whole point
# --------------------------------------------------------------------------

def _phase(results):
    p = sb.PhaseView(id="x", title="t", summary="s", recorded="DONE AND LIVE",
                     confidence="high")
    p.results = results
    return p


def test_one_failing_rule_contradicts_the_whole_phase():
    """A phase is only as good as its weakest proof. One broken check is
    enough — the board must not average rot away."""
    p = _phase([
        sb.RuleResult("file_exists", sb.PASS, ""),
        sb.RuleResult("file_exists", sb.PASS, ""),
        sb.RuleResult("file_exists", sb.FAIL, ""),
    ])
    assert p.verdict == "CONTRADICTED"


def test_a_phase_with_nothing_checkable_is_unverified_not_confirmed():
    """The dangerous case: a phase whose evidence is entirely `manual`. It must
    never read as confirmed just because nothing disproved it."""
    p = _phase([
        sb.RuleResult("manual", sb.UNKNOWN, ""),
        sb.RuleResult("manual", sb.UNKNOWN, ""),
    ])
    assert p.verdict == "UNVERIFIED"


def test_unknowns_alongside_passes_do_not_block_confirmation():
    p = _phase([
        sb.RuleResult("file_exists", sb.PASS, ""),
        sb.RuleResult("manual", sb.UNKNOWN, ""),
    ])
    assert p.verdict == "CONFIRMED"
    assert p.unknown == 1


# --------------------------------------------------------------------------
# the shipped manifest must actually be evaluable
# --------------------------------------------------------------------------

def test_the_real_manifest_parses_and_every_rule_is_well_formed():
    """Guards the manifest itself. Every rule must be one the board understands
    and can evaluate — a rule it can only ever answer `unknown` to is dead
    weight dressed as evidence, and the one exception is `manual`, which is
    honest about needing a person."""
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    assert phases, "manifest carries no phases"

    known = {"commit_in_main", "pr_merged", "file_exists", "symbol_in_file",
             "test_exists", "setting_equals", "manual"}
    malformed = []
    for entry in phases:
        assert entry.get("id"), "every phase needs an id"
        assert entry.get("plain_summary"), f"{entry.get('id')} has no plain-English summary"
        for rule in entry.get("evidence") or []:
            kind = rule.get("kind")
            assert kind in known, f"{entry['id']}: unknown rule kind {kind!r}"
            if kind == "test_exists" and not sb._IDENTIFIER.match(str(rule.get("test", ""))):
                malformed.append((entry["id"], rule.get("test")))
            if kind == "symbol_in_file":
                assert rule.get("path"), f"{entry['id']}: symbol rule with no path"
    assert not malformed, f"test_exists rules carrying prose instead of a test name: {malformed}"


def test_every_phase_has_at_least_one_mechanical_rule():
    """A phase resting entirely on `manual` cannot be verified by the board, so
    it must be visible as such rather than quietly trusted."""
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    for entry in phases:
        rules = entry.get("evidence") or []
        mechanical = [r for r in rules if r.get("kind") != "manual"]
        assert mechanical, (
            f"{entry['id']} has only manual evidence — it can never be verified"
        )


def test_the_template_carries_every_placeholder_the_renderer_fills():
    """A renamed placeholder would silently ship a page with `{{SPEND}}` printed
    on it. Cheap to catch here."""
    template = (Path(__file__).resolve().parents[1]
                / "scripts" / "status_board_template.html").read_text()
    for key in ("{{STAMP}}", "{{DEPLOY}}", "{{CIRCUIT}}", "{{SPEND}}", "{{SPEND_PCT}}",
                "{{SPEND_NOTE}}", "{{SESSIONS}}", "{{ROWS}}", "{{ALARM}}",
                "{{RULES_TOTAL}}", "{{RULES_PASS}}", "{{RULES_FAIL}}",
                "{{RULES_UNKNOWN}}", "{{BOX_SHA}}", "{{MAIN_SHA}}"):
        assert key in template, f"template is missing {key}"


def test_rendered_output_leaves_no_placeholder_behind(tmp_path):
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.66,
             "sessions_today": 14, "box_sha": "abc123", "main_sha": "abc123"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render(phases, state, template)
    assert "{{" not in out, "an unfilled placeholder reached the rendered page"


def test_unknown_live_values_render_as_unknown_not_as_zero():
    """If the box cannot be read, the page must say so. A silent 0 would be a
    lie of exactly the kind this board exists to stop."""
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": None, "circuit": None, "spend_today": None,
             "sessions_today": None, "box_sha": None, "main_sha": None}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render(phases, state, template)
    assert "unknown" in out
    assert "$0.00" not in out
