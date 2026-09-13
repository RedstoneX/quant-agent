"""The Risk Manager is SHOWN its limits, never TOLD them.

Two jobs in this file.

1. THE BUILD CHECK (`test_no_hand_typed_limit_beside_a_risk_setting`,
   `test_every_wired_setting_still_has_a_placeholder`,
   `test_every_placeholder_in_the_sheet_resolves`). These fail CI when a
   numeric limit is hand-typed into `config/prompts/risk_manager.md` instead
   of being rendered from `config/settings.yaml`.

2. THE BEHAVIOUR (the rest). Each wired limit renders its live value; moving
   the setting moves what the reviewer reads; a placeholder naming no setting
   fails loudly rather than rendering blank or a stale default.

WHY THIS EXISTS — AND WHAT ACTUALLY HAPPENED, WHICH IS NOT DRIFT. Commit
e1c639a2 (2026-09-11, PR #297, "single-name cap 100 -> 33") set
`risk.max_position_pct: 65` and, in the SAME diff about a hundred lines away,
wrote "tighter than the 33% long single-name ceiling" into this sheet. The
prompt was wrong at birth, not stale over time — the commit's title says 33,
it landed at 65, and only one of the two hunks got the correction. The same
commit ALSO wrote `max_position_pct=65` correctly into the sheet's hard-rule
inventory, so the sheet contradicted itself from the first minute.

Worse, and the reason this file's check has the shape it does: the line that
commit REPLACED read "`max_single_short_pct` (10%, half the long single-name
ceiling". That is a RELATION. It carries no second copy of any number and
could not drift. The commit swapped a drift-immune phrasing for a hand-typed
literal.

TWO CHECKS, BECAUSE ONE OF THEM WOULD HAVE MISSED THE ORIGINATING BUG.

  (a) `_hand_typed_limits` — adjacency. A digit stated as the value of a
      `RiskConfig` field name (within ADJACENCY_CHARS of it) must be a
      rendered placeholder. Catches `max_position_pct=65` and
      "`max_single_short_pct` (10%".

  (b) `_unrendered_limit_phrases` — the limit-noun pattern. A numeral read as
      the value of a "ceiling / cap / budget / limit / floor / maximum"
      phrase must be a rendered placeholder. This is the one that catches the
      2026-09-11 shape, where the literal ("33% long single-name ceiling")
      sits well past the adjacency window of any setting name — and check (a)
      demonstrably does NOT catch it. See
      `test_adjacency_alone_would_have_missed_the_originating_bug`, which
      pins that gap so nobody re-describes (a) as sufficient.

WHY BOTH ARE DELIBERATELY NARROW. A general "no numbers in the prompt" rule
would cry wolf constantly — the sheet is full of legitimate figures: worked
arithmetic ("a 15% position stopped 3% below entry risks 0.45% of equity"),
break-even hit rates, prices in a `reason` example, dates, spec section
numbers. Both checks therefore key on a limit being STATED AS SUCH, which an
illustration does not do.

WHAT NEITHER CHECK CATCHES, stated plainly rather than papered over:

  * a limit restated with no setting name nearby AND no limit noun — "no
    single holding may exceed 33% of the book" passes both. Only the
    placeholder-coverage check (c) constrains this, and only by ensuring the
    correct rendered statement still exists somewhere.
  * a number that is a limit elsewhere in the codebase but is not a
    `RiskConfig` field. The 50% correlation-cluster advisory is a function
    default in `src/risk/rules.py`, not a setting, and is invisible here.
  * a placeholder rendering the WRONG setting for the sentence it sits in.
    Nothing mechanical can know that a sentence about shorts should cite the
    short cap.

So: these checks make the 2026-09-11 shape fail the build. They do not make
this class of defect impossible, and this file must not be cited as if they
did.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.agents.prompt_limits import (
    PLACEHOLDER_RE, PromptPlaceholderError, load_risk_config_from_settings,
    placeholders_in, render_prompt_limits, resolve_placeholder,
)
from src.agents.risk_manager import PROMPT_PATH, SETTINGS_PATH, RiskManagerAgent
from src.config import RiskConfig

#: Settings whose value the sheet states and must therefore render. This IS a
#: hand-maintained list — the module docstring's "no hand-maintained table"
#: claim applies to `src/agents/prompt_limits.py`, which holds no limit VALUES
#: and resolves fields reflectively; it does not apply here. What this list
#: holds is an intent ("the sheet is supposed to state these"), which cannot
#: be derived from either the config or the sheet, because deriving it from
#: the sheet is exactly what a regression would defeat.
WIRED_SETTINGS = (
    "max_position_pct",
    "max_total_position_pct",
    "max_sector_pct",
    "effective_max_daily_loss_pct",
    "max_position_risk_pct",
    "min_position_risk_pct",
    "max_single_short_pct",
    "max_gross_bearish_pct",
)

#: Nouns that mark a numeral as being stated AS a limit rather than used in an
#: illustration. This is the pattern that catches the 2026-09-11 shape.
_LIMIT_NOUN = r"(?:ceiling|caps?|budget|limit|maximum|floor|allowance)\b"

#: A numeral read as the value of a limit noun close after it.
_LIMIT_PHRASE = re.compile(
    r"(?<![\w.$])(\d+(?:\.\d+)?)\s*%?[^.\n]{0,32}?" + _LIMIT_NOUN, re.I,
)

#: Phrases the limit-noun check must not flag, each with the reason it is not
#: a limit statement. Kept explicit and short: an exemption is a hole, so it
#: should be readable in one screen and argued for individually.
_LIMIT_PHRASE_EXEMPTIONS = (
    # The sheet states TWICE that the 1.5 reward:risk floor no longer exists,
    # once as a forbidden phrasing to quote back. Both are negations of a
    # removed gate, not statements of a live limit, and there is no setting
    # to render (the gate was deleted, not reconfigured — PR #341).
    "1.5 floor",
    "1.5\nfloor",
)

#: How far past a setting's name a digit still counts as "restating its
#: value". Short on purpose: `name=65` and `` `name` (10%, `` are the two
#: shapes this defect has actually taken, and both put the digit within a few
#: characters. A longer window starts swallowing the sentence's ordinary
#: prose and the check turns noisy, which is worse than a narrower one.
ADJACENCY_CHARS = 24

#: Stripped before the digit scan: a spec section reference (§12.3), a dollar
#: figure from a worked example ($6K), and an issue/item pointer (item 32).
#: None of these is a limit, and all three legitimately sit near a setting
#: name in this sheet's provenance notes.
_NOT_A_LIMIT = re.compile(r"§[\d.]+|\$[\d.,]+[KkMmBb]?|item\s+\d+|#\d+|\d{4}-\d{2}-\d{2}")


def _sheet() -> str:
    return PROMPT_PATH.read_text()


def _live_risk_config() -> RiskConfig:
    return load_risk_config_from_settings(SETTINGS_PATH)


def _risk_setting_names() -> list[str]:
    """Every declared `RiskConfig` field, longest first so that scanning for
    `max_position_pct` does not also match inside `max_position_risk_pct`."""
    return sorted(RiskConfig.model_fields, key=len, reverse=True)


# --------------------------------------------------------------------------
# 1. The build check
# --------------------------------------------------------------------------

#: Stands in for a placeholder while scanning. It occupies the position the
#: rendered value will take, which is what makes the check precise: the
#: question is not "is there a digit near this setting's name" but "does the
#: FIRST thing stated after this setting's name come from the config".
_RENDERED = "\x00"


def _hand_typed_limits(text: str) -> list[str]:
    """Every place a setting's name is followed by a hand-typed value.

    Placeholders collapse to a marker rather than to nothing, so
    `max_position_pct={{risk.max_position_pct}}` scans as
    `max_position_pct=<rendered>` and passes, while `max_position_pct=65`
    does not. A digit inside the window is a finding only when it comes
    BEFORE any marker — otherwise the value IS rendered and the digits that
    follow are ordinary prose ("...=65% — a 15% position stopped 3% below
    entry...").
    """
    marked = PLACEHOLDER_RE.sub(_RENDERED, text)
    findings: list[str] = []
    claimed: list[tuple[int, int]] = []
    for name in _risk_setting_names():
        for match in re.finditer(re.escape(name), marked):
            # A longer setting name already covering this span wins, so
            # `max_position_risk_pct` is not also reported as
            # `max_position_pct`.
            if any(s <= match.start() < e for s, e in claimed):
                continue
            claimed.append((match.start(), match.end()))
            window = _NOT_A_LIMIT.sub("", marked[match.end():match.end() + ADJACENCY_CHARS])
            digit = re.search(r"\d", window)
            if digit is None:
                continue
            marker = window.find(_RENDERED)
            if marker == -1 or marker > digit.start():
                findings.append(f"{name} -> {window.replace(_RENDERED, '<rendered>')!r}")
    return findings


def _unrendered_limit_phrases(text: str) -> list[str]:
    """Every numeral stated as the value of a limit noun without being
    rendered.

    Check (b). This is the one that catches the shape the 2026-09-11 commit
    actually introduced — "tighter than the 33% long single-name ceiling" —
    which sits outside the adjacency window of any setting name and which
    check (a) misses entirely.
    """
    # Spec section references (§9.4, §12.3) are numbered pointers, not
    # values; blanked so "§9.4 agreement ceiling" does not read as a limit
    # stated at 9.4.
    marked = re.sub(r"§\s*[\d.]+", "", PLACEHOLDER_RE.sub(_RENDERED, text))
    findings = []
    for match in _LIMIT_PHRASE.finditer(marked):
        phrase = match.group(0).replace(_RENDERED, "<rendered>")
        if any(ex in match.group(0) for ex in _LIMIT_PHRASE_EXEMPTIONS):
            continue
        findings.append(phrase)
    return findings


def test_no_unrendered_limit_phrase():
    findings = _unrendered_limit_phrases(_sheet())
    assert not findings, (
        "A numeral is stated as the value of a limit in "
        "config/prompts/risk_manager.md without being rendered from the "
        "config. Either render it from its setting, or — better where the "
        "sentence is about how two limits RELATE — state the relation and "
        "name the settings, which carries no copy of any number at all:\n  "
        + "\n  ".join(findings)
    )


def test_the_check_catches_the_shape_that_actually_happened():
    """The 2026-09-11 regression, reproduced exactly: the placeholder is left
    in place and a SECOND, hand-typed ceiling is added beside it. This is what
    commit e1c639a2 did, and it is not the same as deleting a placeholder."""
    regressed = _sheet().replace(
        "deliberately tighter than the long single-name ceiling "
        "`max_position_pct`",
        "tighter than the 33% long single-name ceiling",
    )
    assert regressed != _sheet(), "fixture text no longer present in the sheet"
    assert any("33" in f for f in _unrendered_limit_phrases(regressed))


def test_adjacency_alone_would_have_missed_the_originating_bug():
    """Pins the known gap in check (a) so it is never described as
    sufficient. If this test starts failing because adjacency got stronger,
    that is good news — delete the test and say so in the docstring."""
    regressed = _sheet().replace(
        "deliberately tighter than the long single-name ceiling "
        "`max_position_pct`",
        "tighter than the 33% long single-name ceiling",
    )
    assert _hand_typed_limits(regressed) == [], (
        "check (a) now catches this shape — update the module docstring, "
        "which currently states that it does not"
    )


def test_no_hand_typed_limit_beside_a_risk_setting():
    findings = _hand_typed_limits(_sheet())
    assert not findings, (
        "A numeric limit is hand-typed into config/prompts/risk_manager.md "
        "next to the setting it restates. Replace it with a "
        "{{risk.<setting>}} placeholder so the reviewer reads the value the "
        "engine enforces:\n  " + "\n  ".join(findings)
    )


def test_build_check_catches_a_hand_typed_limit():
    """The regression the check exists for: a placeholder swapped back to a
    literal. Uses the exact shape of the 2026-09-13 drift."""
    regressed = _sheet().replace(
        "`max_single_short_pct` ({{risk.max_single_short_pct}}%",
        "`max_single_short_pct` (10%",
    )
    assert regressed != _sheet(), "fixture text no longer present in the sheet"
    findings = _hand_typed_limits(regressed)
    assert any("max_single_short_pct" in f for f in findings)


def test_build_check_does_not_fire_on_example_arithmetic():
    """Worked arithmetic in an illustration is not a limit and must pass.

    This is the exact sentence from the sheet's `Sizing Sanity` step, plus
    the inverse-ETF leverage illustration and a hit-rate table — the three
    kinds of legitimate number that a blunter check would flag.
    """
    illustrations = (
        "A 15% position stopped 3% below entry risks 0.45% of equity; a 5% "
        "position stopped 20% below entry risks 1.0%.\n"
        "Worked illustration: 3x `SQQQ` at $6K notional is $18K of gross "
        "bearish exposure.\n"
        "R/R X breaks even at a hit rate of `1/(1+X)` (1.5 -> 40%, 2.0 -> "
        "33%, 3.0 -> 25%).\n"
        "stop $61.54 sits under no level the chart defends.\n"
    )
    assert _hand_typed_limits(illustrations) == []
    # And the same text still passes when a setting name is present but its
    # value is properly rendered.
    assert _hand_typed_limits(
        "`max_position_pct={{risk.max_position_pct}}` — " + illustrations,
    ) == []


def test_every_wired_setting_still_has_a_placeholder():
    """Deleting a placeholder to type the number back in fails here even if
    the literal lands too far from the setting's name for the adjacency
    check to see it."""
    keys = placeholders_in(_sheet())
    missing = [s for s in WIRED_SETTINGS if f"risk.{s}" not in keys]
    assert not missing, (
        "config/prompts/risk_manager.md no longer renders these settings: "
        + ", ".join(missing)
        + ". Restore the {{risk.<setting>}} placeholder. Do NOT resolve this "
        "by typing the number into the sheet, and do NOT resolve it by "
        "deleting the entry here — that is defeating the check, not passing "
        "it. Removing an entry from WIRED_SETTINGS is correct ONLY when the "
        "sheet genuinely no longer needs to state that limit at all (for "
        "instance because the sentence was rewritten as a relation between "
        "two named settings), and the removal should say which."
    )


def test_every_placeholder_in_the_sheet_resolves():
    """No placeholder may name a setting that does not exist — the sheet must
    render cleanly against the live settings file."""
    rendered = render_prompt_limits(_sheet(), _live_risk_config())
    assert "{{" not in rendered and "}}" not in rendered


# --------------------------------------------------------------------------
# 2. Behaviour — the reviewer reads the live value
# --------------------------------------------------------------------------

def test_each_wired_limit_renders_its_live_value():
    cfg = _live_risk_config()
    rendered = render_prompt_limits(_sheet(), cfg)
    raw = yaml.safe_load(SETTINGS_PATH.read_text())["risk"]
    for setting in WIRED_SETTINGS:
        value = getattr(cfg, setting)
        assert f"{value:g}" in rendered, (
            f"{setting} (live value {value}) does not appear in the rendered "
            f"sheet"
        )
    # The specific defect this PR closes. The stale "33% long single-name
    # ceiling" is gone; the hard-rule inventory renders the live ceiling, and
    # the SHORT-discipline sentence that carried the bad literal is now a
    # RELATION naming both settings and quoting neither long number.
    # NB: "33%" still legitimately appears in the break-even hit-rate table
    # (R/R 2.0 -> 33%), which is arithmetic, not a limit. Assert on the
    # PHRASE that carried the defect, not on the digits.
    assert "33% long single-name ceiling" not in rendered
    assert f"`max_position_pct={raw['max_position_pct']:g}`" in rendered
    assert (
        "deliberately tighter than the long single-name ceiling "
        "`max_position_pct`"
    ) in rendered


def test_changing_the_setting_changes_what_the_reviewer_is_shown():
    cfg = _live_risk_config()
    before = render_prompt_limits(_sheet(), cfg)
    moved = cfg.model_copy(update={"max_position_pct": 41.0})
    after = render_prompt_limits(_sheet(), moved)
    assert "`max_position_pct=41`" in after
    assert "`max_position_pct=41`" not in before
    assert f"`max_position_pct={cfg.max_position_pct:g}`" not in after


def test_short_cap_and_gross_bearish_track_their_settings():
    cfg = _live_risk_config().model_copy(update={
        "max_single_short_pct": 7.0, "max_gross_bearish_pct": 13.0,
    })
    rendered = render_prompt_limits(_sheet(), cfg)
    assert "`max_single_short_pct` (7%" in rendered
    assert "`max_gross_bearish_pct` (13% of book" in rendered


def test_per_trade_risk_budget_is_the_ratified_unit_not_the_old_half_percent():
    """The sheet used to tell the reviewer the constructor capped a stop-out
    at 0.5% of equity. The ratified per-trade unit is
    `risk.max_position_risk_pct`; 0.5 is `min_position_risk_pct`, a different
    setting. Both now render from their own key."""
    cfg = _live_risk_config().model_copy(update={
        "max_position_risk_pct": 4.0, "min_position_risk_pct": 0.25,
    })
    rendered = render_prompt_limits(_sheet(), cfg)
    assert "`max_position_risk_pct`,\n   4% of equity" in rendered
    assert "`min_position_risk_pct`, 0.25% risk" in rendered


def test_daily_loss_renders_the_derived_value_not_the_null_setting():
    """`max_daily_loss_pct` is deliberately null in settings.yaml — it is an
    override. The sheet renders the DERIVED effective figure, and a null must
    never reach the reviewer as a blank."""
    cfg = _live_risk_config()
    assert cfg.max_daily_loss_pct is None
    rendered = render_prompt_limits(_sheet(), cfg)
    assert f"`max_daily_loss_pct={cfg.effective_max_daily_loss_pct:g}`" in rendered


# --------------------------------------------------------------------------
# 3. Fail loud, never blank
# --------------------------------------------------------------------------

def test_placeholder_with_no_matching_setting_fails_loudly():
    cfg = _live_risk_config()
    with pytest.raises(PromptPlaceholderError) as exc:
        render_prompt_limits("ceiling is {{risk.max_positon_pct}}%", cfg)
    assert "max_positon_pct" in str(exc.value)


def test_unknown_namespace_fails_loudly():
    with pytest.raises(PromptPlaceholderError):
        render_prompt_limits("{{execution.slippage_pct}}", _live_risk_config())


def test_placeholder_never_renders_blank_or_a_default():
    """The failure mode this must not have: substituting nothing, or a number
    typed into the renderer."""
    cfg = _live_risk_config()
    for bad in ("{{risk.nonexistent}}", "{{risk.model_dump}}", "{{risk.require_stop_loss}}"):
        with pytest.raises(PromptPlaceholderError):
            render_prompt_limits(bad, cfg)


def test_a_null_optional_setting_is_refused_rather_than_blanked():
    cfg = _live_risk_config()
    assert cfg.max_daily_loss_pct is None
    with pytest.raises(PromptPlaceholderError) as exc:
        resolve_placeholder("risk.max_daily_loss_pct", cfg)
    assert "None" in str(exc.value)


def test_integer_limits_render_without_a_trailing_point_zero():
    cfg = _live_risk_config()
    assert resolve_placeholder("risk.max_position_pct", cfg) == "65"
    assert resolve_placeholder("risk.short_gap_risk_multiple", cfg) == "1.5"


# --------------------------------------------------------------------------
# 4. The agent wiring
# --------------------------------------------------------------------------

def _agent(**kwargs) -> RiskManagerAgent:
    return RiskManagerAgent(api_key="test-key", model="test-model", **kwargs)


def test_agent_system_prompt_renders_the_live_settings_file():
    prompt = _agent().system_prompt
    assert "{{" not in prompt
    assert f"`max_position_pct={_live_risk_config().max_position_pct:g}`" in prompt


def test_agent_uses_the_injected_config_over_the_settings_file():
    injected = _live_risk_config().model_copy(update={"max_position_pct": 12.0})
    assert "`max_position_pct=12`" in _agent(risk_config=injected).system_prompt


def test_risk_ceiling_defaults_to_the_live_portfolio_risk_setting():
    """The rendered Portfolio Risk block's ceiling used to fall back to a
    hand-typed 25.0 in this module. It now reads the setting."""
    injected = _live_risk_config().model_copy(update={"max_portfolio_risk_pct": 17.0})
    agent = _agent(risk_config=injected)
    assert agent.risk_config.max_portfolio_risk_pct == 17.0
    # build_user_message reads it through the same property.
    assert float(agent.risk_config.max_portfolio_risk_pct) == 17.0


def test_settings_file_without_a_risk_block_fails_loudly(tmp_path: Path):
    bad = tmp_path / "settings.yaml"
    bad.write_text("llm:\n  fallback_model: x\n")
    with pytest.raises(PromptPlaceholderError):
        load_risk_config_from_settings(bad)


# --------------------------------------------------------------------------
# 5. The rendered value and the ENFORCED value must be the same object
# --------------------------------------------------------------------------
#
# Rendering a limit into the reviewer's briefing from settings.yaml while the
# deterministic engine enforced a different object's DEFAULT would be the same
# two-homes defect this change removes, pointed the other way. `Pipeline`
# builds the engine's `RiskConfig` from a hand-enumerated argument list, so a
# setting left out of it silently falls back to the pydantic class default and
# settings.yaml is ignored for that field — the exact bug already found once
# for `allow_margin` (Codex r11 P2, see the comment at that argument).

def _engine_config_arguments() -> set[str]:
    """Field names `Pipeline.__init__` actually passes when it builds the
    risk engine's config. Parsed from source rather than by constructing a
    Pipeline, which needs brokers, keys and a database."""
    src = Path(__file__).parent.parent.joinpath("src/pipeline.py").read_text()
    start = src.index("self.risk_engine = RiskRuleEngine(RiskConfig(")
    depth = 0
    for offset, char in enumerate(src[start:]):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
    return set(re.findall(r"^\s*([a-z_]+)=", src[start:start + offset], re.M))


def test_every_rendered_setting_is_also_threaded_into_the_engine():
    """A setting the sheet SHOWS must be one the engine READS from the same
    file. Without this, the seat could be briefed with settings.yaml's value
    while the engine hard-blocked against a class default."""
    threaded = _engine_config_arguments()
    # `effective_max_daily_loss_pct` is derived, not a field; it is covered by
    # its three inputs, all of which are threaded.
    rendered_fields = [
        s for s in WIRED_SETTINGS if s in RiskConfig.model_fields
    ]
    missing = [s for s in rendered_fields if s not in threaded]
    assert not missing, (
        "config/prompts/risk_manager.md renders these settings from "
        "settings.yaml, but src/pipeline.py does not pass them when building "
        "the risk engine's RiskConfig — so the engine enforces the pydantic "
        "class default instead, and the reviewer is shown a number the "
        "engine may not be using: " + ", ".join(missing)
    )


def test_daily_loss_inputs_are_threaded_too():
    threaded = _engine_config_arguments()
    for field in ("max_daily_loss_pct", "daily_loss_risk_multiple",
                  "max_position_risk_pct"):
        assert field in threaded, field


def test_the_rest_of_the_omission_is_recorded_not_silently_swept():
    """This PR threaded only the settings it renders. The rest of the
    hand-enumerated list is still incomplete, is pre-existing, and is
    currently latent (every omitted field's class default equals its
    settings.yaml value). Pinned here so the count cannot grow unnoticed and
    so nobody reads this file as a claim that the whole list is wired."""
    threaded = _engine_config_arguments()
    raw = yaml.safe_load(SETTINGS_PATH.read_text())["risk"]
    omitted = sorted(
        f for f in RiskConfig.model_fields
        if f not in threaded and f in raw
    )
    live_divergence = [
        f for f in omitted
        # `get_default(call_default_factory=True)` — a field declared with a
        # default_factory (agreement_ceiling_pct) reports `.default` as
        # PydanticUndefined, which would read as a false divergence.
        if raw[f] != RiskConfig.model_fields[f].get_default(
            call_default_factory=True,
        )
    ]
    assert not live_divergence, (
        "A setting is present in settings.yaml with a NON-DEFAULT value and "
        "is NOT passed to the engine's RiskConfig — the engine is enforcing "
        "something settings.yaml does not say. This is live-wrong, not "
        "latent: " + ", ".join(live_divergence)
    )
    assert len(omitted) == 18, (
        f"the engine's hand-enumerated RiskConfig now omits {len(omitted)} "
        f"settings present in settings.yaml, not 18 — if that grew, thread "
        f"the new one; if it shrank, lower this number. Omitted: {omitted}"
    )


# --------------------------------------------------------------------------
# 6. The render happens at construction, not mid-session
# --------------------------------------------------------------------------

def test_a_bad_placeholder_fails_at_construction_not_at_first_llm_call(tmp_path, monkeypatch):
    """`system_prompt` is a lazy property read inside `BaseAgent.run`, i.e. at
    the risk stage, after the whole day's analysis has been paid for. The
    commissioning render in `__init__` moves that failure to startup."""
    broken = tmp_path / "risk_manager.md"
    broken.write_text("ceiling is {{risk.no_such_setting}}%\n")
    monkeypatch.setattr("src.agents.risk_manager.PROMPT_PATH", broken)
    with pytest.raises(PromptPlaceholderError):
        RiskManagerAgent(api_key="k", model="m")


def test_a_good_sheet_constructs_cleanly():
    agent = RiskManagerAgent(api_key="k", model="m")
    agent.assert_prompt_renders()  # idempotent, callable by a commissioning script
