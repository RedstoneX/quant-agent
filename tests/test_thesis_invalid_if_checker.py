"""`check_thesis_invalid_if` — checking a real `thesis_invalid_if` condition
against real market data instead of the pure honour system.

Every `thesis_invalid_if` string used below is a REAL value recorded by this
desk, pulled from two real sources during the measurement pass that decided
what this checker should cover (see the module docstring in
`src/risk/exit_guard.py` and `docs/WORK.md` for the full bucket counts):

  - `trades.reasoning`, the `(invalid if: ...)` / `(thesis_invalid_if: ...)`
    marker on real executed BUYs (VLO, OXY, ONDS, RSG, UNH, NVDA below).
  - `specialist_evidence.evidence_json -> thesis_invalid_if`, the
    un-truncated field on real tech-analyst candidates (the Bollinger Band
    and qualitative examples below, which the checker is expected to refuse).

The market numbers (current_price, ma_20, ma_50) fed into each case are
chosen deliberately to land on one side or the other of the real threshold —
that is normal unit-test control, not fabricated `thesis_invalid_if` text.
"""

from src.risk.exit_guard import ThesisInvalidationCheck, check_thesis_invalid_if


# ---------------------------------------------------------------------------
# Bucket (b): moving-average reference — the largest real bucket (984/1028
# unique specialist_evidence values, 95.7%).
# ---------------------------------------------------------------------------


def test_ma_reference_triggered_real_vlo_condition():
    # VLO BUY, 2026-08-21: "Price closes below MA20 (319.25) on increased
    # volume." — MA20 value is stale-at-writing-time; the checker compares
    # against the CURRENT ma_20 the caller supplies, not the embedded number.
    result = check_thesis_invalid_if(
        "Price closes below MA20 (319.25) on increased volume.",
        current_price=300.00,
        ma_20=310.00,
    )
    assert result.status == "TRIGGERED"


def test_ma_reference_not_triggered_real_oxy_condition():
    # OXY BUY, 2026-08-24: "Price closes below MA50 (54.79)"
    result = check_thesis_invalid_if(
        "Price closes below MA50 (54.79)",
        current_price=58.00,
        ma_50=55.00,
    )
    assert result.status == "NOT_TRIGGERED"


def test_ma_reference_worded_as_at_still_parses_real_onds_condition():
    # ONDS BUY, 2026-08-27: "Price closes below the MA50 at 8.15."
    result = check_thesis_invalid_if(
        "Price closes below the MA50 at 8.15.",
        current_price=7.50,
        ma_50=8.00,
    )
    assert result.status == "TRIGGERED"


def test_ma_reference_missing_ma_value_is_unparseable_not_a_guess():
    # Same real VLO text, but the caller didn't supply ma_20 — must refuse
    # rather than silently skip the check or assume NOT_TRIGGERED.
    result = check_thesis_invalid_if(
        "Price closes below MA20 (319.25) on increased volume.",
        current_price=300.00,
    )
    assert result.status == "UNPARSEABLE"


def test_ma_reference_unsupported_period_is_unparseable():
    # No pipeline computation exists for MA10 — refuse rather than guess.
    result = check_thesis_invalid_if(
        "Price closes below MA10 (40.00) on volume.",
        current_price=35.00,
        ma_20=38.0,
    )
    assert result.status == "UNPARSEABLE"


def test_bare_day_average_phrasing_is_recognized():
    """Found by adversarial review, 2026-09-03: the module's own docstring
    cites "the 50-day average" as a covered example, but the regex only
    matched "...moving average" — the bare phrasing silently fell through
    to UNPARSEABLE. Fixed by making "moving" optional. Confirmed against
    real recorded data this exact phrasing doesn't appear yet (so nothing
    was actually miscovered in production), but it's a real, cheap gap to
    close before it does."""
    result = check_thesis_invalid_if(
        "Thesis invalid if price closes below the 50-day average.",
        current_price=100.0,
        ma_50=105.0,
    )
    assert result.status == "TRIGGERED"


# ---------------------------------------------------------------------------
# Bucket (a): bare numeric price level — second real bucket (38/1028, 3.7%).
# ---------------------------------------------------------------------------


def test_price_level_triggered_real_rsg_condition():
    # RSG BUY, 2026-08-31: "Price closes below the $218.51 support level on
    # increased volume."
    result = check_thesis_invalid_if(
        "Price closes below the $218.51 support level on increased volume.",
        current_price=215.00,
    )
    assert result.status == "TRIGGERED"
    assert "218.51" in result.detail


def test_price_level_not_triggered_real_unh_condition():
    # UNH SHORT, 2026-09-02: "Price closes above resistance level 412.60 on
    # strong volume" — SHORT's invalidation direction is UP.
    result = check_thesis_invalid_if(
        "Price closes above resistance level 412.60 on strong volume",
        current_price=400.00,
    )
    assert result.status == "NOT_TRIGGERED"


def test_price_level_triggered_real_nvda_condition():
    # NVDA BUY, 2026-09-02: "Price closes below support at 207.89"
    result = check_thesis_invalid_if(
        "Price closes below support at 207.89",
        current_price=200.00,
    )
    assert result.status == "TRIGGERED"


def test_price_level_missing_current_price_is_unparseable_not_a_guess():
    result = check_thesis_invalid_if(
        "Price closes below the $218.51 support level on increased volume.",
        current_price=None,
    )
    assert result.status == "UNPARSEABLE"


# ---------------------------------------------------------------------------
# Genuinely out-of-scope real examples — must return UNPARSEABLE, never a
# guess, however plausible-looking the text is.
# ---------------------------------------------------------------------------


def test_compound_condition_is_unparseable_real_nvda_condition():
    # NVDA BUY, 2026-08-24: "Price closes below MA50 or breaks $180 support"
    # — refuse to partially evaluate an "or" rather than silently pick a side.
    result = check_thesis_invalid_if(
        "Price closes below MA50 or breaks $180 support",
        current_price=170.00,
        ma_50=190.00,
    )
    assert result.status == "UNPARSEABLE"


def test_indicator_condition_is_unparseable_real_specialist_evidence_condition():
    # Real specialist_evidence value — the only Bollinger Band example found
    # in the whole corpus. Out of scope: no indicator-threshold checker was
    # built (too rare in the real data, 1/1028).
    result = check_thesis_invalid_if(
        "Price closes above the upper Bollinger Band (43.41) on increasing volume.",
        current_price=45.00,
    )
    assert result.status == "UNPARSEABLE"


def test_qualitative_condition_is_unparseable_real_specialist_evidence_condition():
    # Real specialist_evidence value — inherently unparseable, no market
    # data can ever adjudicate this.
    result = check_thesis_invalid_if(
        "Guidance pulled or credit quality significantly deteriorates.",
        current_price=100.00,
    )
    assert result.status == "UNPARSEABLE"


def test_empty_condition_is_unparseable():
    result = check_thesis_invalid_if("", current_price=100.00)
    assert result.status == "UNPARSEABLE"


def test_result_is_frozen_dataclass_with_status_and_detail():
    result = check_thesis_invalid_if(
        "Price closes below the $218.51 support level.",
        current_price=200.00,
    )
    assert isinstance(result, ThesisInvalidationCheck)
    assert result.status == "TRIGGERED"
    assert isinstance(result.detail, str) and result.detail


# ---------------------------------------------------------------------------
# Named macro-series levels — item 99's third checkable shape (2026-09-30)
# ---------------------------------------------------------------------------
#
# Added when all five analyst seats began stating their own falsifier. The
# two price shapes were measured off `tech_analyst` alone, whose condition is
# always about price; `config/prompts/macro_analyst.md` ships "HY OAS widens
# back above 420bps" as its own worked example, and that returned UNPARSEABLE.

_MACRO = {
    "credit_spread": 455.0,  # HY OAS, basis points
    "ig_credit_spread": 92.0,  # IG OAS, basis points
    "vix": 18.2,  # index points
    "treasury_10y": 4.12,  # percent
    "unemployment": 4.3,  # percent
}


def test_the_macro_prompts_own_worked_example_is_now_evaluated() -> None:
    """`macro_analyst.md`'s shipped example must not come back UNPARSEABLE."""
    result = check_thesis_invalid_if(
        "HY OAS widens back above 420bps",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "TRIGGERED", result  # 455bps is above 420bps


def test_macro_series_not_triggered_reads_the_macro_level_not_the_price() -> None:
    result = check_thesis_invalid_if(
        "VIX closes above 30",
        current_price=455.00,
        macro_levels=_MACRO,
    )
    assert result.status == "NOT_TRIGGERED", result
    assert "vix 18.2" in result.detail


def test_percent_on_a_bps_series_is_the_exact_definitional_conversion() -> None:
    """1% = 100bps is a definition, so it is applied; nothing else is."""
    result = check_thesis_invalid_if(
        "high-yield spread above 4.9%",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "NOT_TRIGGERED", result  # 455bps < 490bps


def test_bps_series_with_no_unit_is_refused_rather_than_assumed() -> None:
    result = check_thesis_invalid_if(
        "HY OAS above 420",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "UNPARSEABLE", result
    assert "unit" in result.detail


def test_index_series_quoted_in_a_unit_it_does_not_have_is_refused() -> None:
    result = check_thesis_invalid_if(
        "VIX above 30%",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "UNPARSEABLE", result


def test_a_digit_inside_the_series_name_is_not_read_as_the_threshold() -> None:
    """ "10y yield above 4.5%" must compare 4.12 to 4.5, never to 10."""
    result = check_thesis_invalid_if(
        "10y yield above 4.5%",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "NOT_TRIGGERED", result
    assert "vs level 4.5" in result.detail


def test_macro_series_with_no_supplied_level_is_unparseable_never_a_guess() -> None:
    result = check_thesis_invalid_if(
        "fed funds rate above 5.0%",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "UNPARSEABLE", result
    assert "fed_funds_rate" in result.detail


def test_named_macro_series_wins_over_the_bare_price_shape() -> None:
    """The latent wrong answer: a decimal bps level read as the stock price.

    Before the macro shape existed, "HY OAS above 420.5bps" matched the
    direction-word price pattern and was compared against `current_price`,
    which would have said NOT_TRIGGERED (150 < 420.5) about a condition that
    is in fact true (455bps > 420.5bps).
    """
    result = check_thesis_invalid_if(
        "HY OAS above 420.5bps",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "TRIGGERED", result
    assert "credit_spread" in result.detail


def test_a_qualitative_condition_stays_unparseable_by_design() -> None:
    """The remaining, deliberate limit: words the desk cannot compute."""
    result = check_thesis_invalid_if(
        "the contract award is rescinded",
        current_price=150.00,
        macro_levels=_MACRO,
    )
    assert result.status == "UNPARSEABLE", result


def test_price_and_ma_shapes_are_unchanged_by_the_macro_addition() -> None:
    assert (
        check_thesis_invalid_if(
            "closes below MA50",
            current_price=150.00,
            ma_50=160.00,
            macro_levels=_MACRO,
        ).status
        == "TRIGGERED"
    )
    assert (
        check_thesis_invalid_if(
            "loses the $142.50 level",
            current_price=150.00,
            macro_levels=_MACRO,
        ).status
        == "NOT_TRIGGERED"
    )
