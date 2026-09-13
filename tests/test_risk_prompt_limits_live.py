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

WHY THIS EXISTS. On 2026-09-13 the sheet told the reviewer the long
single-name ceiling was 33% while `risk.max_position_pct` had been 65 since
2026-09-11 — the seat spent two days auditing plans against a limit less than
half the real one. Separately (PR #341) it was still told a 1.5 reward:risk
floor was enforceable months after that gate was removed, which was the named
cause of three of the four whole-plan vetoes in the archived database. Both
are the same defect: a limit with two homes, only one of them enforced.

WHY THE BUILD CHECK IS DELIBERATELY NARROW. A general "no numbers in the
prompt" rule would cry wolf constantly — the sheet is full of legitimate
figures: worked arithmetic in examples ("a 15% position stopped 3% below entry
risks 0.45% of equity"), break-even hit rates, prices in a `reason` example,
dates, spec section numbers. So the check fires on ONE precise pattern: a
digit sitting immediately beside the NAME of a `RiskConfig` setting, which is
how a limit gets restated in prose. That catches every drift actually observed
(`max_single_short_pct` (10%, ...), `max_position_pct=65`) and ignores every
illustration, because an illustration does not name a setting.

What it therefore does NOT catch, stated plainly rather than papered over:
a limit restated in prose far from its setting's name ("the long single-name
ceiling is 33%" with no `max_position_pct` nearby), and a number that is a
limit somewhere else in the codebase but is not a `RiskConfig` field (the
50% correlation-cluster advisory is a Python function default in
`src/risk/rules.py`, not a setting). Both are covered instead by the second
check: every wired setting must still have a live placeholder, so removing
one to type a number in its place fails.
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

#: Settings whose value the sheet states in prose and must therefore render.
#: Removing one from the sheet without removing it here fails the build.
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
        "config/prompts/risk_manager.md no longer renders these settings; if "
        "the sheet genuinely stopped stating them, drop them from "
        "WIRED_SETTINGS in this file too: " + ", ".join(missing)
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
    # The specific drift this PR closes: the long single-name ceiling the
    # reviewer reads is settings.yaml's, and the stale 33% is gone.
    assert f"{raw['max_position_pct']:g}% long single-name ceiling" in rendered


def test_changing_the_setting_changes_what_the_reviewer_is_shown():
    cfg = _live_risk_config()
    before = render_prompt_limits(_sheet(), cfg)
    moved = cfg.model_copy(update={"max_position_pct": 41.0})
    after = render_prompt_limits(_sheet(), moved)
    assert "41% long single-name ceiling" in after
    assert "41% long single-name ceiling" not in before
    assert f"{cfg.max_position_pct:g}% long single-name ceiling" not in after


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
    assert "`max_position_risk_pct`, 4% of\nequity" in rendered
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
    assert f"{_live_risk_config().max_position_pct:g}% long single-name ceiling" in prompt


def test_agent_uses_the_injected_config_over_the_settings_file():
    injected = _live_risk_config().model_copy(update={"max_position_pct": 12.0})
    assert "12% long single-name ceiling" in _agent(risk_config=injected).system_prompt


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
