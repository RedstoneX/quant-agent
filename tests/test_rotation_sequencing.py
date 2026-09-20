"""Board item 39 — a rotation SELL may not fire ahead of a replacement BUY
that is going to be refused.

The defect these pin: the `ranked_margin` rotation tier sells a held
position purely to fund a specific replacement. Every gate that can refuse
that replacement runs LATER in `ExecutionStage._run_session` than the SELL
loop does, so the desk could end the session sold out of a position with
nothing bought in its place and only an owner alert behind it.

Two earlier fixes failed review:

  * attempt 1 closed one refusal path and turned the structural barrier in
    front of the tier into a config boolean, which is a weaker guard than
    the one it replaced;
  * attempt 2 re-ran the daily-loss gate using PRE-SALE account state. The
    real gate recomputes from `ctx.positions` AFTER the sale, on BOTH sides
    of its comparison — the numerator is the held book's own intraday
    change and the volatility-relative rung of the threshold is measured
    from the held book too — so a pre-sale check is structurally blind to
    the state change the real check depends on.

What is pinned here:

  * a winner sold out of a losing book: pre-sale the daily-loss gate
    passes, post-sale it trips, and the fix withdraws BOTH legs rather
    than selling into a buy that will be refused (the failure attempt 2
    cannot see);
  * the projection moves the THRESHOLD as well as the loss;
  * each of the four downstream refusal paths — daily-loss, no_price,
    stale_entry, qty_zero — withdraws both legs;
  * exits earlier in the SAME session are applied to the projection too;
  * the barrier in front of the sale is a `RotationClearance` object and
    not a flag: `rotation_sell_reason` raises without one, raises for one
    minted for a different pair, and raises for one missing a gate, with no
    config read anywhere in the refusal;
  * `REQUIRED_BUY_LEG_GATES` and the gate function cannot silently stop
    agreeing;
  * the flag stays OFF by default and a MagicMock config never reads as on.
"""
from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from src import pipeline_stages as ps
from src.config import ExecutionConfig, RiskConfig
from src.models import Position
from src.pipeline_context import RunContext
from src.risk.rules import RiskRuleEngine
from src.rotation import (
    REQUIRED_BUY_LEG_GATES,
    RotationClearance,
    RotationOpportunity,
    rotation_proposal_reason,
    rotation_sell_reason,
)
from src.storage.db import Database

BROKEN_DETAIL = "structural level 100.0 has closed beyond it twice"

#: The repo root, so a test that reads a shipped file works whatever
#: directory pytest was started from.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _risk_kwargs(**overrides) -> dict:
    """A complete `RiskConfig` — every required field, no daily limit unless
    a test asks for one."""
    base = dict(
        max_position_pct=20.0, max_total_position_pct=90.0,
        max_sector_pct=40.0, require_stop_loss=True, allow_margin=False,
    )
    base.update(overrides)
    return base


#: The held book's daily volatility that makes the volatility-relative rung
#: of the daily-loss limit come out at exactly 2% of equity
#: (`vol_relative_drawdown_threshold_pct` = sensitivity x sigma x sqrt(1)).
#: Derived from the config's own sensitivity rather than hardcoded, so a
#: change to that default retunes these fixtures instead of silently
#: changing what they prove.
#:
#: The vol-relative rung matters here specifically: it is the ONLY basis
#: whose numerator is the held book (`daily_loss_numerator`), and the held
#: book is what a sale changes. Under either fixed-percentage rung the
#: numerator is the account's day change, which a sale barely moves — so
#: the defect this file is about lives on this rung.
SIGMA_FOR_2PCT = 2.0 / RiskConfig(**_risk_kwargs()).drawdown_vol_sensitivity


# ---------------------------------------------------------------------------
# Fixtures — a pipeline stub with the REAL risk engine and the REAL
# daily-loss arithmetic behind it.
# ---------------------------------------------------------------------------

def _pos(symbol, qty=10.0, price=100.0, intraday=0.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=100.0, current_price=price,
        market_value=qty * price, unrealized_pnl=0.0,
        unrealized_intraday_pnl=intraday, sector="Technology",
    )


def _buy(symbol="NEW", allocation_pct=2.0, entry_price=50.0, stop_loss=45.0):
    return SimpleNamespace(
        symbol=symbol, action="BUY", allocation_pct=allocation_pct,
        entry_price=entry_price, stop_loss=stop_loss, reasoning="best idea",
    )


def _sell(symbol="OLD", allocation_pct=100.0):
    return SimpleNamespace(
        symbol=symbol, action="SELL", allocation_pct=allocation_pct,
        entry_price=0.0, stop_loss=0.0, reasoning="rotation close",
    )


def _opportunity() -> RotationOpportunity:
    return RotationOpportunity(
        new_symbol="NEW", new_score=4.0, held_symbol="OLD", held_score=2.0,
        tier="ranked_margin", shared_seats=("technical",),
        held_shared_score=2.0, new_shared_score=4.0,
    )


def _pipeline(tmp_path, *, positions=None, total_value=100_000.0,
              cash=50_000.0, vol_by_book=None, prices=None):
    """A pipeline stub carrying a REAL `RiskRuleEngine`.

    `vol_by_book` maps a frozenset of remaining symbols to the held book's
    daily volatility in percent, so a test can make the THRESHOLD depend on
    which names are still held — which is what the real
    `held_book_daily_vol_pct` does, and what a pre-sale check cannot see.
    """
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    risk_config = RiskConfig(**_risk_kwargs())
    pipeline = SimpleNamespace()
    pipeline.db = db
    pipeline.config = SimpleNamespace(
        execution=ExecutionConfig(
            rotation_enabled=True, rotation_ranked_margin_enabled=True,
        ),
        cash_sweep=SimpleNamespace(symbol="SGOV"),
        risk=risk_config,
    )
    pipeline.risk_engine = RiskRuleEngine(
        risk_config, portfolio_vol_provider=lambda: SIGMA_FOR_2PCT,
    )
    pipeline._prices = prices if prices is not None else {"NEW": 50.0}

    def _vol(positions=None, equity=None):
        if vol_by_book is None:
            return None
        key = frozenset(
            str(getattr(p, "symbol", "")).upper() for p in (positions or [])
        )
        return vol_by_book.get(key)

    pipeline.held_book_daily_vol_pct = _vol

    #: The gate refreshes the account before projecting, because the state
    #: this stage is handed is the research snapshot from 5-10 minutes
    #: earlier. The stub hands back exactly what the test set up, so a
    #: fixture stays in control of the numbers.
    pipeline.refreshed = []

    def _refresh():
        pipeline.refreshed.append(True)
        return (
            {"cash": pipeline.stub_cash,
             "portfolio_value": pipeline.stub_total_value},
            list(pipeline.stub_positions), {},
        )

    pipeline.stub_cash = cash
    pipeline.stub_total_value = total_value
    pipeline.stub_positions = list(positions or [])
    pipeline._refresh_account_state = _refresh
    return pipeline, db


def _ctx(positions, total_value=100_000.0, last_equity=100_000.0,
         rotation=True) -> RunContext:
    ctx = RunContext(run_id="run-1", session="morning")
    ctx.positions = list(positions)
    ctx.total_value = total_value
    ctx.last_equity = last_equity
    if rotation:
        ctx.rotation = {
            "held_symbol": "OLD", "new_symbol": "NEW", "tier": "ranked_margin",
            "clearance": None, "opportunity": _opportunity(),
            "protection_basis_text": "structural_level_broken",
            "protection_detail_text": BROKEN_DETAIL,
            "headroom_pct": 0.2, "ceiling_pct": 25.0, "floor_pct": 0.5,
            "reason": "proposal text",
        }
    return ctx


def _stub_sizing(monkeypatch, *, qty=40.0, risk_qty=40.0, price=50.0,
                 budget=1_000_000.0, single_name_cap=1_000_000.0,
                 min_order_usd=100.0):
    """Hold the price, size and funding helpers still so a test can aim at
    ONE gate at a time."""
    monkeypatch.setattr(
        ps, "_entry_deployment_budget",
        lambda pipeline, ctx, positions, equity, cash: (
            budget, True, "stubbed budget",
        ),
    )
    monkeypatch.setattr(
        ps, "_single_name_execution_cap",
        lambda pipeline, equity: single_name_cap,
    )
    monkeypatch.setattr(ps, "_min_order_usd", lambda pipeline: min_order_usd)
    monkeypatch.setattr(
        ps, "_live_fill_price", lambda pipeline, symbol: price,
    )
    monkeypatch.setattr(
        ps, "_fractional_sizing_allowed",
        lambda pipeline, symbol, *, is_short: False,
    )
    monkeypatch.setattr(
        ps, "_size_shares",
        # Whole-share quantization, capped at what the test asked for, so a
        # budget the fixture shrinks actually shrinks the order.
        lambda pipeline, raw, *, fractional: min(qty, float(int(raw))),
    )
    monkeypatch.setattr(
        ps, "_qty_by_risk_budget",
        lambda pipeline, **kwargs: risk_qty,
    )


def _gate(pipeline, ctx, positions, *, sells=None, buys=None, cash=50_000.0,
          total_value=100_000.0):
    """Run the rotation's own close through `_rotation_sell_gate`, the way
    the SELL loop does — the rotation's close is ordered LAST, so by the
    time it is reached the other exits have already run and the account is
    re-read rather than projected."""
    ordered = ps._rotation_sell_last(
        list(sells) if sells is not None else [_sell()], ctx,
    )
    decision = ordered[-1]
    result = ps._rotation_sell_gate(
        pipeline, ctx, decision, list(buys) if buys is not None else [_buy()],
        positions, total_value, cash,
    )
    return result, ordered


def _events(db, run_id="run-1") -> list[dict]:
    rows = db.conn.execute(
        "SELECT evidence_json FROM specialist_evidence WHERE run_id = ? "
        "ORDER BY id", (run_id,),
    ).fetchall()
    return [json.loads(r["evidence_json"]) for r in rows]


def _rotation_events(db) -> list[dict]:
    return [e for e in _events(db) if e.get("stage") == "rotation"]


# ---------------------------------------------------------------------------
# The failure this closes — the one attempt 2's pre-sale check cannot see
# ---------------------------------------------------------------------------

def test_selling_a_winner_out_of_a_losing_book_trips_the_real_gate(
    tmp_path, monkeypatch,
):
    """OLD is UP today; the rest of the book is DOWN. Before the sale the
    held book's day change is -$400 and the breaker is quiet. The sale
    removes OLD's +$500 from the very number the REAL post-sale check
    recomputes, leaving -$900 — over the 2% limit on a $100k account — so
    the replacement BUY would be refused and the desk would be left naked.

    A pre-sale check sees -$400 and clears the sale. This one projects the
    post-sale book and refuses it.
    """
    _stub_sizing(monkeypatch)
    positions = [
        _pos("OLD", qty=10.0, price=100.0, intraday=500.0),
        _pos("KEEP", qty=10.0, price=100.0, intraday=-2500.0),
    ]
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    sell, buy = _sell(), _buy()

    # Sanity: the PRE-sale number does not trip the breaker. This is
    # precisely what attempt 2 measured, and why it cleared the sale.
    from src.risk.rules import daily_loss_numerator
    pre_sale_pnl, _ = daily_loss_numerator(
        0.0, positions, vol_relative=True, cash_park_symbol="SGOV",
    )
    assert pre_sale_pnl == -2000.0
    assert pipeline.risk_engine.check_daily_loss(100_000.0, pre_sale_pnl) is None

    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[sell], buys=[buy],
    )

    assert cleared is False, "the rotation SELL must not reach the wire"
    assert ctx.rotation["withdrawn"].startswith("buy_leg_would_be_refused")
    assert ctx.rotation.get("sell_order_id") is None
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:daily_loss_recheck"
    assert "-2500.00" in withdrawn["detail"]
    skips = {s["reason"] for s in ctx.execution_skips}
    assert skips == {"daily_loss_recheck", "rotation_withdrawn"}


def test_the_projection_moves_the_threshold_not_only_the_loss(
    tmp_path, monkeypatch,
):
    """The volatility-relative rung of the daily limit is measured FROM the
    held book, so selling a holding moves the threshold too. Same day
    change either way here; only the surviving book differs, and that alone
    decides the outcome.
    """
    _stub_sizing(monkeypatch)
    positions = [
        _pos("OLD", qty=10.0, price=100.0, intraday=0.0),
        _pos("KEEP", qty=10.0, price=100.0, intraday=-1500.0),
    ]  # -$1,500 all session: inside the wide limit, outside the tight one
    # No explicit max_daily_loss_pct, so the vol-relative rung governs.
    wide, tight = SIGMA_FOR_2PCT * 4, SIGMA_FOR_2PCT * 0.25
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        # Pre-sale the book looks volatile, so a wide limit; post-sale, with
        # the volatile name gone, the measured volatility collapses and the
        # limit tightens under the loss that is still there.
        frozenset({"OLD", "KEEP"}): wide,
        frozenset({"KEEP"}): tight,
    })
    pipeline.risk_engine = RiskRuleEngine(
        RiskConfig(**_risk_kwargs()), portfolio_vol_provider=lambda: wide,
    )
    ctx = _ctx(positions)

    pre_sale_limit = pipeline.risk_engine.daily_loss_limit_pct
    post_sale_limit, _basis = pipeline.risk_engine._daily_loss_limit_and_basis(
        portfolio_vol_pct=tight, use_supplied_vol=True,
    )
    assert post_sale_limit < pre_sale_limit, (
        "the fixture must actually tighten the limit, or this test proves "
        "nothing about the threshold"
    )

    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[_sell()], buys=[_buy()],
    )
    assert cleared is False
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:daily_loss_recheck"


# ---------------------------------------------------------------------------
# Each of the four downstream refusal paths withdraws BOTH legs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate", REQUIRED_BUY_LEG_GATES)
def test_every_required_gate_withdraws_both_legs(tmp_path, monkeypatch, gate):
    positions = [_pos("OLD", intraday=0.0), _pos("KEEP", intraday=0.0)]
    vol = {
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    }
    if gate == "daily_loss_recheck":
        positions = [
            _pos("OLD", intraday=500.0), _pos("KEEP", intraday=-2500.0),
        ]
        _stub_sizing(monkeypatch)
    elif gate == "no_price":
        _stub_sizing(monkeypatch)
        monkeypatch.setattr(ps, "_live_fill_price", lambda p, s: None)
    elif gate == "stale_entry":
        # The PM's entry is 40% away from the live market.
        _stub_sizing(monkeypatch, price=80.0)
    elif gate == "qty_zero":
        _stub_sizing(monkeypatch, qty=0.0)
    elif gate == "insufficient_cash":
        # 40 shares at $50 is $2,000 of notional; nothing is deployable.
        _stub_sizing(monkeypatch, budget=0.0)
    elif gate == "below_min_notional":
        # Enough deployable for 1 share, under the $500 minimum worth
        # trading, so the re-size lands below the floor.
        _stub_sizing(monkeypatch, budget=60.0, min_order_usd=500.0)
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book=vol)
    ctx = _ctx(positions)

    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[_sell()], buys=[_buy()],
    )

    assert cleared is False, f"{gate}: the SELL must be withdrawn"
    assert ctx.rotation.get("sell_order_id") is None
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == f"buy_leg_would_be_refused:{gate}"
    assert gate in {s["reason"] for s in ctx.execution_skips}


def test_qty_zero_from_the_risk_budget_alone_also_withdraws(
    tmp_path, monkeypatch,
):
    """The allocation rounds to a real size but the risk budget cannot
    carry one orderable unit — the second `qty_zero` exit in the preflight,
    and a separate code path from the first."""
    _stub_sizing(monkeypatch, qty=40.0, risk_qty=0.0)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[_sell()], buys=[_buy()],
    )
    assert cleared is False
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:qty_zero"
    assert "risk budget" in withdrawn["detail"]


# ---------------------------------------------------------------------------
# Earlier exits in the SAME session are part of the projection
# ---------------------------------------------------------------------------

def test_the_rotations_close_is_ordered_last_among_this_sessions_exits(tmp_path):
    """Everything else this session sells goes first, so that when the
    rotation's own close is gated, the other exits are a measurement
    instead of an assumption. Their relative order is preserved."""
    positions = [_pos("OLD"), _pos("EARLY"), _pos("LATER"), _pos("KEEP")]
    _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    ordered = ps._rotation_sell_last(
        [_sell("EARLY"), _sell("OLD"), _sell("LATER")], ctx,
    )
    assert [d.symbol for d in ordered] == ["EARLY", "LATER", "OLD"]

    # A session with no ranked-margin rotation is untouched.
    ctx.rotation = None
    same = [_sell("EARLY"), _sell("OLD")]
    assert ps._rotation_sell_last(same, ctx) is same


def test_a_partial_exit_leaves_a_proportional_share_of_its_intraday_pnl(
    tmp_path,
):
    """Selling 40% of a name leaves 60% of its intraday change on the books
    — the field the daily-loss numerator actually reads."""
    positions = [_pos("PART", qty=10.0, intraday=1000.0)]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    projected, equity, account_pnl = ps._projected_post_sale_book(
        pipeline, ctx, positions, 100_000.0, [_sell("PART", 40.0)], [],
    )
    remaining, = projected
    # Whole-share position: 40% of 10 floors to 4 shares sold, 6 left.
    assert remaining.qty == pytest.approx(6.0)
    assert remaining.unrealized_intraday_pnl == pytest.approx(600.0)
    assert equity == 100_000.0
    # The limit concession on 4 shares marked at $100 sold at $99.50.
    assert account_pnl == pytest.approx(-2.0)


def test_a_full_exit_removes_the_position_entirely(tmp_path):
    positions = [_pos("GONE", qty=10.0, intraday=1000.0), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    projected, _equity, _pnl = ps._projected_post_sale_book(
        pipeline, ctx, positions, 100_000.0, [_sell("GONE")], [],
    )
    assert [p.symbol for p in projected] == ["KEEP"]


# ---------------------------------------------------------------------------
# The clean path: a clearance is minted and the sale becomes buildable
# ---------------------------------------------------------------------------

def test_a_clean_projection_mints_a_clearance_and_keeps_both_legs(
    tmp_path, monkeypatch,
):
    _stub_sizing(monkeypatch)
    positions = [_pos("OLD", intraday=100.0), _pos("KEEP", intraday=100.0)]
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    sell, buy = _sell(), _buy()

    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[sell], buys=[buy],
    )
    assert cleared is True

    clearance = ctx.rotation["clearance"]
    assert isinstance(clearance, RotationClearance)
    assert clearance.covers(held_symbol="OLD", new_symbol="NEW")
    assert set(clearance.gates_checked) >= set(REQUIRED_BUY_LEG_GATES)
    assert clearance.projected_positions == ("KEEP",)

    cleared, = [
        e for e in _rotation_events(db) if e["outcome"] == "buy_leg_cleared"
    ]
    assert cleared["projected_positions"] == ["KEEP"]

    # And the sale's reason now builds, carrying the projected numbers.
    reason = ps._rotation_ranked_margin_sell_reason(pipeline, ctx, sell)
    assert isinstance(reason, str)
    assert "BUY pre-cleared post-sale" in reason
    assert len(reason) <= 500, "the constructor truncates at 500 characters"


def test_a_missing_buy_leg_withdraws_the_sell(tmp_path):
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[_sell()], buys=[_buy("OTHER")],
    )
    assert cleared is False
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_absent"


def test_a_sell_leg_the_risk_manager_already_refused_is_left_alone(tmp_path):
    """Nothing to withdraw and nothing that can go naked — the existing
    `_drop_rotation_buy_if_room_not_freed` already stops the buy."""
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    result = ps._rotation_sell_gate(
        pipeline, ctx, _sell("SOMETHINGELSE"), [_buy()], positions,
        100_000.0, 50_000.0,
    )
    assert result is None, "not the rotation's own close"
    assert _rotation_events(db) == []


def test_the_categorical_tier_is_untouched_by_all_of_this(tmp_path):
    """`ineligible_hold` is already live and sells a holding the desk's own
    rules say it would not buy today. That sale stands on its own and is
    not withdrawn when the replacement falls through."""
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    ctx.rotation["tier"] = "ineligible_hold"
    result, _ordered = _gate(pipeline, ctx, positions)
    assert result is None, "the categorical tier is not gated here"
    assert _rotation_events(db) == []


def test_a_session_with_no_rotation_is_a_no_op(tmp_path):
    positions = [_pos("OLD")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    sell = _sell()
    assert ps._rotation_sell_last([sell], ctx) == [sell]
    assert ps._rotation_sell_gate(
        pipeline, ctx, sell, [_buy()], positions, 100_000.0, 50_000.0,
    ) is None
    assert _events(db) == []


# ---------------------------------------------------------------------------
# The barrier is an object carrying evidence, not a flag
# ---------------------------------------------------------------------------

def _reason_kwargs():
    return dict(
        protection_basis="structural_level_broken",
        protection_detail=BROKEN_DETAIL, headroom_pct=0.2, ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_ranked_margin_sale_cannot_be_built_without_a_clearance():
    with pytest.raises(ValueError, match="RotationClearance"):
        rotation_sell_reason(_opportunity(), **_reason_kwargs())


@pytest.mark.parametrize("substitute", [True, 1, "yes", {"cleared": True}, object()])
def test_no_truthy_value_substitutes_for_a_clearance(substitute):
    """Board item 39, attempt 1: the guard in front of this tier was a
    config boolean, so one truthy value put a real sale on the wire. A
    truthy value is not evidence and is refused here."""
    with pytest.raises(ValueError, match="RotationClearance"):
        rotation_sell_reason(
            _opportunity(), clearance=substitute, **_reason_kwargs()
        )


def _clearance(**overrides) -> RotationClearance:
    base = dict(
        held_symbol="OLD", new_symbol="NEW",
        gates_checked=tuple(REQUIRED_BUY_LEG_GATES),
        projected_daily_pnl=-100.0, projected_basis="held_book",
        projected_positions=("KEEP",), projected_equity=100_000.0,
    )
    base.update(overrides)
    return RotationClearance(**base)


@pytest.mark.parametrize("bad", [
    {"held_symbol": "SOMETHINGELSE"},
    {"new_symbol": "SOMETHINGELSE"},
    {"gates_checked": ("no_price", "stale_entry", "qty_zero")},
    {"gates_checked": ()},
])
def test_a_clearance_for_a_different_sale_or_a_partial_check_is_refused(bad):
    with pytest.raises(ValueError, match="not for|RotationClearance"):
        rotation_sell_reason(
            _opportunity(), clearance=_clearance(**bad), **_reason_kwargs()
        )


def test_a_real_clearance_builds_a_reason_naming_what_it_was_cleared_on():
    reason = rotation_sell_reason(
        _opportunity(), clearance=_clearance(), **_reason_kwargs()
    )
    assert "ranked margin" in reason
    assert "OLD" in reason and "NEW" in reason
    assert "technical" in reason, "the like-for-like seats must be named"
    assert "held_book" in reason and "$-100" in reason


def test_the_proposal_reason_says_the_close_is_contingent():
    """What the Risk Manager reviews. It is not an authorisation, and it
    says so in the text the reviewer reads."""
    proposal = rotation_proposal_reason(_opportunity(), **_reason_kwargs())
    assert "CONTINGENT" in proposal


def test_the_wire_barrier_refuses_and_records_when_no_clearance_exists(tmp_path):
    positions = [_pos("OLD")]
    pipeline, db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)  # clearance is None — the gate never cleared it
    assert ps._rotation_ranked_margin_sell_reason(
        pipeline, ctx, _sell(),
    ) is ps._ROTATION_SELL_REFUSED
    refused, = [e for e in _rotation_events(db) if e["outcome"] == "sell_refused"]
    assert refused["reason"] == "no_clearance"
    assert "rotation_sell_refused" in {s["reason"] for s in ctx.execution_skips}


def test_the_wire_barrier_ignores_every_non_rotation_sell(tmp_path):
    positions = [_pos("OLD")]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions)
    assert ps._rotation_ranked_margin_sell_reason(
        pipeline, ctx, _sell("SOMETHINGELSE"),
    ) is None
    ctx.rotation["tier"] = "ineligible_hold"
    assert ps._rotation_ranked_margin_sell_reason(
        pipeline, ctx, _sell(),
    ) is None
    ctx.rotation = None
    assert ps._rotation_ranked_margin_sell_reason(
        pipeline, ctx, _sell(),
    ) is None


# ---------------------------------------------------------------------------
# The list of gates and the code that checks them cannot drift apart
# ---------------------------------------------------------------------------

def test_the_gate_function_evaluates_every_required_gate(tmp_path, monkeypatch):
    _stub_sizing(monkeypatch)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    clearance, reason, _detail = ps._rotation_buy_leg_projected_refusal(
        pipeline, ctx, rotation=ctx.rotation, buy_decision=_buy(),
        positions=positions, total_value=100_000.0, cash=50_000.0,
        rotation_sell=_sell(),
    )
    assert reason is None
    assert set(clearance.gates_checked) == set(REQUIRED_BUY_LEG_GATES), (
        "REQUIRED_BUY_LEG_GATES and the gate function have drifted apart"
    )


def test_required_gates_name_real_execution_skip_reasons():
    """The gate names are the execution stage's own `_record_execution_skip`
    reason codes, so the two can be compared by reading the file."""
    source = (REPO_ROOT / "src" / "pipeline_stages.py").read_text()
    for gate in REQUIRED_BUY_LEG_GATES:
        assert f'"{gate}",' in source, (
            f"{gate} is not a reason code the execution stage records"
        )


# ---------------------------------------------------------------------------
# Default posture
# ---------------------------------------------------------------------------

def test_the_ranked_margin_flag_is_off_by_default_and_needs_both_switches():
    from unittest.mock import MagicMock
    assert ExecutionConfig().rotation_ranked_margin_enabled is False

    def _p(**execution):
        return SimpleNamespace(config=SimpleNamespace(
            execution=SimpleNamespace(**execution),
        ))

    assert ps._rotation_ranked_margin_enabled(
        _p(rotation_enabled=True, rotation_ranked_margin_enabled=True),
    ) is True
    # Either switch off is off.
    assert ps._rotation_ranked_margin_enabled(
        _p(rotation_enabled=False, rotation_ranked_margin_enabled=True),
    ) is False
    assert ps._rotation_ranked_margin_enabled(
        _p(rotation_enabled=True, rotation_ranked_margin_enabled=False),
    ) is False
    # A MagicMock config must never read as "yes, close a real position".
    assert ps._rotation_ranked_margin_enabled(
        SimpleNamespace(config=MagicMock()),
    ) is False
    assert ps._rotation_ranked_margin_enabled(
        _p(rotation_enabled=True, rotation_ranked_margin_enabled=1),
    ) is False


def test_the_shipped_settings_leave_the_ranked_margin_tier_off():
    import yaml
    with open(REPO_ROOT / "config" / "settings.yaml") as handle:
        settings = yaml.safe_load(handle)
    assert settings["execution"]["rotation_ranked_margin_enabled"] is False


def test_the_reason_fits_the_field_it_is_truncated_into():
    """`PortfolioConstructor._build_sell` appends the thesis condition and
    truncates at 500 characters, and the LAST clause is the one naming what
    the sale was cleared on. A worst-case seat list and protection detail
    must still fit, and the builder refuses rather than be silently cut."""
    wide = RotationOpportunity(
        new_symbol="NEWLONGSYM", new_score=44.4444, held_symbol="OLDLONGSYM",
        held_score=22.2222, tier="ranked_margin",
        shared_seats=("technical", "fundamental", "macro", "flow", "news"),
        held_shared_score=22.2222, new_shared_score=44.4444,
    )
    long_detail = "structural level " + ("9" * 300)
    for clearance in (None, _clearance(held_symbol="OLDLONGSYM",
                                       new_symbol="NEWLONGSYM")):
        reason = (
            rotation_sell_reason(
                wide, clearance=clearance,
                protection_basis="structural_level_broken",
                protection_detail=long_detail, headroom_pct=0.2,
                ceiling_pct=25.0, floor_pct=0.5,
            ) if clearance is not None else
            rotation_proposal_reason(
                wide, protection_basis="structural_level_broken",
                protection_detail=long_detail, headroom_pct=0.2,
                ceiling_pct=25.0, floor_pct=0.5,
            )
        )
        assert len(reason) <= 500, len(reason)
    # The contingency clause survives the worst case — it is the whole
    # point of the proposal text.
    assert "CONTINGENT" in rotation_proposal_reason(
        wide, protection_basis="structural_level_broken",
        protection_detail=long_detail, headroom_pct=0.2, ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_an_unrefreshable_account_withdraws_the_rotation(tmp_path, monkeypatch):
    """The state this stage is handed is the research snapshot from five to
    ten minutes earlier, and projecting from a stale higher equity on a
    falling tape clears sales reality would refuse. The gate refreshes
    first, and a refresh it cannot make is a refusal to act."""
    _stub_sizing(monkeypatch)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions)

    def _boom():
        raise RuntimeError("broker unreachable")

    pipeline._refresh_account_state = _boom
    ctx = _ctx(positions)
    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, positions, sells=[_sell()], buys=[_buy()],
    )
    assert cleared is False
    assert ctx.rotation["withdrawn"] == "account_refresh_failed"
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "account_refresh_failed"


def test_a_cover_is_projected_as_a_real_close_not_a_no_op(tmp_path):
    """A short is carried at a NEGATIVE quantity. Sizing the projection off
    the signed number made every COVER a no-op, which left a covered short
    in the projected book contributing an intraday change that will not be
    there — a projection looser than reality."""
    short = _pos("SHRT", qty=-10.0, price=100.0, intraday=800.0)
    positions = [short, _pos("KEEP", intraday=-100.0)]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)
    cover = SimpleNamespace(
        symbol="SHRT", action="COVER", allocation_pct=100.0,
        entry_price=0.0, stop_loss=0.0, reasoning="cover",
    )
    projected, _equity, _pnl = ps._projected_post_sale_book(
        pipeline, ctx, positions, 100_000.0, [], [cover],
    )
    assert [p.symbol for p in projected] == ["KEEP"], (
        "the covered short must leave the projected book"
    )
    # And covering SPENDS cash, at the 1.005 mirror of the sell cushion.
    cash = ps._projected_post_sale_cash(50_000.0, positions, [], [cover])
    assert cash == pytest.approx(50_000.0 - 100.50 * 10)


def test_earlier_entries_drain_the_projected_budget_first(tmp_path, monkeypatch):
    """The submit loop subtracts each order's cost from the pool as it
    walks the list, so an entry ahead of the rotation's own buy has already
    taken its share by the time the rotation's is reached."""
    _stub_sizing(monkeypatch, budget=2_100.0, min_order_usd=500.0)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    earlier = _buy("FIRST", allocation_pct=2.0)  # $2,000 of a $100k book
    rotation_buy = _buy()  # 40 shares at $50 = $2,000

    # Alone it fits the $2,100 pool.
    (cleared, *_rest), _ordered = _gate(
        pipeline, ctx, positions, buys=[rotation_buy],
    )
    assert cleared is True

    # Behind the earlier entry it does not: $2,100 - $2,000 leaves $100,
    # which re-sizes the order under the $500 minimum worth trading.
    ctx = _ctx(positions)
    (cleared, *_rest), _ordered = _gate(
        pipeline, ctx, positions, sells=[_sell()],
        buys=[earlier, rotation_buy],
    )
    assert cleared is False


# ---------------------------------------------------------------------------
# Second adversary pass — the action/sign pairing, the short's pool draw,
# the unfilled-exit assumption, and the refreshed state reaching the loop
# ---------------------------------------------------------------------------

def test_a_sell_on_a_short_is_not_projected_as_a_close(tmp_path):
    """The SELL loop refuses a SELL on a short and the COVER loop refuses a
    COVER on a long. A projection that closed either removes exposure the
    real session keeps — and a book that keeps a LOSING position the
    projection dropped is more negative than the projection said."""
    short = _pos("SHRT", qty=-10.0, price=100.0, intraday=-400.0)
    long_ = _pos("LONG", qty=10.0, price=100.0, intraday=-400.0)
    positions = [short, long_]
    pipeline, _db = _pipeline(tmp_path, positions=positions)
    ctx = _ctx(positions, rotation=False)

    # A SELL aimed at the short: the loop skips it, so must the projection.
    projected, _e, _p = ps._projected_post_sale_book(
        pipeline, ctx, positions, 100_000.0, [_sell("SHRT")], [],
    )
    assert {p.symbol for p in projected} == {"SHRT", "LONG"}

    # A COVER aimed at the long: likewise.
    cover_long = SimpleNamespace(
        symbol="LONG", action="COVER", allocation_pct=100.0,
        entry_price=0.0, stop_loss=0.0, reasoning="cover",
    )
    projected, _e, _p = ps._projected_post_sale_book(
        pipeline, ctx, positions, 100_000.0, [], [cover_long],
    )
    assert {p.symbol for p in projected} == {"SHRT", "LONG"}


def test_an_earlier_short_draws_a_gross_pool_but_not_a_cash_one():
    """`if budget_is_gross or not is_short: entry_budget -= estimated_cost`
    — a short occupies gross exactly as a long does, so it draws the ladder
    pool. It does not touch the settled-cash fallback (D11)."""
    short = _buy("SHORTED", allocation_pct=10.0)
    short.action = "SHORT"
    assert ps._projected_entry_cost(
        short, 100_000.0, budget_is_gross=True,
    ) == pytest.approx(10_000.0)
    assert ps._projected_entry_cost(
        short, 100_000.0, budget_is_gross=False,
    ) == 0.0
    # A long draws either pool.
    assert ps._projected_entry_cost(
        _buy(allocation_pct=2.0), 100_000.0, budget_is_gross=False,
    ) == pytest.approx(2_000.0)


def test_the_other_exits_are_measured_not_projected(tmp_path, monkeypatch):
    """The rotation's close goes LAST, so by the time it is gated the other
    exits have already run and the account has been re-read. Their effect
    is whatever the broker now reports — nothing about them is assumed.

    Here the refresh reports OTHEREXIT already gone. The funding gate sees
    the book the broker actually has, plus the rotation's own close, and
    counts only the rotation's own proceeds as new cash."""
    seen = {}

    def _budget(pipeline, ctx, positions, equity, cash):
        seen["symbols"] = {p.symbol for p in positions}
        seen["cash"] = cash
        return 1_000_000.0, True, "stubbed"

    _stub_sizing(monkeypatch)
    monkeypatch.setattr(ps, "_entry_deployment_budget", _budget)
    stale = [_pos("OLD"), _pos("OTHEREXIT"), _pos("KEEP")]
    # What the broker reports once the earlier exit has gone through.
    after_other_exit = [_pos("OLD"), _pos("KEEP")]
    pipeline, _db = _pipeline(
        tmp_path, positions=after_other_exit, cash=59_950.0, vol_by_book={
            frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
            frozenset({"KEEP"}): SIGMA_FOR_2PCT,
        },
    )
    ctx = _ctx(stale)
    (cleared, *_rest), ordered = _gate(
        pipeline, ctx, stale, sells=[_sell("OTHEREXIT"), _sell("OLD")],
    )
    assert [d.symbol for d in ordered] == ["OTHEREXIT", "OLD"], (
        "the rotation's close must be ordered last"
    )
    assert cleared is True
    assert seen["symbols"] == {"KEEP"}, (
        "the measured book, minus only the rotation's own close"
    )
    # The earlier exit's proceeds are already in the refreshed cash; only
    # the rotation's own are added on top of it.
    assert seen["cash"] == pytest.approx(59_950.0 + 99.50 * 10)


def test_an_unfilled_earlier_exit_is_seen_because_it_is_measured(
    tmp_path, monkeypatch,
):
    """The failure a projection cannot bound: an earlier exit carrying an
    intraday LOSS does not fill, so its loss is still in the book when the
    daily-loss gate runs.

    A gate that assumed every planned exit fills would drop that loss and
    clear the sale. This one re-reads the account and finds the position
    still there, because the rotation's close is ordered last."""
    _stub_sizing(monkeypatch)
    stale = [
        _pos("OLD", intraday=0.0),
        _pos("STUCK", intraday=-2400.0),
        _pos("KEEP", intraday=0.0),
    ]
    # The broker still reports STUCK: its exit did not fill.
    pipeline, db = _pipeline(tmp_path, positions=stale, vol_by_book={
        frozenset({"OLD", "STUCK", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"STUCK", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(stale)
    (cleared, *_rest), _ordered = _gate(
        pipeline, ctx, stale, sells=[_sell("STUCK"), _sell("OLD")],
    )
    assert cleared is False
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "buy_leg_would_be_refused:daily_loss_recheck"


def test_the_replacement_buy_dies_with_the_withdrawn_close(tmp_path, monkeypatch):
    """A withdrawal must not leave the BUY behind. The constructor sized it
    on the premise that the close frees room; buying anyway would put the
    book over the risk ceiling. `ctx.rotation` is therefore MARKED
    withdrawn rather than cleared, which is the state
    `_drop_rotation_buy_if_room_not_freed` already reads as no room."""
    _stub_sizing(monkeypatch, qty=0.0)
    positions = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    buy, other = _buy(), _buy("UNRELATED")
    (cleared, *_rest), _ordered = _gate(
        pipeline, ctx, positions, buys=[other, buy],
    )
    assert cleared is False
    assert isinstance(ctx.rotation, dict), (
        "clearing it would make the buy-side guard a no-op"
    )
    kept = ps._drop_rotation_buy_if_room_not_freed(
        pipeline, ctx, [other, buy], {},
    )
    assert [d.symbol for d in kept] == ["UNRELATED"]
    assert "rotation_room_not_freed" in {s["reason"] for s in ctx.execution_skips}


def test_the_refreshed_book_is_handed_back_to_the_sell_loop(
    tmp_path, monkeypatch,
):
    """Clearing the gate against a fresh book while the SELL loop sizes off
    a stale one would reintroduce, one call later, the divergence
    `_projected_sale_qty` exists to prevent."""
    _stub_sizing(monkeypatch)
    stale = [_pos("OLD", qty=10.0), _pos("KEEP")]
    fresh = [_pos("OLD", qty=4.0), _pos("KEEP")]
    pipeline, _db = _pipeline(tmp_path, positions=fresh, vol_by_book={
        frozenset({"OLD", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    pipeline.stub_total_value = 99_000.0
    pipeline.stub_cash = 44_000.0
    ctx = _ctx(stale)
    (cleared, positions, total_value, cash), _ordered = _gate(
        pipeline, ctx, stale,
    )
    assert cleared is True
    assert [p.qty for p in positions] == [4.0, 10.0]
    assert total_value == 99_000.0 and cash == 44_000.0


def test_the_sell_loop_still_consults_the_wire_barrier():
    """A mechanical guard, not a promise. The barrier that stops a
    ranked-margin close reaching the broker is one `continue` in a long
    loop; if a future edit removes it, `rotation_sell_reason`'s raise is
    never reached and the object guard becomes decorative."""
    source = (REPO_ROOT / "src" / "pipeline_stages.py").read_text()
    assert "rotation_final_reason = _rotation_ranked_margin_sell_reason(" in source
    assert "if rotation_final_reason is _ROTATION_SELL_REFUSED:" in source
    barrier = source.index("if rotation_final_reason is _ROTATION_SELL_REFUSED:")
    assert "continue" in source[barrier:barrier + 200]


# ---------------------------------------------------------------------------
# Fourth adversary pass — the COVER loop runs after the SELL loop
# ---------------------------------------------------------------------------

def _cover(symbol, allocation_pct=100.0):
    return SimpleNamespace(
        symbol=symbol, action="COVER", allocation_pct=allocation_pct,
        entry_price=0.0, stop_loss=0.0, reasoning="cover",
    )


def test_any_pending_cover_refuses_the_rotation_outright(tmp_path, monkeypatch):
    """The COVER loop runs AFTER the SELL loop, so at gate time no cover has
    happened. It also cannot be bounded: the daily-loss NUMERATOR's worst
    case keeps a pending exit that is down, the volatility-relative
    THRESHOLD's worst case drops it (sigma is a property of price history,
    not of today's P&L sign, and the limit is strictly increasing in
    sigma), and GROSS exposure's worst case keeps every one of them. Three
    consumers, three opposite selections, no single book. So the desk
    refuses rather than guesses."""
    _stub_sizing(monkeypatch)
    positions = [
        _pos("OLD", intraday=0.0),
        _pos("SHRT", qty=-10.0, intraday=4000.0),
        _pos("KEEP", intraday=0.0),
    ]
    pipeline, db = _pipeline(tmp_path, positions=positions, vol_by_book={
        frozenset({"OLD", "SHRT", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"SHRT", "KEEP"}): SIGMA_FOR_2PCT,
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(positions)
    cleared, *_rest = ps._rotation_sell_gate(
        pipeline, ctx, _sell(), [_buy()], positions, 100_000.0, 50_000.0,
        [_cover("SHRT")],
    )
    assert cleared is False
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "cover_pending"
    assert "SHRT" in withdrawn["detail"]
    # Refused before the account is even re-read: nothing to measure.
    assert pipeline.refreshed == []

    # A COVER the COVER loop would itself refuse does NOT block it: it
    # takes nothing off the book, so there is nothing to bound.
    ctx = _ctx(positions)
    cleared, *_rest = ps._rotation_sell_gate(
        pipeline, ctx, _sell(), [_buy()], positions, 100_000.0, 50_000.0,
        [_cover("KEEP")],  # KEEP is a LONG; a COVER against it is a no-op
    )
    assert cleared is True

    # And with no cover pending the same book clears.
    ctx = _ctx(positions)
    cleared, *_rest = ps._rotation_sell_gate(
        pipeline, ctx, _sell(), [_buy()], positions, 100_000.0, 50_000.0, [],
    )
    assert cleared is True


def test_only_a_cover_that_can_move_the_book_counts_as_pending():
    """A COVER the COVER loop will itself refuse takes zero shares off the
    book, changes no intraday P&L and moves no gross — so there is nothing
    about it to bound, and counting it would refuse the rotation for a
    cause with no effect, every session the PM keeps proposing it."""
    short = _pos("SHRT", qty=-10.0)
    long_ = _pos("LONGNAME", qty=10.0)
    positions = [short, long_]
    # A real cover of a real short counts, once, in order.
    assert ps._pending_cover_symbols(
        [_cover("shrt"), _cover("SHRT")], positions,
    ) == ("SHRT",)
    # A COVER against a LONG: the COVER loop refuses it outright.
    assert ps._pending_cover_symbols([_cover("LONGNAME")], positions) == ()
    # A COVER against nothing held at all.
    assert ps._pending_cover_symbols([_cover("ABSENT")], positions) == ()
    # `allocation_pct == 0` is the loop's own ambiguity skip.
    assert ps._pending_cover_symbols(
        [_cover("SHRT", allocation_pct=0.0)], positions,
    ) == ()
    assert ps._pending_cover_symbols([], positions) == ()
    assert ps._pending_cover_symbols(None, positions) == ()


def test_the_replacement_is_never_a_name_the_desk_already_holds():
    """Why two more BUY-loop refusals cannot reach a rotation's replacement
    leg. `short_add_blocked` fires on adding to an existing short and the
    scale-in cancel-confirm on adding to an existing long; both require the
    BUY's symbol to be held. `evaluate_rotation_opportunity` picks the
    replacement from `[c for c in ranked if c.symbol not in held]`, so it
    never is. This pins the premise, so that if the candidate source ever
    changes the omission is caught rather than inherited."""
    from src.rotation import evaluate_rotation_opportunity
    from src.verdicts import RankedCandidate

    ranked = [
        RankedCandidate(symbol="HELD", direction="bullish", score=9.0),
        RankedCandidate(symbol="FRESH", direction="bullish", score=4.0),
    ]
    opportunity = evaluate_rotation_opportunity(
        ranked=ranked, blocked={"HELD": ["R5 no rung"]},
        held_symbols={"HELD"}, headroom_pct=0.2, floor_pct=0.5,
    )
    assert opportunity is not None
    assert opportunity.new_symbol == "FRESH", (
        "the best-ranked name overall is HELD, and it is excluded because "
        "it is held — the replacement is always a name the desk does not "
        "already have"
    )


def test_a_position_the_broker_no_longer_holds_is_withdrawn_with_a_reason(
    tmp_path, monkeypatch,
):
    """A stop filled between research and execution. The SELL loop would
    drop this silently; a candidate must not leave the pipeline without a
    durable, per-symbol reason."""
    _stub_sizing(monkeypatch)
    stale = [_pos("OLD"), _pos("KEEP")]
    pipeline, db = _pipeline(tmp_path, positions=[_pos("KEEP")], vol_by_book={
        frozenset({"KEEP"}): SIGMA_FOR_2PCT,
    })
    ctx = _ctx(stale)
    (cleared, *_rest), _ordered = _gate(pipeline, ctx, stale)
    assert cleared is False
    withdrawn, = [e for e in _rotation_events(db) if e["outcome"] == "withdrawn"]
    assert withdrawn["reason"] == "held_position_gone"
    assert "rotation_withdrawn" in {s["reason"] for s in ctx.execution_skips}
