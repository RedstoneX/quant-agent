"""Correlation-aware risk budgeting — spec §2.2.

The property under test throughout: "total risk is under 25%" is not on its own
a statement about diversification. A book can satisfy it while holding one
theme four times over, which is the concentration the ceiling exists to
prevent. The cluster cap is what makes the total ceiling mean something.
"""

from src.risk.budget import RiskRequest, allocate_risk_budget

NUCLEAR = ["OKLO", "CEG", "VST", "CCJ"]
SEMIS = ["NVDA", "AMD", "AVGO"]


def _req(**pcts):
    return [RiskRequest(sym, pct) for sym, pct in pcts.items()]


# --------------------------------------------------------------------------
# The uncontended case
# --------------------------------------------------------------------------

def test_requests_within_every_ceiling_are_served_in_full():
    alloc = allocate_risk_budget(_req(AAPL=3.0, XOM=2.0))
    assert alloc.granted("AAPL") == 3.0
    assert alloc.granted("XOM") == 2.0
    assert all(g.limited_by is None for g in alloc.grants.values())
    assert alloc.committed_pct == 5.0
    assert alloc.headroom_pct == 20.0


def test_a_symbol_correlated_with_nothing_is_bounded_only_by_the_total():
    """Singleton clusters are omitted upstream; an uncorrelated name must not
    be rationed as though it were a one-member theme."""
    alloc = allocate_risk_budget(
        _req(XOM=9.0), clusters=[NUCLEAR], cluster_share_pct=40.0,
    )
    # 9% exceeds the 10% cluster cap? No — XOM is in no cluster, so only the
    # 25% total applies and the full request stands.
    assert alloc.granted("XOM") == 9.0
    assert alloc.grants["XOM"].limited_by is None


# --------------------------------------------------------------------------
# The cluster cap — the point of the exercise
# --------------------------------------------------------------------------

def test_one_theme_cannot_consume_the_whole_book_under_the_total_ceiling():
    """Four nuclear names at 5% risk each is 20% total — comfortably under the
    25% ceiling, and one 20% bet on a single theme. The cluster cap (40% of
    25% = 10% of equity) must cut it to one bet's worth."""
    alloc = allocate_risk_budget(
        _req(OKLO=5.0, CEG=5.0, VST=5.0, CCJ=5.0),
        clusters=[NUCLEAR], ceiling_pct=25.0, cluster_share_pct=40.0,
    )
    cluster_total = sum(alloc.granted(s) for s in NUCLEAR)
    assert cluster_total == 10.0
    assert alloc.committed_pct == 10.0
    # Served largest-first with an alphabetical tie-break: all four requested
    # 5.0, so CCJ and CEG (alphabetically first) take the budget and the rest
    # fall under the floor and are denied outright.
    assert alloc.granted("CCJ") == 5.0
    assert alloc.granted("CEG") == 5.0
    assert alloc.grants["OKLO"].denied
    assert alloc.grants["VST"].denied


def test_two_uncorrelated_themes_each_get_their_own_cluster_budget():
    """The cap is per cluster, not global — genuine diversification is
    rewarded, which is the other half of §2.2."""
    alloc = allocate_risk_budget(
        _req(OKLO=6.0, CEG=6.0, NVDA=6.0, AMD=6.0),
        clusters=[NUCLEAR, SEMIS], ceiling_pct=25.0, cluster_share_pct=40.0,
    )
    assert sum(alloc.granted(s) for s in NUCLEAR) == 10.0
    assert sum(alloc.granted(s) for s in SEMIS) == 10.0
    assert alloc.committed_pct == 20.0  # under the 25% total, both themes full


def test_cluster_cap_note_explains_the_cut_as_arithmetic():
    """The note is carried into the order's reasoning. On 2026-08-20 an
    unexplained constructor cap read to the AI Risk Manager as the PM
    contradicting itself and drew a full-plan veto."""
    alloc = allocate_risk_budget(
        _req(OKLO=8.0, CEG=8.0),
        clusters=[NUCLEAR], ceiling_pct=25.0, cluster_share_pct=40.0,
    )
    # CEG wins the alphabetical tie-break and takes 8 of the 10% cluster cap;
    # OKLO is cut to the remaining 2%, which clears the 0.5% floor.
    assert alloc.granted("CEG") == 8.0
    assert alloc.granted("OKLO") == 2.0
    note = alloc.grants["OKLO"].note
    assert "cut from 8.00% to 2.00%" in note
    assert "cluster CCJ/CEG/OKLO/VST capped at 10.00%" in note
    assert "one bet" in note and "not PM inconsistency" in note
    assert alloc.grants["OKLO"].cluster == tuple(sorted(NUCLEAR))


def test_a_denial_names_the_ceiling_that_produced_it():
    """A denied request produces no order at all. The operator has to be able
    to tell "the PM never asked" from "the budget refused it"."""
    alloc = allocate_risk_budget(
        _req(OKLO=5.0, CEG=5.0, VST=5.0),
        clusters=[NUCLEAR], ceiling_pct=25.0, cluster_share_pct=40.0,
    )
    assert alloc.grants["VST"].denied
    assert alloc.grants["VST"].limited_by == "below_floor"
    assert "cluster cap leaves 0.00%" in alloc.grants["VST"].note
    assert alloc.grants["VST"].cluster == tuple(sorted(NUCLEAR))


# --------------------------------------------------------------------------
# The total ceiling
# --------------------------------------------------------------------------

def test_total_ceiling_rations_the_remainder_to_the_next_request():
    alloc = allocate_risk_budget(
        _req(AAPL=20.0, XOM=8.0), ceiling_pct=25.0,
    )
    assert alloc.granted("AAPL") == 20.0
    assert alloc.granted("XOM") == 5.0  # 25 - 20
    assert alloc.grants["XOM"].limited_by == "total_ceiling"
    assert alloc.committed_pct == 25.0
    assert alloc.headroom_pct == 0.0


def test_largest_request_is_served_first_regardless_of_listing_order():
    """Rationing must not depend on the order the PM happened to list its
    targets, or the same decision produces different books run to run."""
    forward = allocate_risk_budget(_req(SMALL=2.0, BIG=24.0), ceiling_pct=25.0)
    backward = allocate_risk_budget(_req(BIG=24.0, SMALL=2.0), ceiling_pct=25.0)
    assert forward.grants["BIG"].granted_pct == backward.grants["BIG"].granted_pct == 24.0
    assert forward.grants["SMALL"].granted_pct == backward.grants["SMALL"].granted_pct


def test_request_cut_below_the_floor_is_denied_not_shrunk_to_a_token():
    """A 0.1%-risk position pays full commission and full attention for an
    immaterial payoff."""
    alloc = allocate_risk_budget(
        _req(AAPL=24.8, XOM=5.0), ceiling_pct=25.0, floor_pct=0.5,
    )
    assert alloc.granted("AAPL") == 24.8
    assert alloc.grants["XOM"].denied           # only 0.2% left, under the floor
    assert alloc.grants["XOM"].limited_by == "below_floor"
    assert "under the 0.50% minimum" in alloc.grants["XOM"].note


# --------------------------------------------------------------------------
# Held positions
# --------------------------------------------------------------------------

def test_held_positions_consume_budget_even_when_this_session_ignores_them():
    """The way to release budget is a stop reaching entry or a sale — never
    the allocator forgetting an open position is there."""
    alloc = allocate_risk_budget(
        _req(NEW=10.0), existing_pct={"HELD": 20.0}, ceiling_pct=25.0,
    )
    assert alloc.granted("NEW") == 5.0
    assert alloc.grants["NEW"].limited_by == "total_ceiling"
    assert alloc.committed_pct == 25.0


def test_resizing_a_held_name_replaces_its_risk_rather_than_adding_to_it():
    """Otherwise holding a name would make adding to it cost double, and a
    trim would be charged as though it were a new bet."""
    alloc = allocate_risk_budget(
        _req(HELD=4.0), existing_pct={"HELD": 3.0}, ceiling_pct=25.0,
    )
    assert alloc.granted("HELD") == 4.0
    assert alloc.committed_pct == 4.0  # not 7.0


def test_a_held_position_inside_a_cluster_crowds_out_new_names_in_that_theme():
    alloc = allocate_risk_budget(
        _req(OKLO=6.0), existing_pct={"CEG": 8.0},
        clusters=[NUCLEAR], ceiling_pct=25.0, cluster_share_pct=40.0,
    )
    assert alloc.granted("OKLO") == 2.0  # 10% cluster cap less CEG's 8%
    assert alloc.grants["OKLO"].limited_by == "cluster_cap"


def test_trimming_is_never_blocked_by_a_full_budget():
    """Reducing risk must not require budget — a book over its ceiling would
    otherwise be unable to de-risk."""
    alloc = allocate_risk_budget(
        _req(HELD=1.0), existing_pct={"HELD": 30.0, "OTHER": 20.0},
        ceiling_pct=25.0,
    )
    assert alloc.granted("HELD") == 1.0
    assert alloc.grants["HELD"].limited_by is None


def test_closing_a_name_is_a_zero_grant_not_a_denial():
    alloc = allocate_risk_budget(_req(GONE=0.0), existing_pct={"GONE": 5.0})
    assert alloc.granted("GONE") == 0.0
    assert alloc.grants["GONE"].denied is False
    assert alloc.grants["GONE"].limited_by is None


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------

def test_nan_and_negative_inputs_do_not_mint_budget():
    """Broker snapshots carry NaN and the PM is an LLM. Neither may produce a
    negative risk figure that credits the budget."""
    alloc = allocate_risk_budget(
        [RiskRequest("A", float("nan")), RiskRequest("B", -5.0),
         RiskRequest("C", float("inf"))],
        existing_pct={"HELD": float("nan"), "NEG": -10.0},
        ceiling_pct=25.0,
    )
    assert alloc.committed_pct == 0.0
    assert all(g.granted_pct == 0.0 for g in alloc.grants.values())


def test_a_duplicated_symbol_takes_the_last_request_not_the_sum():
    """Summing would silently double the size of a malformed PM decision."""
    alloc = allocate_risk_budget(
        [RiskRequest("AAPL", 3.0), RiskRequest("AAPL", 2.0)],
    )
    assert alloc.granted("AAPL") == 2.0


def test_symbols_are_matched_case_insensitively():
    alloc = allocate_risk_budget(
        [RiskRequest("oklo", 6.0)], existing_pct={"ceg": 8.0},
        clusters=[["oklo", "ceg"]], ceiling_pct=25.0, cluster_share_pct=40.0,
    )
    assert alloc.granted("OKLO") == 2.0


def test_zero_ceiling_denies_everything_without_raising():
    alloc = allocate_risk_budget(_req(AAPL=3.0), ceiling_pct=0.0)
    assert alloc.grants["AAPL"].denied
    assert alloc.headroom_pct == 0.0


def test_no_clusters_supplied_falls_back_to_the_total_ceiling_alone():
    """Correlation data can be missing (a cold universe, a data outage). The
    allocator must still bound the book rather than failing open."""
    alloc = allocate_risk_budget(
        _req(OKLO=20.0, CEG=20.0), clusters=None, ceiling_pct=25.0,
    )
    assert alloc.committed_pct == 25.0
