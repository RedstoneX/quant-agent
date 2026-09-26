"""Board item 107 — prompt drift the deletion-site check cannot see.

Three separate guarantees, tested separately because they fail separately:

  * a bound mechanism cannot change without the prose describing it being
    re-read (`src/prompt_bindings.py`);
  * every one of the ten standing sheets is covered by the limits check,
    not the two that happened to be wired up
    (`src/agents/prompt_limits.audit_prompt_coverage`);
  * the drift flag's weight threshold has one definition site, and the one
    site that still types it is named here rather than being quietly
    tolerated.

No test here makes a model call.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from src import prompt_bindings
from src.agents.prompt_limits import (
    PROMPT_NAMESPACES,
    PromptPlaceholderError,
    audit_prompt_coverage,
    placeholders_in,
    render_prompt_limits,
)
from src.risk.metrics import DRIFT_PNL_PCT, DRIFT_WEIGHT_PCT, drift_flag

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "config" / "prompts"


# ---------------------------------------------------------------------------
# (a) the behaviour-change registry
# ---------------------------------------------------------------------------

def test_the_live_registry_agrees_with_the_live_tree():
    """The whole point: a red build here means somebody changed a mechanism
    and has not yet re-read the sentences that describe it."""
    problems = prompt_bindings.check(REPO_ROOT)
    assert problems == [], "\n\n".join(problems)


def test_the_registry_is_not_empty():
    """An empty registry passes while checking nothing — the failure mode
    that makes a green build a lie."""
    bindings = prompt_bindings.load_registry()
    assert len(bindings) >= 2
    names = {b.name for b in bindings}
    assert "position_drift_flag" in names
    assert "constructor_stop_widening" in names


def _tiny_tree(tmp_path: Path, *, behaviour: str, prose: str) -> Path:
    """A two-file repo: one module, one prompt sheet, one binding."""
    (tmp_path / "src").mkdir()
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    (tmp_path / "src" / "thing.py").write_text(textwrap.dedent(f"""
        def decide(x):
            '''A docstring nobody should be digesting.'''
            {behaviour}
    """))
    (tmp_path / "config" / "prompts" / "seat.md").write_text(
        f"# Seat\n\n{prose}\n\nUnrelated line.\n",
    )
    (tmp_path / "config" / "prompt_bindings.yaml").write_text(yaml.safe_dump({
        "bindings": [{
            "name": "the_thing",
            "why": "the seat is told what decide() does",
            "code": [{"file": "src/thing.py", "symbol": "decide"}],
            "prose": [{
                "file": "config/prompts/seat.md",
                "contains": ["the desk refuses above"],
            }],
            "code_digest": "0" * 16,
            "prose_digest": "0" * 16,
        }],
    }))
    return tmp_path


def _pin(root: Path) -> None:
    registry = root / "config" / "prompt_bindings.yaml"
    raw = yaml.safe_load(registry.read_text())
    code, prose = prompt_bindings.current_digests(root, registry)["the_thing"]
    raw["bindings"][0]["code_digest"] = code
    raw["bindings"][0]["prose_digest"] = prose
    registry.write_text(yaml.safe_dump(raw))


def test_a_mechanism_that_changes_without_its_prompt_fails(tmp_path):
    """THE LOAD-BEARING TEST. This is the case `src/retired_mechanisms.py`
    states it cannot catch: nothing is deleted, a threshold moves, and the
    sheet goes on describing the old behaviour."""
    root = _tiny_tree(
        tmp_path,
        behaviour="return x > 12",
        prose="the desk refuses above 12 percent.",
    )
    _pin(root)
    assert prompt_bindings.check(root) == []

    # The threshold moves. The sheet is untouched — the whole defect.
    (root / "src" / "thing.py").write_text(
        (root / "src" / "thing.py").read_text().replace("x > 12", "x > 20"),
    )
    problems = prompt_bindings.check(root)
    assert len(problems) == 1
    assert "BEHAVIOUR changed and the prose describing it did not" in problems[0]


def test_changing_both_sides_still_asks_for_a_look_then_passes(tmp_path):
    root = _tiny_tree(
        tmp_path,
        behaviour="return x > 12",
        prose="the desk refuses above 12 percent.",
    )
    _pin(root)
    (root / "src" / "thing.py").write_text(
        (root / "src" / "thing.py").read_text().replace("x > 12", "x > 20"),
    )
    sheet = root / "config" / "prompts" / "seat.md"
    sheet.write_text(sheet.read_text().replace("above 12", "above 20"))
    problems = prompt_bindings.check(root)
    assert len(problems) == 1
    assert "both sides changed" in problems[0]

    _pin(root)
    assert prompt_bindings.check(root) == []


def test_reformatting_and_rewriting_a_docstring_do_not_fire(tmp_path):
    """A check that fires on cosmetics gets switched off. The digest is
    taken over the parsed tree with docstrings dropped."""
    root = _tiny_tree(
        tmp_path,
        behaviour="return x > 12",
        prose="the desk refuses above 12 percent.",
    )
    _pin(root)
    (root / "src" / "thing.py").write_text(textwrap.dedent("""
        # a new comment


        def decide(x):
            '''A completely different explanation, several
            lines long, saying more about why.'''
            return (
                x
                > 12
            )
    """))
    assert prompt_bindings.check(root) == []


def test_a_renamed_or_deleted_anchor_is_an_error_not_a_pass(tmp_path):
    root = _tiny_tree(
        tmp_path,
        behaviour="return x > 12",
        prose="the desk refuses above 12 percent.",
    )
    _pin(root)
    (root / "src" / "thing.py").write_text("def other(x):\n    return x > 12\n")
    with pytest.raises(prompt_bindings.BindingError) as exc:
        prompt_bindings.check(root)
    assert "no such function" in str(exc.value)


def test_a_prose_anchor_that_selects_nothing_is_an_error_not_a_pass(tmp_path):
    root = _tiny_tree(
        tmp_path,
        behaviour="return x > 12",
        prose="the desk refuses above 12 percent.",
    )
    _pin(root)
    (root / "config" / "prompts" / "seat.md").write_text("# Seat\n\nnothing.\n")
    with pytest.raises(prompt_bindings.BindingError):
        prompt_bindings.check(root)


def test_an_empty_registry_is_refused(tmp_path):
    path = tmp_path / "prompt_bindings.yaml"
    path.write_text("bindings: []\n")
    with pytest.raises(prompt_bindings.BindingError):
        prompt_bindings.load_registry(path)


def test_the_behaviour_registry_is_not_a_second_deletion_grep():
    """Item 107 says to check for the neighbouring idea before building a
    second one. The deletion-site check exists and is untouched; this
    module must not duplicate it."""
    assert (REPO_ROOT / "src" / "retired_mechanisms.py").exists()
    assert (REPO_ROOT / "config" / "retired_mechanisms.yaml").exists()
    source = (REPO_ROOT / "src" / "prompt_bindings.py").read_text()
    assert "retired_mechanisms" in source, (
        "the behaviour-change registry must point at the deletion-site "
        "check, so the next reader does not build a third one"
    )


# ---------------------------------------------------------------------------
# (c) all ten sheets, not a subset
# ---------------------------------------------------------------------------

def test_every_prompt_file_on_disk_is_registered():
    on_disk = {p.name for p in PROMPT_DIR.glob("*.md")}
    assert on_disk, "no prompt sheets found — the check would pass vacuously"
    assert on_disk == set(PROMPT_NAMESPACES), (
        f"unregistered: {sorted(on_disk - set(PROMPT_NAMESPACES))}; "
        f"registered but missing: {sorted(set(PROMPT_NAMESPACES) - on_disk)}"
    )
    assert len(on_disk) == 10, f"expected ten sheets, found {len(on_disk)}"


def test_the_live_prompt_directory_audits_clean():
    from src.agents.prompt_limits import load_risk_config_from_settings

    cfg = load_risk_config_from_settings(REPO_ROOT / "config" / "settings.yaml")
    problems = audit_prompt_coverage(PROMPT_DIR, cfg)
    assert problems == [], "\n".join(problems)


def test_an_unregistered_sheet_fails_the_audit(tmp_path):
    (tmp_path / "brand_new_seat.md").write_text("no placeholders here\n")
    problems = audit_prompt_coverage(tmp_path)
    assert any("not registered" in p for p in problems)


def test_a_placeholder_in_a_namespace_the_sheet_is_not_registered_for_fails(tmp_path):
    (tmp_path / "news_analyst.md").write_text("ceiling {{risk.max_position_pct}}\n")
    problems = audit_prompt_coverage(tmp_path)
    assert any("news_analyst.md" in p and "risk" in p for p in problems)


def test_a_typo_in_a_resolvable_placeholder_fails_the_audit(tmp_path):
    (tmp_path / "position_reviewer.md").write_text("{{flags.drift_wieght_pct}}\n")
    problems = audit_prompt_coverage(tmp_path)
    assert any("drift_wieght_pct" in p for p in problems)


def test_no_sheet_ships_an_unrendered_placeholder_to_a_model():
    """A sheet that carries `{{...}}` and is read raw sends the braces to a
    paid seat. Every sheet with placeholders must name a renderer."""
    for path in sorted(PROMPT_DIR.glob("*.md")):
        keys = placeholders_in(path.read_text())
        if not keys:
            continue
        assert PROMPT_NAMESPACES[path.name], (
            f"{path.name} carries {sorted(keys)} but is registered as "
            f"carrying no placeholders"
        )


def test_the_position_reviewer_sheet_is_actually_rendered():
    """It was read raw until item 107. Now it carries placeholders, so
    reading it raw would ship `{{flags.drift_weight_pct}}` to the seat."""
    from src.agents.position_reviewer import PositionReviewerAgent

    agent = PositionReviewerAgent.__new__(PositionReviewerAgent)
    rendered = agent.system_prompt
    assert "{{" not in rendered
    assert f"weight > {DRIFT_WEIGHT_PCT:g}%" in rendered


def test_the_flags_namespace_renders_and_refuses():
    assert render_prompt_limits("{{flags.drift_weight_pct}}", None) == "12"
    assert render_prompt_limits("{{flags.drift_pnl_pct}}", None) == "10"
    with pytest.raises(PromptPlaceholderError):
        render_prompt_limits("{{flags.no_such_threshold}}", None)
    with pytest.raises(PromptPlaceholderError):
        render_prompt_limits("{{execution.slippage_pct}}", None)


# ---------------------------------------------------------------------------
# (b) the 12 has one definition site
# ---------------------------------------------------------------------------

#: The one site that still types the pair as bare literals. `src/pipeline.py`
#: was owned by another change when item 107 shipped and could not be edited
#: in the same pass. It is named here rather than tolerated silently, so the
#: count can only go down: a second entry cannot appear without this test
#: failing. Item 107 stays open on exactly this line.
KNOWN_UNCONVERTED = {
    "src/pipeline.py",
}

_WEIGHT_LITERAL = re.compile(r"weight_pct\s*[><]=?\s*12\b")
_PROSE_LITERAL = re.compile(r"[Ww]eight\s*>\s*12%|weight\s*>\s*12\b|>12% weight")


def test_the_drift_threshold_has_exactly_one_definition_site():
    definitions = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*DRIFT_WEIGHT_PCT\s*=", line):
                definitions.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert definitions == ["src/risk/metrics.py:" + str(_line_of_definition())], (
        f"the drift weight threshold must have exactly one definition; "
        f"found {definitions}"
    )
    assert DRIFT_WEIGHT_PCT == 12.0
    assert DRIFT_PNL_PCT == 10.0


def _line_of_definition() -> int:
    text = (REPO_ROOT / "src" / "risk" / "metrics.py").read_text().splitlines()
    for lineno, line in enumerate(text, 1):
        if line.startswith("DRIFT_WEIGHT_PCT"):
            return lineno
    raise AssertionError("DRIFT_WEIGHT_PCT is not defined in src/risk/metrics.py")


def test_no_new_site_types_the_drift_threshold():
    offenders = set()
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "src/risk/metrics.py":
            continue
        if _WEIGHT_LITERAL.search(path.read_text()):
            offenders.add(rel)
    assert offenders == KNOWN_UNCONVERTED, (
        f"drift-threshold literals outside the one definition: "
        f"unexpected {sorted(offenders - KNOWN_UNCONVERTED)}; "
        f"already fixed (remove from KNOWN_UNCONVERTED): "
        f"{sorted(KNOWN_UNCONVERTED - offenders)}"
    )


def test_no_prompt_sheet_types_the_drift_threshold():
    offenders = []
    for path in sorted(PROMPT_DIR.glob("*.md")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _PROSE_LITERAL.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()[:90]}")
    assert offenders == [], (
        "a prompt sheet types the drift threshold instead of rendering it "
        "from `flags.drift_weight_pct`:\n" + "\n".join(offenders)
    )


def test_drift_flag_treats_unknowable_as_not_flagged():
    assert drift_flag(20.0, 20.0) is True
    assert drift_flag(12.0, 20.0) is False       # strict, not >=
    assert drift_flag(20.0, 10.0) is False
    assert drift_flag(None, 20.0) is False       # never a silent zero
    assert drift_flag(20.0, None) is False
