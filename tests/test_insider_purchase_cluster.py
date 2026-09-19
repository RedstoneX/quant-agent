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

def _provider_row(*, symbol, owner, value, accession):
    disclosed = date.today()
    return SmartMoneyObservation(
        symbol=symbol, stream="insider", actor=f"Owner {owner}",
        actor_cik=owner, actor_roles=["director"], direction="buy",
        transaction_date=disclosed - timedelta(days=2),
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
