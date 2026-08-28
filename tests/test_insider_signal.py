"""Routine/opportunistic Form 4 classification.

Evidence basis is ``docs/RESEARCH_FINDINGS.md`` section 1. The tests below
pin the rules that document actually supports — including the two places it
contradicts the folk version of this filter (10b5-1 is not a noise marker on
its own; sell materiality is proportional, not absolute).
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.data.insider_signal import (
    InsiderHistory,
    InsiderPriorTrade,
    InsiderSignalThresholds,
    classify_observations,
    classify_transaction,
)
from src.data.smart_money import SECForm4Provider
from src.models import SmartMoneyObservation

ET = ZoneInfo("America/New_York")


def _row(
    *,
    symbol="NVDA",
    owner="1",
    direction="buy",
    shares=1_000.0,
    price=100.0,
    post_shares=50_000.0,
    transaction_date=date(2026, 8, 20),
    is_10b5_1=None,
    roles=("officer", "Chief Financial Officer"),
    accession="0000000001-26-000001",
    row=0,
    age=0,
    stream="insider",
    code=None,
):
    disclosed = transaction_date + timedelta(days=2)
    return SmartMoneyObservation(
        symbol=symbol,
        stream=stream,
        actor=f"Owner {owner}",
        actor_cik=owner,
        actor_roles=list(roles),
        direction=direction,
        transaction_date=transaction_date,
        disclosure_date=disclosed,
        accepted_at=datetime.combine(disclosed, datetime.min.time(), tzinfo=ET),
        source_url=f"https://www.sec.gov/{accession}.txt",
        accession_number=accession,
        filing_form="4",
        transaction_code=code if code is not None else ("P" if direction == "buy" else "S"),
        transaction_row=row,
        security_title="Common Stock",
        shares=shares,
        price_per_share=price,
        transaction_value_usd=None if shares is None or price is None else shares * price,
        post_transaction_shares=post_shares,
        ownership_nature="direct",
        listed_exchange="Nasdaq",
        lag_days=2,
        disclosure_age_days=age,
        freshness="fresh",
        economic_role="confirmatory",
        is_10b5_1=is_10b5_1,
    )


def _history(owner="1", symbol="NVDA", direction="buy", days=()):
    return InsiderHistory({
        (owner, symbol): [
            InsiderPriorTrade(transaction_date=day, direction=direction)
            for day in days
        ]
    })


# --- opportunistic ---------------------------------------------------------

def test_discretionary_officer_purchase_is_opportunistic():
    verdict = classify_transaction(_row(), InsiderHistory())

    assert verdict.label == "opportunistic"
    assert verdict.reason == "opportunistic_purchase"
    assert verdict.weight == 1.0
    assert "$100,000" in verdict.detail


def test_purchase_by_owner_with_no_inside_role_is_still_opportunistic_but_flagged():
    verdict = classify_transaction(_row(roles=("other",)), InsiderHistory())

    assert verdict.label == "opportunistic"
    assert "no officer, director or 10% role" in verdict.detail


def test_sale_large_relative_to_the_holding_is_opportunistic():
    verdict = classify_transaction(
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0),
        InsiderHistory(),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "material_stake_sale"
    assert "80.0%" in verdict.detail


# --- routine categories ----------------------------------------------------

def test_same_calendar_month_for_three_years_is_routine():
    """Cohen/Malloy/Pomorski's own definition of a routine trader."""
    verdict = classify_transaction(
        _row(transaction_date=date(2026, 8, 20)),
        _history(days=[date(2025, 8, 14), date(2024, 8, 3), date(2023, 8, 28)]),
    )

    assert verdict.label == "routine"
    assert verdict.reason == "calendar_routine"
    assert verdict.weight == 0.0
    assert "August" in verdict.detail


def test_two_years_of_same_month_history_is_not_yet_routine():
    """Boundary: the rule needs three preceding years, not two."""
    verdict = classify_transaction(
        _row(transaction_date=date(2026, 8, 20)),
        _history(days=[date(2025, 8, 14), date(2024, 8, 3)]),
    )

    assert verdict.label == "opportunistic"


def test_a_gap_year_breaks_the_consecutive_streak():
    """2023 is missing, so the three preceding years are not consecutive."""
    verdict = classify_transaction(
        _row(transaction_date=date(2026, 8, 20)),
        _history(days=[
            date(2025, 8, 14), date(2022, 8, 3), date(2021, 8, 28), date(2020, 8, 9),
        ]),
    )

    assert verdict.label == "opportunistic"


def test_history_in_the_other_direction_does_not_make_a_purchase_routine():
    verdict = classify_transaction(
        _row(direction="buy", transaction_date=date(2026, 8, 20)),
        _history(direction="sell", days=[
            date(2025, 8, 14), date(2024, 8, 3), date(2023, 8, 28),
        ]),
    )

    assert verdict.label == "opportunistic"


def test_evenly_spaced_recurring_programme_is_routine():
    verdict = classify_transaction(
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0,
             transaction_date=date(2026, 8, 20)),
        _history(direction="sell", days=[
            date(2026, 5, 20), date(2026, 2, 20), date(2025, 11, 20),
            date(2025, 8, 20),
        ]),
    )

    assert verdict.label == "routine"
    assert verdict.reason == "recurring_cadence"
    assert "scheduled programme" in verdict.detail


def test_lumpy_discretionary_history_is_not_a_cadence():
    """Boundary: same trade count, irregular spacing, so no routine label."""
    verdict = classify_transaction(
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0,
             transaction_date=date(2026, 8, 20)),
        _history(direction="sell", days=[
            date(2026, 8, 1), date(2026, 7, 28), date(2025, 12, 3), date(2025, 3, 9),
        ]),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "material_stake_sale"


def test_small_proportional_sale_is_routine_noise():
    """Only sales large relative to the insider's own position predict."""
    verdict = classify_transaction(
        _row(direction="sell", shares=1_000.0, post_shares=99_000.0),
        InsiderHistory(),
    )

    assert verdict.label == "routine"
    assert verdict.reason == "immaterial_stake_sale"
    assert "1.0%" in verdict.detail


def test_small_planned_sale_names_the_10b5_1_plan_in_its_reason():
    verdict = classify_transaction(
        _row(direction="sell", shares=1_000.0, post_shares=99_000.0, is_10b5_1=True),
        InsiderHistory(),
    )

    assert verdict.label == "routine"
    assert verdict.reason == "planned_small_disposition"
    assert "10b5-1" in verdict.detail


# --- every SEC Form 4 transaction code, pinned individually --------------
#
# ``SmartMoneyObservation.transaction_code`` is itself typed
# ``Literal["", "P", "S"]`` (`src/models.py`) — a row carrying any other SEC
# code cannot be constructed at all, because ``SECForm4Provider._parse_
# submission`` filters ``transactionCode not in {"P", "S"}`` before a
# ``SmartMoneyObservation`` is ever built (see ``tests/test_smart_money.py``,
# which pins the parser boundary for codes A/M/F/G/D/X — the grant/award,
# option-exercise, tax-withholding, gift and issuer-disposition codes never
# reach this module). What *this* module can be asked to classify is P, S,
# or the empty string a defensive caller might pass; ``non_open_market_code``
# is a contract guard for that last case, not a live filter on real SEC
# codes. Each of the three is pinned below with a hard literal.
def test_code_empty_is_routine_via_the_contract_guard():
    verdict = classify_transaction(_row(code="", direction="buy"), InsiderHistory())
    assert verdict.label == "routine"
    assert verdict.reason == "non_open_market_code"
    assert verdict.weight == 0.0


def test_code_p_open_market_purchase_is_opportunistic():
    """Pinned separately from ``test_discretionary_officer_purchase_is_
    opportunistic`` so every code in the reference list above has its own
    literal-asserted test, P included."""
    verdict = classify_transaction(_row(code="P", direction="buy"), InsiderHistory())
    assert verdict.label == "opportunistic"
    assert verdict.reason == "opportunistic_purchase"
    assert verdict.weight == 1.0


def test_code_s_open_market_sale_is_opportunistic_when_material():
    """Pinned separately from ``test_sale_large_relative_to_the_holding_is_
    opportunistic`` for the same reason — code S gets its own literal test."""
    verdict = classify_transaction(
        _row(code="S", direction="sell", shares=40_000.0, post_shares=10_000.0),
        InsiderHistory(),
    )
    assert verdict.label == "opportunistic"
    assert verdict.reason == "material_stake_sale"
    assert verdict.weight == 1.0


def test_zero_price_transaction_is_routine():
    verdict = classify_transaction(_row(price=0.0), InsiderHistory())

    assert verdict.label == "routine"
    assert verdict.reason == "zero_price_transaction"


# --- the RESEARCH_FINDINGS contradictions ----------------------------------

def test_large_10b5_1_sale_is_not_demoted_for_being_planned():
    """RESEARCH_FINDINGS: 10b5-1 is not a clean noise filter.

    "For high-value sales, planned and discretionary transactions show similar
    opportunism, and the 2022 SEC reform did not reduce abnormal returns on
    insider selling." A large planned sale therefore stays opportunistic, and
    the reason says the flag was seen and deliberately not acted on.
    """
    verdict = classify_transaction(
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0, is_10b5_1=True),
        InsiderHistory(),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "material_stake_sale"
    assert "deliberately not treated as a noise marker" in verdict.detail


def test_10b5_1_purchase_is_not_routine():
    verdict = classify_transaction(_row(is_10b5_1=True), InsiderHistory())

    assert verdict.label == "opportunistic"


# --- boundaries ------------------------------------------------------------

def test_sale_at_exactly_the_materiality_fraction_is_opportunistic():
    verdict = classify_transaction(
        _row(direction="sell", shares=5_000.0, post_shares=95_000.0),
        InsiderHistory(),
    )

    assert verdict.label == "opportunistic"


def test_sale_just_under_the_materiality_fraction_is_routine():
    verdict = classify_transaction(
        _row(direction="sell", shares=4_999.0, post_shares=95_001.0),
        InsiderHistory(),
    )

    assert verdict.label == "routine"


def test_missing_post_transaction_holding_is_indeterminate_not_routine():
    verdict = classify_transaction(
        _row(direction="sell", post_shares=None), InsiderHistory(),
    )

    assert verdict.label == "indeterminate"
    assert verdict.reason == "unknown_holding"
    assert verdict.weight == 0.5


def test_missing_amounts_are_indeterminate():
    verdict = classify_transaction(_row(shares=None, price=None), InsiderHistory())

    assert verdict.label == "indeterminate"
    assert verdict.reason == "incomplete_amounts"


def test_congressional_stream_is_out_of_scope():
    verdict = classify_transaction(
        _row(stream="congressional", code=""), InsiderHistory(),
    )

    assert verdict.label == "indeterminate"
    assert verdict.reason == "not_form4"


def test_history_only_counts_trades_strictly_before_the_transaction():
    """A same-day duplicate row must not be read as its own precedent."""
    same_day = date(2026, 8, 20)
    verdict = classify_transaction(
        _row(transaction_date=same_day),
        _history(days=[same_day, date(2025, 8, 14), date(2024, 8, 3), date(2023, 8, 28)]),
    )

    assert verdict.reason == "calendar_routine"
    assert "3 preceding years" in verdict.detail


def test_classify_observations_defaults_to_self_derived_history():
    rows = classify_observations([
        _row(),
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0, row=1),
        _row(direction="sell", row=2),
    ])

    assert [row.signal_class for row in rows] == [
        "opportunistic", "opportunistic", "routine",
    ]
    assert [row.signal_weight for row in rows] == [1.0, 1.0, 0.0]
    assert all(row.signal_class_reason and row.signal_class_detail for row in rows)


# --- provider wiring -------------------------------------------------------

def _cached(provider, rows):
    provider.observations_path.write_text(
        json.dumps([row.model_dump(mode="json") for row in rows])
    )


def _provider(tmp_path, **kwargs):
    return SECForm4Provider(data_dir=str(tmp_path), **kwargs)


def test_routine_purchase_never_becomes_admission_eligible(tmp_path):
    today = date.today()
    provider = _provider(tmp_path, external_min_transaction_value_usd=10_000)
    routine = _row(
        symbol="ABCD", shares=5_000.0, price=100.0, transaction_date=today,
        is_10b5_1=True,
    )
    _cached(provider, [routine])
    provider.history_path.write_text(json.dumps({
        "1|ABCD": [
            f"{today.replace(year=today.year - offset).isoformat()}|buy"
            for offset in (1, 2, 3)
        ]
    }))

    observations, error = provider.fetch(["NVDA"])

    assert error is None
    assert [row.signal_class for row in observations] == ["routine"]
    assert observations[0].signal_class_reason == "calendar_routine"
    assert observations[0].admission_eligible is False
    assert observations[0].transient_admission_eligible is False
    assert observations[0].economic_role == "confirmatory"


def test_opportunistic_purchase_still_gets_admission(tmp_path):
    provider = _provider(tmp_path, external_min_transaction_value_usd=10_000)
    _cached(provider, [_row(symbol="ABCD", transaction_date=date.today())])

    observations, _ = provider.fetch(["NVDA"])

    assert [row.signal_class for row in observations] == ["opportunistic"]
    assert observations[0].admission_eligible is True


def test_model_validator_refuses_to_mark_a_routine_row_eligible():
    """Belt and braces: the gate holds even if a caller sets the flag by hand."""
    row = _row().model_dump()
    row.update({
        "admission_eligible": True,
        "transient_admission_eligible": True,
        "signal_class": "routine",
        "signal_class_reason": "calendar_routine",
    })

    rebuilt = SmartMoneyObservation(**row)

    assert rebuilt.admission_eligible is False
    assert rebuilt.transient_admission_eligible is False


def test_routine_rows_sort_behind_opportunistic_ones(tmp_path):
    """A large routine sale must not crowd out a smaller real purchase."""
    today = date.today()
    provider = _provider(tmp_path, min_transaction_value_usd=10_000)
    _cached(provider, [
        _row(symbol="NVDA", owner="1", direction="sell", shares=1_000.0,
             price=900.0, post_shares=999_000.0, transaction_date=today,
             accession="0000000001-26-000009"),
        _row(symbol="NVDA", owner="2", direction="buy", shares=200.0,
             price=100.0, transaction_date=today,
             accession="0000000001-26-000010"),
    ])

    observations, _ = provider.fetch(["NVDA"])

    assert [row.signal_class for row in observations] == ["opportunistic", "routine"]
    assert observations[1].transaction_value_usd > observations[0].transaction_value_usd


def test_history_index_survives_the_observation_cache_prune(tmp_path):
    """The lookback prune is exactly why a separate history file exists."""
    provider = _provider(tmp_path)
    old = date.today() - timedelta(days=400)
    provider._record_history([
        {"actor_cik": "1", "symbol": "NVDA", "direction": "buy",
         "transaction_date": old.isoformat()},
        {"actor_cik": "1", "symbol": "NVDA", "direction": "buy",
         "transaction_date": "not-a-date"},
    ])

    history = provider._load_history()

    assert history.prior_trades(
        "1", "NVDA", direction="buy", before=date.today(),
    ) == [InsiderPriorTrade(transaction_date=old, direction="buy")]


def test_history_index_prunes_beyond_retention(tmp_path):
    provider = _provider(tmp_path)
    ancient = date.today() - timedelta(days=6 * 366)
    provider._record_history([
        {"actor_cik": "1", "symbol": "NVDA", "direction": "buy",
         "transaction_date": ancient.isoformat()},
    ])

    assert json.loads(provider.history_path.read_text()) == {}


def test_history_index_is_append_only_across_refreshes(tmp_path):
    provider = _provider(tmp_path)
    for day in (date(2024, 8, 3), date(2025, 8, 14)):
        provider._record_history([{
            "actor_cik": "1", "symbol": "NVDA", "direction": "buy",
            "transaction_date": day.isoformat(),
        }])

    assert json.loads(provider.history_path.read_text()) == {
        "1|NVDA": ["2024-08-03|buy", "2025-08-14|buy"],
    }


# --- fail closed: an unclassifiable filing is kept, never dropped ---------
#
# The owner's rule: a filing the classifier cannot place (missing data, an
# unrecognised stream, ...) comes back ``indeterminate``, never ``routine``
# — losing a real signal is worse than keeping noise. This section checks
# that guarantee survives the full ``SECForm4Provider.fetch`` pipeline, not
# just ``classify_transaction`` in isolation: an indeterminate row must
# still clear materiality/cluster gating and appear in the returned
# observations exactly like an opportunistic one would.

def test_indeterminate_filing_is_kept_by_fetch_not_dropped(tmp_path):
    """A sale with no reported post-transaction holding cannot be sized
    against the insider's position (``unknown_holding``), so the classifier
    returns ``indeterminate`` rather than guessing. Fail-closed means this
    row must still reach the operator — it is materially large enough to
    matter and nothing else about it is invalid."""
    provider = _provider(tmp_path, min_transaction_value_usd=100_000)
    unclassifiable = _row(
        symbol="NVDA", direction="sell", shares=2_000.0, price=100.0,
        post_shares=None, transaction_date=date.today(),
    )
    _cached(provider, [unclassifiable])

    observations, error = provider.fetch(["NVDA"])

    assert error is None
    assert len(observations) == 1  # kept, not dropped
    assert observations[0].signal_class == "indeterminate"
    assert observations[0].signal_class_reason == "unknown_holding"
    assert observations[0].signal_weight == 0.5


def test_indeterminate_filing_from_missing_amounts_is_not_downgraded_to_routine():
    """Same guarantee, different unclassifiable cause: shares/value missing
    entirely (``incomplete_amounts``) rather than just the post-transaction
    holding.

    Note on scope: a row with ``transaction_value_usd is None`` can never
    reach ``SECForm4Provider.fetch()``'s returned observations at all —
    the survivors loop (both the individual-materiality and the cluster
    path) requires ``transaction_value_usd is not None`` before a row is
    even added to the candidate window, so a value-unknown row is dropped
    there regardless of its signal_class. That gate is a materiality filter
    inherited unchanged from `b1944cd` (pre-dates this branch and
    routine/opportunistic classification entirely), not something this
    classifier introduced or can fail-closed around — you cannot assess
    dollar materiality for a transaction of unknown dollar value. It is
    flagged in the PR description as a separate, pre-existing gap rather
    than fixed here. What this test pins is the part that *is* this
    classifier's contract: at the classification layer itself, missing
    amounts produce ``indeterminate``, never ``routine`` — so nothing
    silently zeroes this row's weight or reason before the materiality gate
    even gets a chance to run.
    """
    verdict = classify_transaction(_row(shares=None, price=None), InsiderHistory())

    assert verdict.label == "indeterminate"
    assert verdict.reason == "incomplete_amounts"
    assert verdict.weight == 0.5


# --- configurable thresholds: no hardcoded constants -----------------------
#
# Every number the classifier compares against is an ``InsiderSignalThresholds``
# field, not a module constant — these tests change the thresholds and check
# the verdict actually moves, which a hardcoded number could not do.

def test_custom_sell_fraction_threshold_changes_the_verdict():
    """The same 3% sale is routine noise under the 5% default but material
    (opportunistic) under a stricter 1% threshold — proving the boundary is
    read from ``thresholds``, not compiled into the function."""
    row = _row(direction="sell", shares=3_000.0, post_shares=97_000.0)

    default_verdict = classify_transaction(row, InsiderHistory())
    assert default_verdict.label == "routine"
    assert default_verdict.reason == "immaterial_stake_sale"

    strict_verdict = classify_transaction(
        row, InsiderHistory(),
        InsiderSignalThresholds(min_material_sell_fraction=0.01),
    )
    assert strict_verdict.label == "opportunistic"
    assert strict_verdict.reason == "material_stake_sale"


def test_custom_calendar_routine_years_changes_the_verdict():
    """One year of matching history is not routine under the literature's
    3-year default, but is routine under a 1-year threshold."""
    history = _history(days=[date(2025, 8, 14)])
    row = _row(transaction_date=date(2026, 8, 20))

    default_verdict = classify_transaction(row, history)
    assert default_verdict.label == "opportunistic"

    lenient_verdict = classify_transaction(
        row, history, InsiderSignalThresholds(calendar_routine_years=1),
    )
    assert lenient_verdict.label == "routine"
    assert lenient_verdict.reason == "calendar_routine"


def test_provider_threading_a_custom_sell_fraction_reaches_fetch(tmp_path):
    """The same configurability, exercised end-to-end through the provider
    constructor kwargs that ``src/pipeline.py`` wires from
    ``config.smart_money.insider_min_material_sell_fraction``."""
    lenient = _provider(
        tmp_path, min_transaction_value_usd=10_000,
        insider_min_material_sell_fraction=0.01,
    )
    row = _row(
        symbol="NVDA", direction="sell", shares=3_000.0, price=100.0,
        post_shares=97_000.0, transaction_date=date.today(),
    )
    _cached(lenient, [row])
    observations, _ = lenient.fetch(["NVDA"])
    assert [obs.signal_class for obs in observations] == ["opportunistic"]

    strict = _provider(tmp_path / "strict", min_transaction_value_usd=10_000)
    _cached(strict, [row])
    observations, _ = strict.fetch(["NVDA"])
    assert [obs.signal_class for obs in observations] == ["routine"]
