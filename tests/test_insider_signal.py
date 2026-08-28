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


def test_non_open_market_code_is_routine():
    """Awards, option exercises and tax withholding carry no direction."""
    verdict = classify_transaction(_row(code="", direction="buy"), InsiderHistory())

    assert verdict.label == "routine"
    assert verdict.reason == "non_open_market_code"


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
