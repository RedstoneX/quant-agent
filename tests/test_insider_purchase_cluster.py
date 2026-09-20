"""Board item 124 — the research-defined insider purchase cluster.

A cluster is two or more DISTINCT insiders making OPPORTUNISTIC open-market
purchases (Form 4 code P) in the same symbol on the SAME transaction date
(Alldredge & Blank 2019, abstract: a purchase "on the same day as another
insider purchase at the same company"; Cohen, Malloy & Pomorski: routine
trades carry essentially zero abnormal return). Computed in code, recorded
per symbol as a fact, configured universe only. Its one effect: a bullish
smart-money verdict at "medium" conviction is lifted to "high".
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.data.smart_money import SECForm4Provider
from src.data.smart_money_cluster import insider_purchase_clusters
from src.models import (
    InsiderPurchaseCluster, SmartMoneyFinding, SmartMoneyObservation,
)

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 9, 19)
DAY = date(2026, 9, 10)
UNIVERSE = {"NVDA"}


def _buy(
    *, owner="1", symbol="NVDA", day=DAY, code="P", direction="buy",
    signal_class="opportunistic", value=150_000.0, amendment=False,
    accession=None, disclosed=None,
):
    disclosed = disclosed or day + timedelta(days=2)
    return SmartMoneyObservation(
        symbol=symbol, stream="insider", actor=f"Owner {owner}",
        actor_cik=owner, direction=direction, transaction_date=day,
        disclosure_date=disclosed,
        source_url="https://www.sec.gov/x.txt",
        accession_number=accession or f"000000000{owner}-26-{day:%m%d}",
        filing_form="4/A" if amendment else "4", amendment=amendment,
        transaction_code=code, transaction_row=0, shares=value / 100,
        price_per_share=100, transaction_value_usd=value,
        lag_days=2, disclosure_age_days=0, freshness="fresh",
        economic_role="confirmatory", signal_class=signal_class,
    )


def _clusters(rows, universe=UNIVERSE):
    return insider_purchase_clusters(rows, universe=universe, today=TODAY)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def test_two_distinct_insiders_same_day_is_a_cluster_with_its_facts():
    got = _clusters([
        _buy(owner="1", value=100_000),
        _buy(owner="2", value=250_000, disclosed=DAY + timedelta(days=3)),
    ])
    cluster = got["NVDA"]
    assert cluster.transaction_date == DAY
    assert cluster.distinct_insiders == 2
    assert cluster.insider_ciks == ["1", "2"]
    assert cluster.combined_value_usd == 350_000
    assert cluster.latest_disclosure_date == DAY + timedelta(days=3)
    assert cluster.filing_age_days == (TODAY - (DAY + timedelta(days=3))).days


def test_next_day_purchases_are_not_a_cluster():
    assert _clusters([
        _buy(owner="1", day=DAY),
        _buy(owner="2", day=DAY + timedelta(days=1)),
    ]) == {}


def test_a_routine_purchase_does_not_count_toward_a_cluster():
    assert _clusters([
        _buy(owner="1"),
        _buy(owner="2", signal_class="routine"),
    ]) == {}


def test_an_indeterminate_purchase_does_not_count_toward_a_cluster():
    assert _clusters([
        _buy(owner="1"),
        _buy(owner="2", signal_class="indeterminate"),
    ]) == {}


def test_sales_are_not_a_cluster_and_do_not_complete_one():
    assert _clusters([
        _buy(owner="1", code="S", direction="sell"),
        _buy(owner="2", code="S", direction="sell"),
    ]) == {}
    assert _clusters([
        _buy(owner="1"),
        _buy(owner="2", code="S", direction="sell"),
    ]) == {}


def test_one_insider_buying_twice_the_same_day_is_not_a_cluster():
    assert _clusters([
        _buy(owner="1", accession="0000000001-26-000001"),
        _buy(owner="1", accession="0000000001-26-000002"),
    ]) == {}


def test_an_amendment_is_not_a_second_purchase():
    assert _clusters([
        _buy(owner="1"),
        _buy(owner="2", amendment=True),
    ]) == {}


def test_a_symbol_outside_the_universe_never_gets_a_cluster():
    rows = [_buy(owner="1", symbol="XYZ"), _buy(owner="2", symbol="XYZ")]
    assert _clusters(rows) == {}
    assert "XYZ" in _clusters(rows, universe={"XYZ"})


def test_the_most_recent_cluster_is_the_one_recorded():
    later = DAY + timedelta(days=5)
    got = _clusters([
        _buy(owner="1"), _buy(owner="2"),
        _buy(owner="3", day=later), _buy(owner="4", day=later),
        _buy(owner="5", day=later),
    ])
    assert got["NVDA"].transaction_date == later
    assert got["NVDA"].distinct_insiders == 3


# --------------------------------------------------------------------------
# provider: stamped on universe rows only, no effect on admission or order
# --------------------------------------------------------------------------

def _provider_row(
    *, symbol, owner, value, accession,
    transaction_date=None, disclosure_date=None,
):
    disclosed = disclosure_date or date.today()
    txn = transaction_date if transaction_date is not None else disclosed - timedelta(days=2)
    return SmartMoneyObservation(
        symbol=symbol, stream="insider", actor=f"Owner {owner}",
        actor_cik=owner, actor_roles=["director"], direction="buy",
        transaction_date=txn,
        disclosure_date=disclosed,
        accepted_at=datetime.combine(disclosed, datetime.min.time(), tzinfo=ET),
        source_url=f"https://www.sec.gov/{accession}.txt",
        accession_number=accession, filing_form="4", transaction_code="P",
        transaction_row=0, security_title="Common Stock", shares=value / 100,
        price_per_share=100, transaction_value_usd=value,
        post_transaction_shares=10_000, ownership_nature="direct",
        listed_exchange="Nasdaq", lag_days=2, disclosure_age_days=0,
        freshness="fresh", economic_role="confirmatory",
    )


def test_fetch_stamps_the_cluster_on_universe_rows_and_leaves_others_alone(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = [
        _provider_row(symbol="NVDA", owner="1", value=150_000, accession="0000000001-26-000001"),
        _provider_row(symbol="NVDA", owner="2", value=150_000, accession="0000000002-26-000001"),
        _provider_row(symbol="XYZ", owner="3", value=300_000, accession="0000000003-26-000001"),
        _provider_row(symbol="XYZ", owner="4", value=300_000, accession="0000000004-26-000001"),
    ]
    provider.observations_path.write_text(json.dumps(
        [row.model_dump(mode="json") for row in rows]
    ))
    got, error = provider.fetch(["NVDA"])
    assert error is None
    nvda = [row for row in got if row.symbol == "NVDA"]
    xyz = [row for row in got if row.symbol == "XYZ"]
    assert nvda and all(row.purchase_cluster is not None for row in nvda)
    assert nvda[0].purchase_cluster.distinct_insiders == 2
    assert xyz and all(row.purchase_cluster is None for row in xyz)
    # Admission is untouched: the non-universe name is admitted exactly as it
    # would be on its own size, and a universe name is never "admitted".
    assert all(row.transient_admission_eligible for row in xyz)
    assert not any(row.admission_eligible for row in nvda)


def test_a_cluster_does_not_change_the_seats_symbol_ranking():
    plain = [_buy(owner="1"), _buy(owner="2")]
    stamped_cluster = _clusters(plain)["NVDA"]
    stamped = [row.model_copy(update={"purchase_cluster": stamped_cluster}) for row in plain]
    assert (
        SmartMoneyAnalystAgent._symbol_rank("NVDA", plain)
        == SmartMoneyAnalystAgent._symbol_rank("NVDA", stamped)
    )


def test_the_seat_is_handed_the_cluster_fact_and_null_without_one():
    plain = [_buy(owner="1"), _buy(owner="2")]
    cluster = _clusters(plain)["NVDA"]
    stamped = [row.model_copy(update={"purchase_cluster": cluster}) for row in plain]
    fact = SmartMoneyAnalystAgent._compact_symbol("NVDA", stamped)["insider_purchase_cluster"]
    assert fact == {
        "transaction_date": DAY.isoformat(),
        "distinct_insiders": 2,
        "combined_value_usd": 300_000,
        "filing_age_days": cluster.filing_age_days,
    }
    assert SmartMoneyAnalystAgent._compact_symbol("NVDA", plain)["insider_purchase_cluster"] is None


def test_evidence_hash_tracks_the_cluster_but_not_its_age():
    plain = [_buy(owner="1"), _buy(owner="2")]
    cluster = _clusters(plain)["NVDA"]
    stamped = [row.model_copy(update={"purchase_cluster": cluster}) for row in plain]
    older = cluster.model_copy(update={"filing_age_days": cluster.filing_age_days + 1})
    aged = [row.model_copy(update={"purchase_cluster": older}) for row in plain]
    h = SmartMoneyAnalystAgent._evidence_hash
    assert h(plain) != h(stamped)
    assert h(stamped) == h(aged)


# --------------------------------------------------------------------------
# the conviction lift and its limit
# --------------------------------------------------------------------------

_CLUSTER = InsiderPurchaseCluster(
    transaction_date=DAY, distinct_insiders=2, insider_ciks=["1", "2"],
    combined_value_usd=300_000, latest_disclosure_date=DAY,
    filing_age_days=9,
)


def _finding(*, role="confirmatory", stance="bullish", cluster=_CLUSTER, stream="insider"):
    row = _buy(owner="1").model_copy(update={
        "purchase_cluster": cluster, "stream": stream,
    })
    if stance == "bearish":
        row = row.model_copy(update={"direction": "sell", "transaction_code": "S"})
    return SmartMoneyFinding(
        symbol="NVDA", stance=stance, economic_role=role,
        summary="insiders bought", why_now="fresh Form 4 purchases",
        observations=[row],
    )


def test_a_cluster_lifts_medium_to_high():
    assert _finding(cluster=None).to_verdict().conviction == "medium"
    assert _finding().to_verdict().conviction == "high"


def test_the_lift_is_recorded_as_evidence():
    evidence = [e for e in _finding().to_verdict().evidence
                if e.label == "insider_purchase_cluster"]
    assert len(evidence) == 1
    assert evidence[0].value == 300_000
    assert evidence[0].as_of == DAY
    assert "2 distinct insiders" in evidence[0].text


def test_the_lift_never_raises_low_and_high_stays_high():
    assert _finding(role="contradictory").to_verdict().conviction == "low"
    assert _finding(role="actionable").to_verdict().conviction == "high"


def test_the_lift_does_not_apply_to_a_bearish_or_neutral_read():
    assert _finding(stance="bearish").to_verdict().conviction == "medium"
    assert _finding(stance="neutral").to_verdict().conviction == "medium"


# --------------------------------------------------------------------------
# item 124's second DONE-WHEN criterion, re-derived rather than asserted.
#
# No real production Form 4 cache at the scale of the 2026-09-19 measurement
# (15,068 insider rows, one year) was reachable from this checkout — only a
# 5-row local dev snapshot exists on disk, far too small to exercise
# ``max_observations`` (default 40) at all. What follows instead runs the
# REAL ``SECForm4Provider.fetch()`` pipeline (not a reimplementation of its
# sort) over synthetic rows sized to match the production shape the
# measurement above described: a set of individually-material opportunistic
# buys plus a smaller cluster whose members are each below the per-symbol
# materiality threshold on their own. This is evidence about the code's
# actual behaviour, not a fixture of real filings, and is reported as such.
# --------------------------------------------------------------------------

def test_a_cluster_rescued_by_the_retention_rule_can_still_be_truncated_before_the_seat_sees_it(tmp_path):
    """Board item 124's DONE-WHEN #2 is NOT met: a cluster's own rows are not
    protected in ``SECForm4Provider.fetch``'s final sort, so when enough
    individually-material opportunistic buys compete for the same
    ``max_observations`` cap, a below-threshold cluster (rescued into
    ``cluster_survivors`` by the two-owner window rule, and simultaneously a
    same-day opportunistic ``insider_purchase_clusters`` cluster) is the
    first thing cut. The sort key in ``SECForm4Provider.fetch`` is
    ``(not transient_admission_eligible, not in_core_universe,
    -signal_weight, -transaction_value_usd, -accepted_at)`` — nothing in it
    ever looks at ``purchase_cluster``.
    """
    provider = SECForm4Provider(data_dir=str(tmp_path))
    today = date.today()
    rows = []
    # 40 solo buys, each alone above the $100k core threshold, distinct
    # owners and accessions, far enough from the rescue pair's date (more
    # than cluster_window_days=2) to form their own separate window.
    for i in range(40):
        rows.append(_provider_row(
            symbol="CORE", owner=f"solo-{i}", value=500_000,
            accession=f"0000000100-26-{i:06d}",
            transaction_date=today - timedelta(days=10),
            disclosure_date=today,
        ))
    # Two distinct insiders, same day, each below the $100k threshold alone
    # ($60k) but $120k combined -- rescued by cluster_survivors's window
    # rule, and independently a same-day opportunistic purchase cluster
    # (the fact that lifts conviction).
    rows.append(_provider_row(
        symbol="CORE", owner="rescue-1", value=60_000,
        accession="0000000200-26-000001",
        transaction_date=today - timedelta(days=1), disclosure_date=today,
    ))
    rows.append(_provider_row(
        symbol="CORE", owner="rescue-2", value=60_000,
        accession="0000000200-26-000002",
        transaction_date=today - timedelta(days=1), disclosure_date=today,
    ))
    provider.observations_path.write_text(json.dumps(
        [row.model_dump(mode="json") for row in rows]
    ))
    got, error = provider.fetch(["CORE"])
    assert error is None
    assert len(got) == provider.max_observations == 40

    rescued = [row for row in got if row.actor_cik in ("rescue-1", "rescue-2")]
    # This assertion documents the CURRENT, UNFIXED behaviour: the rescue
    # pair is real (both rows would independently show up as a cluster) but
    # is crowded out by 40 higher-dollar solo buys before the seat ever
    # receives it, so the cluster's own rows do NOT survive the truncation.
    # If this assertion starts failing, item 124's DONE-WHEN #2 may finally
    # be met and should be re-examined against real data before closing it.
    assert rescued == [], (
        "cluster-rescued rows unexpectedly survived truncation — re-check "
        "item 124's second DONE-WHEN criterion against this result"
    )

    # The cluster fact itself is real and was computed correctly (confirming
    # the defect is specifically about the SORT, not about cluster
    # detection): had the rescue pair survived, they would have carried it.
    # ``insider_purchase_clusters`` only counts rows already classified
    # opportunistic (`fetch` stamps this; these raw rows have not been
    # through the classifier, so stamp them the same way here).
    stamped = [row.model_copy(update={"signal_class": "opportunistic"}) for row in rows]
    clusters = insider_purchase_clusters(stamped, universe={"CORE"}, today=today)
    assert clusters["CORE"].distinct_insiders == 2
    assert clusters["CORE"].combined_value_usd == 120_000


# --------------------------------------------------------------------------
# board item 124, adversary finding: does the routine/opportunistic
# classifier actually keep an ESPP-style recurring cluster from lifting
# conviction? (docs/INCIDENT_HISTORY.md's 2026-09-19 entry found 9 of 15
# measured cluster-days were TSM's employee stock purchase plan, and noted
# the routine test only catches a participant once enough prior months are
# on record.) These tests run the real classifier and cluster function
# together, rather than asserting `signal_class="routine"` by hand as the
# tests above do.
# --------------------------------------------------------------------------

def test_an_established_recurring_monthly_buyer_is_excluded_from_the_cluster():
    """Two insiders each with four prior monthly purchases of the same small
    dollar amount (an ESPP-shaped pattern) buy again, same day, same symbol.
    `classify_transaction`'s recurring-cadence rule should label both
    ROUTINE off their own trade history, and `insider_purchase_clusters`
    must then find no cluster -- the exclusion this feature depends on to
    avoid an ESPP false positive.
    """
    from src.data.insider_signal import InsiderHistory, InsiderPriorTrade, classify_transaction

    today = date(2026, 9, 19)
    history = InsiderHistory({
        ("espp-1", "TSM"): [
            InsiderPriorTrade(transaction_date=today - timedelta(days=d), direction="buy")
            for d in (120, 90, 60, 30)
        ],
        ("espp-2", "TSM"): [
            InsiderPriorTrade(transaction_date=today - timedelta(days=d), direction="buy")
            for d in (121, 91, 61, 31)
        ],
    })
    newest = [
        _buy(owner="espp-1", symbol="TSM", day=today, value=4_000,
             accession="espp-1-newest"),
        _buy(owner="espp-2", symbol="TSM", day=today, value=4_500,
             accession="espp-2-newest"),
    ]
    verdicts = [classify_transaction(row, history) for row in newest]
    assert [v.label for v in verdicts] == ["routine", "routine"]
    assert all(v.reason == "recurring_cadence" for v in verdicts)

    stamped = [
        row.model_copy(update={"signal_class": v.label})
        for row, v in zip(newest, verdicts)
    ]
    clusters = insider_purchase_clusters(stamped, universe={"TSM"}, today=today)
    assert "TSM" not in clusters


def test_a_brand_new_participants_first_purchase_has_no_history_to_classify_routine():
    """Documents a real, pre-existing, structural limit (not introduced by
    this PR, and already stated in docs/INCIDENT_HISTORY.md): a purchase
    cannot be recognised as part of a recurring pattern before any pattern
    exists. An insider's first-ever recorded purchase, with no prior trades
    in the history index, is classified opportunistic even if it happens to
    be the start of a routine programme -- this is inherent to a
    history-based classifier, not something last-mile-fixable by widening a
    threshold, and is left open on the board rather than fixed here.
    """
    from src.data.insider_signal import InsiderHistory, classify_transaction

    today = date(2026, 9, 19)
    first_ever = _buy(owner="new-participant", symbol="TSM", day=today, value=4_000)
    verdict = classify_transaction(first_ever, InsiderHistory({}))
    assert verdict.label == "opportunistic"


# --------------------------------------------------------------------------
# board item 124, corrected defect axis (2026-09-20): CROSS-symbol
# crowd-out. A previously proposed fix (adjusting the shared row-level sort
# key in `SECForm4Provider.fetch`) was reviewed and found unsafe -- wrong
# signal-weight values, a nonexistent flag, and it would have overridden
# dollar-value ordering entirely instead of narrowly tie-breaking, since
# competing opportunistic rows already tie at the top weight. The fix
# actually shipped instead is `src.data.smart_money._reserve_cluster_symbols`:
# a small, bounded reservation (`MAX_CLUSTER_RESERVED_SLOTS`) that guarantees
# at least one surviving row per genuinely clustered SYMBOL, leaving the main
# dollar-value sort untouched. These tests prove the FACT actually reaches
# the seat for a symbol whose only rows are the cluster's own -- not just
# that some row of that symbol survives.
# --------------------------------------------------------------------------

def test_a_cluster_survives_cross_symbol_crowd_out_by_unrelated_higher_dollar_buys(tmp_path):
    """CLUSTERED has ONLY its two cluster-member rows -- no other row for
    that symbol exists anywhere in the cache. 45 unrelated symbols each
    carry one solo buy at 8x the cluster's combined value, enough alone to
    fill every ``max_observations`` (=40) slot ahead of CLUSTERED under the
    plain dollar-value sort. This is the case the previous within-symbol
    test (``test_a_cluster_rescued_by_the_retention_rule_can_still_be_
    truncated_before_the_seat_sees_it``) does NOT cover: there, CORE's own
    non-cluster rows already occupy the cap, which the reservation
    deliberately does not touch (it only protects a symbol with ZERO
    surviving rows, not a symbol whose OWN rows crowd out its OWN cluster).
    Here every crowding row belongs to a different symbol, which is exactly
    the defect item 124 was re-opened for.
    """
    provider = SECForm4Provider(data_dir=str(tmp_path))
    today = date.today()
    rows = []
    for i in range(45):
        rows.append(_provider_row(
            symbol=f"CROWD{i}", owner=f"solo-{i}", value=1_000_000,
            accession=f"0000000300-26-{i:06d}",
            transaction_date=today - timedelta(days=10),
            disclosure_date=today,
        ))
    # CLUSTERED: two distinct insiders, same day, each below the $100k core
    # threshold alone ($60k) but $120k combined -- rescued into
    # `cluster_survivors` by the two-owner window rule, and independently a
    # same-day opportunistic `insider_purchase_clusters` cluster.
    rows.append(_provider_row(
        symbol="CLUSTERED", owner="rescue-1", value=60_000,
        accession="0000000400-26-000001",
        transaction_date=today - timedelta(days=1), disclosure_date=today,
    ))
    rows.append(_provider_row(
        symbol="CLUSTERED", owner="rescue-2", value=60_000,
        accession="0000000400-26-000002",
        transaction_date=today - timedelta(days=1), disclosure_date=today,
    ))
    provider.observations_path.write_text(json.dumps(
        [row.model_dump(mode="json") for row in rows]
    ))
    symbols = ["CLUSTERED"] + [f"CROWD{i}" for i in range(45)]
    got, error = provider.fetch(symbols)
    assert error is None
    assert len(got) == provider.max_observations == 40

    clustered_rows = [row for row in got if row.symbol == "CLUSTERED"]
    assert clustered_rows, (
        "CLUSTERED was crowded out of max_observations entirely by unrelated "
        "higher-dollar buys on OTHER symbols -- the cross-symbol crowd-out "
        "defect (board item 124) is not fixed"
    )
    # The load-bearing assertion: not just that a CLUSTERED row survived, but
    # that the fact the seat reads (`purchase_cluster`) is actually stamped
    # on it -- this is what `SmartMoneyFinding.purchase_cluster()` reads.
    with_cluster = [row for row in clustered_rows if row.purchase_cluster is not None]
    assert with_cluster, "a CLUSTERED row survived but without its cluster fact stamped"
    cluster = with_cluster[0].purchase_cluster
    assert cluster.distinct_insiders == 2
    assert cluster.combined_value_usd == 120_000
    assert set(cluster.insider_ciks) == {"rescue-1", "rescue-2"}


def test_cross_symbol_reservation_is_bounded_and_does_not_starve_everything():
    """The reservation is capped so a day with many clusters cannot crowd out
    the whole dollar-sorted list; it only ever displaces the lowest-priority
    non-clustered rows, one per reserved symbol.
    """
    from src.data.smart_money_cluster import (
        MAX_CLUSTER_RESERVED_SLOTS, reserve_cluster_symbols as _reserve_cluster_symbols,
    )

    def _row(symbol, value, cik):
        return _buy(owner=cik, symbol=symbol, value=value, day=DAY,
                    accession=f"{cik}-{symbol}")

    # 10 clustered symbols, each with exactly one overflow row, competing
    # against 40 unrelated higher-value rows that fill the whole cap.
    crowd = [_row(f"CROWD{i}", 1_000_000, f"crowd{i}") for i in range(40)]
    clustered = [_row(f"CLUSTER{i}", 10_000, f"cluster{i}") for i in range(10)]
    ordered = crowd + clustered  # already worst-first for the clustered set
    final = _reserve_cluster_symbols(
        ordered,
        {f"CLUSTER{i}" for i in range(10)},
        max_observations=40,
        max_reserved_slots=MAX_CLUSTER_RESERVED_SLOTS,
    )
    reserved_symbols = {row.symbol for row in final if row.symbol.startswith("CLUSTER")}
    assert len(reserved_symbols) == MAX_CLUSTER_RESERVED_SLOTS
    assert len(final) == 40


def test_a_routine_cluster_gets_no_reserved_slot():
    """A routine-classified same-day cluster (e.g. an ESPP-shaped recurring
    buy) must never be granted a reserved slot: `insider_purchase_clusters`
    only records genuinely OPPORTUNISTIC clusters (`_cluster_member` in
    `src.data.smart_money_cluster` requires ``signal_class == "opportunistic"``),
    so a routine same-day pair never appears in ``clusters`` at all and the
    reservation mechanism -- keyed off exactly that dict -- has nothing to
    reserve for it.
    """
    today = date(2026, 9, 19)
    routine_pair = [
        _buy(owner="espp-1", symbol="TSM", day=today, value=4_000,
             signal_class="routine", accession="espp-1-x"),
        _buy(owner="espp-2", symbol="TSM", day=today, value=4_500,
             signal_class="routine", accession="espp-2-x"),
    ]
    clusters = insider_purchase_clusters(routine_pair, universe={"TSM"}, today=today)
    assert "TSM" not in clusters
