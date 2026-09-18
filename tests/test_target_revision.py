"""The two things about a revisable take-profit that must never regress.

1. A REVISION CANNOT SWITCH OFF THE EXIT GUARD'S VETO.

   The target is the DENOMINATOR of `thesis_progress_pct`, and `pace` is
   progress over elapsed horizon. Both are in
   `src.risk.exit_guard._HIGHER_IS_BETTER`, and
   `veto_contradicted_exit` only blocks a deterioration claim while
   `MetricDeltas.net_improved` holds — i.e. something improved and NOTHING
   measurably worsened.

   So raising a target mechanically lowers progress and lowers pace, which
   lands both in `worsened`, which clears `net_improved`, which lets a
   "this position is stalling" SELL through that was previously blocked. A
   revisable target built without fixing the measurement side is a machine
   for making winners look stalled and then selling them.

   The fix chosen is to measure progress and pace against the PINNED entry
   target (`trades.initial_take_profit`) rather than the live one. These
   tests pin it end to end: the arithmetic, and the guard's actual verdict.

2. NOTHING AUTOMATICALLY EXITS AT THE TARGET, and the revision path did not
   change that. The automatic trim was deleted in PR #321;
   `tests/test_pipeline.py::test_no_fixed_gain_automatic_profit_trim_exists`
   pins the general rule, and the checks here pin it specifically for the
   revision module and for the flag schema.
"""

import ast
import inspect
import pathlib
import re

import pytest

from src.data.levels import CLUSTER_TOLERANCE_PCT, COVERAGE_MEASURED
from src.models import TargetRevisionFlag
from src.risk import target_revision as tr
from src.risk.exit_guard import (
    _HIGHER_IS_BETTER,
    NOISE_BAND_ATR_MULTIPLE,
    compute_deltas,
    veto_contradicted_exit,
)

STALL_REASON = "position is stalling, no progress against thesis"


def _progress(entry: float, current: float, target: float) -> float:
    """The pipeline's own progress arithmetic (`_build_position_facts`)."""
    return (current - entry) / (target - entry) * 100


def _pace(progress_pct: float, days_held: float, horizon: int) -> float:
    return progress_pct / ((days_held / horizon) * 100)


# ---------------------------------------------------------------------------
# 1. A revision cannot switch off the veto
# ---------------------------------------------------------------------------


def test_raising_the_live_target_would_worsen_both_guarded_metrics():
    """The defect itself, stated as arithmetic, so the reason for the fix
    cannot quietly stop being true.

    Good news: entry 100, target derived at 110, price runs to 108, the
    resistance the target sat on is gone and the target re-derives to 125.
    Measured against the LIVE target, the position instantly looks less
    progressed AND slower than it did an hour earlier.
    """
    entry, current_before, current_after = 100.0, 106.0, 108.0
    old_target, new_target = 110.0, 125.0
    horizon = 10

    before = _progress(entry, current_before, old_target)
    after_live = _progress(entry, current_after, new_target)
    assert after_live < before, (
        "raising the target must lower progress — if this ever stops being "
        "true the premise of this whole module has changed"
    )
    assert _pace(after_live, 5, horizon) < _pace(before, 4, horizon)

    # And both metrics are ones the guard reads directionally.
    assert "thesis_progress_pct" in _HIGHER_IS_BETTER
    assert "pace" in _HIGHER_IS_BETTER


def test_live_target_denominator_switches_the_veto_off():
    """Demonstrates the danger against the REAL guard, not a model of it.

    This is the behaviour the fix exists to prevent. If this test ever
    starts passing a veto, the guard's semantics changed and the pinning
    below needs rechecking.
    """
    entry = 100.0
    prior = {
        "thesis_progress_pct": _progress(entry, 106.0, 110.0),
        "pace": _pace(_progress(entry, 106.0, 110.0), 4, 10),
    }
    # Same position, one hour later, better price — but measured against the
    # newly RAISED target.
    current = {
        "thesis_progress_pct": _progress(entry, 108.0, 125.0),
        "pace": _pace(_progress(entry, 108.0, 125.0), 5, 10),
    }
    deltas = compute_deltas("AAPL", prior, current)
    assert deltas.worsened, "the revision should look like deterioration here"
    assert not deltas.net_improved
    assert veto_contradicted_exit("SELL", STALL_REASON, deltas) is None, (
        "with a live-target denominator the guard stops protecting the "
        "position — this is the machine for selling winners"
    )


def test_pinned_target_denominator_keeps_the_veto_on():
    """The fix. Progress and pace measured against the PINNED entry target
    are unaffected by the revision, so the same SELL stays vetoed."""
    entry, pinned = 100.0, 110.0
    prior = {
        "thesis_progress_pct": _progress(entry, 106.0, pinned),
        "pace": _pace(_progress(entry, 106.0, pinned), 4, 10),
    }
    current = {
        "thesis_progress_pct": _progress(entry, 108.0, pinned),
        "pace": _pace(_progress(entry, 108.0, pinned), 5, 10),
    }
    deltas = compute_deltas("AAPL", prior, current)
    assert not deltas.worsened
    assert deltas.net_improved
    veto = veto_contradicted_exit("SELL", STALL_REASON, deltas)
    assert veto is not None and "vetoed" in veto


def test_a_revision_alone_moves_neither_guarded_metric_at_all():
    """Stronger than "the veto survives": with the denominator pinned, a
    revision on an otherwise unchanged position produces NO delta in either
    guarded metric, so it cannot register as improvement OR deterioration.
    Prevention by construction rather than by a special case."""
    entry, pinned, price = 100.0, 110.0, 108.0
    snapshot = {
        "thesis_progress_pct": _progress(entry, price, pinned),
        "pace": _pace(_progress(entry, price, pinned), 5, 10),
    }
    # The live target moved from 110 to 125; nothing else did.
    deltas = compute_deltas("AAPL", dict(snapshot), dict(snapshot))
    assert deltas.improved == [] and deltas.worsened == []


def test_pipeline_measures_progress_against_the_pinned_target():
    """Pins the wiring, not just the arithmetic: `_build_position_facts`
    must divide by `initial_take_profit`, never by the mutable
    `take_profit`. A regression here reintroduces the defect silently."""
    from src.pipeline import TradingPipeline

    src = inspect.getsource(TradingPipeline._build_position_facts)
    assert 'progress_target = float(' in src
    assert '"initial_take_profit"' in src
    assert "(cur - entry) / (progress_target - entry)" in src
    assert "(cur - entry) / (take_profit - entry)" not in src, (
        "progress is being measured against the MUTABLE target again — a "
        "revision can now switch the exit guard's veto off"
    )
    # The snapshot the next review compares against must not carry a target.
    assert "take_profit" not in TradingPipeline._REVIEW_METRIC_KEYS
    assert "entry_take_profit" not in TradingPipeline._REVIEW_METRIC_KEYS


def test_pinned_target_column_is_never_written_by_a_revision():
    """`update_open_take_profit` writes the live target only. If it ever
    touched `initial_take_profit` the pinned denominator would move with the
    revision and the guard would be exposed again."""
    from src.storage.db import Database

    src = inspect.getsource(Database.update_open_take_profit)
    assert "SET take_profit" in src
    assert "initial_take_profit" not in src.split('"""')[2], (
        "the revision write-back must not touch the pinned entry target"
    )


# ---------------------------------------------------------------------------
# 2. No automatic exit at the target
# ---------------------------------------------------------------------------


def test_revision_module_places_no_orders_and_triggers_no_exit():
    """The revision path adjusts a measurement. It must never sell, trim,
    cover or submit anything — the trailing stop remains the only automatic
    exit (PR #321, 2026-09-12)."""
    # Parsed, not grepped: every docstring in this module legitimately
    # DISCUSSES exits, so a text search would only ever find prose. What
    # matters is whether the CODE names an exit.
    tree = ast.parse(pathlib.Path(tr.__file__).read_text())
    docstrings = {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    identifiers: set[str] = set()
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                literals.add(node.value)

    forbidden_calls = {
        "submit_order", "place_order", "close_position", "liquidate",
        "_auto_take_profit", "update_open_take_profit",
        "record_target_revision", "get_ohlcv",
    }
    assert not (identifiers & forbidden_calls), (
        f"the revision module reaches out: {identifiers & forbidden_calls}"
    )
    # No exit ACTION word appears as a live string constant anywhere.
    exit_words = re.compile(r"\b(SELL|REDUCE|COVER|TARGET_BREACH|TAKE_PROFIT)\b")
    offending = sorted(lit for lit in literals if exit_words.search(lit))
    assert offending == [], f"exit action word(s) in live code: {offending}"
    # Pure: no broker, DB, market-data or LLM dependency is even imported.
    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    for banned in ("broker", "storage", "db", "anthropic", "openai", "agents"):
        assert not any(banned in name for name in imported), (
            f"{banned!r} reached by {sorted(imported)}"
        )


def test_flag_schema_cannot_carry_a_typed_target_price():
    """A revision is a RE-DERIVATION. The seat supplies evidence; the code
    supplies the number. A model-typed target would be board item 80 (stop
    provenance) reappearing on the field that feeds pace and the guard."""
    assert set(TargetRevisionFlag.model_fields) == {"symbol", "evidence"}
    # A price typed anyway is dropped, not stored, and is unreachable.
    flag = TargetRevisionFlag(
        symbol="aapl", evidence="gapped through and closed above 214",
        target_price=999.0, take_profit=999.0, new_target=999.0,
    )
    assert flag.model_dump() == {
        "symbol": "AAPL", "evidence": "gapped through and closed above 214",
    }
    for attr in ("target_price", "take_profit", "new_target"):
        assert getattr(flag, attr, "ABSENT") == "ABSENT"


# ---------------------------------------------------------------------------
# Trigger discipline — a revision needs a structural event
# ---------------------------------------------------------------------------

_COMMON = dict(
    symbol="AAPL",
    direction="long",
    entry_price=100.0,
    pinned_horizon_sessions=10,
    setup_type=None,
    levels_coverage=COVERAGE_MEASURED,
)


def test_an_opinion_is_refused_by_name_not_silently_ignored():
    # Entry 100, ATR 2.5, horizon 10: reach 11.86, noise floor 2.50 — a
    # target at 110 is exactly what the derivation produces, and today's
    # bars change nothing. Price is up but the ceiling is intact.
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0, levels=[110.0, 95.0],
        atr=2.5, close_price=104.0, break_seen_prior_close=False, **_COMMON,
    )
    assert not out.revised
    assert out.code == tr.REVISION_NO_TRIGGER
    assert out.refusal and not out.fault
    assert out.detail  # never a blank


def test_a_one_day_break_is_pending_confirmation_not_a_revision():
    """Same two-consecutive-closes gate as
    `exit_guard.check_structural_protection` — a one-day spring is not a
    break."""
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0, levels=[110.0, 128.0, 95.0],
        # The gap expands ATR, as a real one does.
        atr=6.0,
        # A close more than one noise band above the broken level.
        close_price=118.0,
        break_seen_prior_close=False, **_COMMON,
    )
    assert out.code == tr.REVISION_BREAK_PENDING_CONFIRMATION
    assert out.new_price is None


def test_a_confirmed_level_break_re_derives_on_todays_bars():
    # The owner's motivating case, end to end. Entry 100 with a target on
    # the 110 resistance; the name gaps through on earnings and closes 118
    # with ATR expanded 2.5 -> 6.0, which widens `horizon_reach` from 11.86
    # to 28.46 and brings the next level up, 128, into reach.
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0,
        levels=[110.0, 128.0, 95.0], atr=6.0, close_price=118.0,
        break_seen_prior_close=True, **_COMMON,
    )
    assert out.revised
    assert out.trigger == tr.TRIGGER_LEVEL_BROKEN
    assert out.new_price == pytest.approx(128.0)
    assert out.basis == "structural_level"


def test_the_re_derivation_reuses_the_pinned_horizon_and_never_recomputes():
    """`derive_structural_target` refuses without a horizon, and the horizon
    is pinned at BUY. A position with no pinned horizon is refused by name
    rather than having one guessed for it."""
    args = dict(_COMMON)
    args["pinned_horizon_sessions"] = None
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0, levels=[110.0, 128.0],
        atr=6.0, close_price=118.0, break_seen_prior_close=True, **args,
    )
    assert out.code == tr.REVISION_NO_PINNED_HORIZON
    assert out.new_price is None
    # And the module never reaches for a horizon of its own.
    body = pathlib.Path(tr.__file__).read_text().split('"""', 2)[2]
    assert "trading_sessions_held" not in body
    assert "days_held" not in body


def test_the_atr_trigger_introduces_no_new_constant():
    """"ATR changed enough" is read off the derivation's OWN acceptance
    bounds — the noise floor and `horizon_reach`. A bare numeric literal
    used as a threshold here would be an arbitrary number on the live risk
    path (docs/WORK.md, no-arbitrary-numbers)."""
    body = pathlib.Path(tr.__file__).read_text().split('"""', 2)[2]
    signature = inspect.signature(tr.stale_reach_trigger)
    defaults = {
        name: p.default for name, p in signature.parameters.items()
        if p.default is not inspect.Parameter.empty
    }
    # Every bound comes from src.data.levels, not from a literal here.
    from src.data import levels as lv
    assert defaults["min_target_atr_multiple"] == lv.MIN_TARGET_ATR_MULTIPLE
    assert defaults["max_reach_atr_multiple"] == lv.MAX_REACH_ATR_MULTIPLE
    assert defaults["max_horizon_sessions"] == lv.MAX_HORIZON_SESSIONS
    # No float literal other than 0/100.0-style arithmetic scaffolding.
    body_after_helpers = body[body.index("def stale_reach_trigger"):]
    body_after_helpers = body_after_helpers[:body_after_helpers.index("def assess_target_revision")]
    literals = re.findall(r"(?<![\w.])\d+\.\d+(?![\w.])", body_after_helpers)
    assert literals == [], f"invented threshold literal(s): {literals}"


def test_a_target_now_beyond_todays_reach_is_a_trigger():
    """Volatility collapsed, so the stored target is no longer reachable
    inside the pinned horizon — the derivation would not accept it today."""
    trigger = tr.stale_reach_trigger(
        entry_price=100.0, stored_target=140.0, atr=0.5, horizon_sessions=10,
    )
    assert trigger == tr.TRIGGER_TARGET_BEYOND_REACH


def test_a_target_now_inside_todays_noise_floor_is_a_trigger():
    """Volatility exploded, so the stored target no longer clears the
    instrument's own daily range."""
    trigger = tr.stale_reach_trigger(
        entry_price=100.0, stored_target=101.0, atr=8.0, horizon_sessions=10,
    )
    assert trigger == tr.TRIGGER_TARGET_INSIDE_NOISE


def test_an_unchanged_target_under_unchanged_volatility_is_not_a_trigger():
    # ATR 2.5 over 10 sessions: noise floor 2.50, reach 11.86. A target 10
    # away from entry sits inside both, which is why the derivation picked
    # it in the first place.
    assert tr.stale_reach_trigger(
        entry_price=100.0, stored_target=110.0, atr=2.5, horizon_sessions=10,
    ) == ""


def test_a_data_fault_is_recorded_as_a_fault_not_a_refusal():
    """The desk's own split: a missing measurable input is a FAULT, a real
    geometry judgement is a REFUSAL. They must never be confused."""
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0, levels=[110.0],
        atr=None, close_price=None, break_seen_prior_close=True, **_COMMON,
    )
    assert out.code == tr.REVISION_UNMEASURABLE_INPUTS
    assert out.fault and not out.refusal


def test_a_refused_re_derivation_leaves_the_old_target_standing():
    """A trigger fired, but the close has cleared EVERY level on the chart,
    so today's bars hold nothing left in the trade's direction. The old
    target stands, under a code that does not claim the chart is empty."""
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0,
        levels=[110.0, 95.0, 90.0], atr=6.0, close_price=118.0,
        break_seen_prior_close=True, **_COMMON,
    )
    assert out.new_price is None
    assert out.prior_price == pytest.approx(110.0)
    assert out.code == tr.REVISION_NO_CEILING_LEFT
    assert out.trigger == tr.TRIGGER_LEVEL_BROKEN
    assert out.detail


def test_a_broken_level_is_never_handed_back_as_the_new_target():
    """`find_structural_levels` keeps returning a ceiling price has gapped
    through, and `derive_structural_target` partitions against ENTRY — so
    without `levels_still_in_the_way` the re-derivation re-picks the very
    level that just broke and the revision is a no-op in exactly the case
    it exists for."""
    surviving = tr.levels_still_in_the_way(
        computed_levels=[110.0, 128.0, 95.0], close_price=118.0, atr=6.0,
        is_short=False,
    )
    assert surviving == [128.0]
    # A short's cleared floors drop the other way: with a close of 88 and a
    # 6.0 band, 95 has been fallen through (88 <= 89) and 90 has not.
    assert tr.levels_still_in_the_way(
        computed_levels=[95.0, 90.0], close_price=88.0, atr=6.0,
        is_short=True,
    ) == [90.0]
    # Missing inputs must not silently empty the set.
    assert tr.levels_still_in_the_way(
        computed_levels=[110.0, 95.0], close_price=None, atr=6.0,
        is_short=False,
    ) == [110.0, 95.0]


def test_a_target_the_price_has_already_passed_is_refused_not_stored():
    """The honest cost of holding entry and horizon fixed: reach is measured
    from entry, so after a run the only derivable target can be behind the
    price. Refused by name; the old target stands."""
    out = tr.assess_target_revision(
        stored_target=110.0, target_level=110.0,
        # The 110 ceiling is broken (close 117 is a full 6.0 noise band
        # above it) so the trigger fires; 116 survives the filter and is
        # well inside reach of entry — but the close is already past it.
        levels=[110.0, 116.0], atr=6.0, close_price=117.0,
        break_seen_prior_close=True, **_COMMON,
    )
    assert out.code == tr.REVISION_BEHIND_PRICE
    assert out.new_price is None
    assert out.prior_price == pytest.approx(110.0)


def test_shorts_break_their_target_level_downward():
    """Direction is the mirror image, not a long-only rule with a patch."""
    assert tr.target_level_broken(
        target_level=90.0, close_price=90.0 - 2.5 * NOISE_BAND_ATR_MULTIPLE,
        atr=2.5, is_short=True,
    ) is True
    assert tr.target_level_broken(
        target_level=90.0, close_price=90.0 - 2.5 * NOISE_BAND_ATR_MULTIPLE,
        atr=2.5, is_short=False,
    ) is False
    # Unanswerable rather than "not broken" when an input is missing.
    assert tr.target_level_broken(
        target_level=None, close_price=100.0, atr=2.5, is_short=False,
    ) is None


def test_a_measured_move_target_has_no_level_to_break():
    """`level_backing_target` returns None when nothing sits in the stored
    target's own cluster zone — correct for a measured-move target, which
    was never measured against a level."""
    assert tr.level_backing_target(
        stored_target=110.0, computed_levels=[95.0, 128.0],
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    ) is None
    assert tr.level_backing_target(
        stored_target=110.0, computed_levels=[95.0, 110.2, 128.0],
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    ) == pytest.approx(110.2)


def test_every_outcome_carries_a_machine_code():
    """A refusal is a first-class outcome. No path returns a blank."""
    for kwargs in (
        dict(stored_target=None, target_level=None, levels=[], atr=2.5,
             close_price=104.0),
        dict(stored_target=110.0, target_level=110.0, levels=[110.0],
             atr=2.5, close_price=104.0),
        dict(stored_target=110.0, target_level=110.0, levels=[110.0],
             atr=None, close_price=None),
    ):
        out = tr.assess_target_revision(break_seen_prior_close=False,
                                        **kwargs, **_COMMON)
        assert out.code, f"blank outcome for {kwargs}"
        assert out.detail, f"blank detail for {kwargs}"
