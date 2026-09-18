"""The holding detail view's "why do we hold this" assembly.

Two things are pinned here and they matter for different reasons.

**RSG's real recorded shape** (`tests/fixtures/holding_why_rsg_20260917.
json`, copied verbatim out of the production database on 2026-09-18) is
the case the owner actually complained about. The raw view rendered him
accession numbers, `temporary: true` and broker-eligibility JSON while
hiding the one interesting fact in it — a $720m open-market purchase by
Cascade Investment / Bill Gates. Every assertion about that fixture is an
assertion about what a person sees.

**The honest-fallback case** is the one that protects him from a
confident-looking lie. A position with nothing recorded must SAY nothing
is recorded, in every field, rather than omitting the field or showing a
zero. That is the path a pre-2026-09 position takes today.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.api.holding_why import (
    NOTHING_ACTS_ON_TARGET,
    build_holding_why,
    humanize_date,
    humanize_dollars,
    humanize_name,
)

FIXTURE = Path(__file__).parent / "fixtures" / "holding_why_rsg_20260917.json"


@pytest.fixture(scope="module")
def rsg() -> dict:
    data = json.loads(FIXTURE.read_text())
    return build_holding_why(data["entry"], data["evidence"], data["interim"])


def _all_readable_text(result: dict) -> str:
    """Every string a person would actually read, concatenated. Excludes
    `raw_evidence` on purpose — machine detail is allowed to live there."""
    return json.dumps(
        {"lede": result["lede"], "readable": result["readable"],
         "not_recorded": result["not_recorded"]}
    )


# --------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (720543738.73, "$721 million"),
        (1_280_000_000, "$1.3 billion"),
        (55_425_001.27, "$55.4 million"),
        (251_250, "$251,250"),
        (42.5, "$42.50"),
        (None, None),
        ("not a number", None),
    ],
)
def test_dollars_are_written_the_way_a_person_reads_them(value, expected):
    assert humanize_dollars(value) == expected


def test_block_capital_filer_names_become_readable_without_mangling_suffixes():
    # "III" must not become "Iii", and "L.L.C." must not become "L.l.c.".
    assert humanize_name("CASCADE INVESTMENT, L.L.C. / GATES WILLIAM H III") == (
        "Cascade Investment, L.L.C. / Gates William H III"
    )


def test_dates_are_written_the_way_a_person_writes_them():
    assert humanize_date("2026-09-11") == "11 September 2026"
    assert humanize_date("2026-09-11 14:26:39") == "11 September 2026"
    assert humanize_date("") is None
    assert humanize_date("not a date") is None


# --------------------------------------------------------------------
# RSG — the real recorded shape
# --------------------------------------------------------------------

def test_rsg_lede_is_one_sentence_naming_who_bought_how_much_when_and_at_what_price(rsg):
    lede = rsg["lede"]
    # One sentence: no sentence break anywhere in it. (A plain full-stop
    # count will not do — "L.L.C." is part of the filer's legal name.)
    assert lede.endswith(".")
    assert not re.search(r"\.\s+[A-Z]", lede)
    assert "Cascade Investment, L.L.C. / Gates William H III" in lede
    assert "$721 million" in lede
    assert "Republic Services Inc." in lede
    # The purchase DATES and PRICE — stored per transaction on the
    # smart-money finding's observations, nowhere else.
    assert "20 August 2026" in lede and "11 September 2026" in lede
    assert "$221.52" in lede


def test_rsg_names_one_primary_driver_not_a_list_of_everything_that_voted(rsg):
    assert rsg["readable"]["primary_driver"] == "Smart money"
    assert "SEC Form 4 scan" in rsg["readable"]["raised_by"]


def test_rsg_insider_facts_are_recorded_and_surfaced(rsg):
    insider = rsg["readable"]["insider"]
    assert insider["actor"] == "Cascade Investment, L.L.C. / Gates William H III"
    assert insider["role"] == "ten percent owner"
    assert insider["first_transaction_date"] == "2026-08-20"
    assert insider["last_transaction_date"] == "2026-09-11"
    assert insider["average_price"] == pytest.approx(221.52, abs=0.01)
    assert insider["not_recorded"] == []


def test_rsg_readable_section_carries_no_machine_output(rsg):
    text = _all_readable_text(rsg)
    # Accession numbers, internal booleans, enum values and JSON blobs —
    # each one verbatim from the panel the owner called gobbledygook.
    assert "0001104659" not in text
    assert "temporary" not in text
    assert "material_sec_form4_purchase" not in text
    assert "opportunistic_purchase" not in text
    assert '"eligible"' not in text
    assert "avg_dollar_volume" not in text
    # No raw float with a long mantissa anywhere a person reads.
    assert not re.search(r"\d+\.\d{4,}", text)


def test_rsg_machine_detail_is_kept_available_behind_the_toggle(rsg):
    raw = rsg["raw_evidence"]
    assert "0001104659-26-100306" in raw["admission"]["accessions"]
    assert raw["admission"]["temporary"] is True
    assert raw["admission"]["broker"]["eligible"] is True
    assert raw["identifiers"]["run_id"] == "run-a93b805c"


def test_rsg_take_profit_is_shown_as_a_reference_that_nothing_acts_on(rsg):
    tp = rsg["readable"]["take_profit"]
    assert tp["price"] == pytest.approx(224.20)
    assert tp["acted_on"] is False
    assert tp["note"] == NOTHING_ACTS_ON_TARGET
    assert "Nothing sells at this price" in tp["note"]


def test_rsg_horizon_is_the_pinned_plan_and_says_nothing_acts_on_it(rsg):
    horizon = rsg["readable"]["horizon"]
    assert horizon["sessions"] == 12
    assert "12 trading sessions" in horizon["plain"]
    assert "never updated" in horizon["note"]


def test_rsg_records_what_would_prove_the_thesis_wrong(rsg):
    assert rsg["readable"]["invalidation"].startswith("We are wrong if:")
    assert "213.33" in rsg["readable"]["invalidation"]
    assert rsg["readable"]["stop_price"] == pytest.approx(213.33)


def test_rsg_states_each_fact_once(rsg):
    """The smart-money purchase is stored in the admission, the seat's
    finding AND the PM's provenance; the thesis is stored in the PM
    target, the constructor's order and the trade row. The reader sees
    each once."""
    readable = rsg["readable"]
    statements = [readable["why"], readable["fundamental_reason"]]
    statements += [s["reason"] for s in readable["supporting"]]
    statements = [s for s in statements if s]
    normalised = [re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() for s in statements]
    assert len(normalised) == len(set(normalised))
    # The PM's "technical=buy, earnings=bullish, smart_money=bullish all
    # align" recap is dropped — the provenance sentences already say it,
    # in words, and the enum shorthand is not for a person.
    assert "=" not in (readable["why"] or "")
    assert "smart_money" not in _all_readable_text(rsg)


def test_rsg_has_nothing_missing(rsg):
    assert rsg["not_recorded"] == []


# --------------------------------------------------------------------
# the honest-fallback path
# --------------------------------------------------------------------

def test_a_holding_with_no_recorded_why_says_so_in_every_field():
    """A position opened before the desk recorded any of this. Nothing may
    be omitted, nothing may read as zero, and nothing may be inferred."""
    result = build_holding_why(
        {"symbol": "OLD", "action": "BUY", "price": 100.0}, [], [],
    )
    readable = result["readable"]
    assert result["lede"] == "Why OLD is held: Not recorded."
    assert readable["why"] is None
    assert readable["primary_driver"] is None
    assert readable["raised_by"] is None
    assert readable["insider"] is None
    assert readable["supporting"] == []
    assert readable["fundamental_reason"] is None
    assert readable["since_entry"] == []
    assert readable["horizon"]["sessions"] is None
    assert "Not recorded." in readable["horizon"]["plain"]
    assert readable["take_profit"]["price"] is None
    assert "Not recorded." in readable["take_profit"]["plain"]
    # Still true with nothing recorded, and still worth saying.
    assert readable["take_profit"]["acted_on"] is False
    assert readable["take_profit"]["note"] == NOTHING_ACTS_ON_TARGET
    assert readable["invalidation"] == "What would prove this wrong: Not recorded."
    assert result["not_recorded"] == [
        "why we opened it",
        "which seat raised it",
        "the fundamental reason",
        "the intended holding period",
        "the take-profit target",
        "what would prove the thesis wrong",
    ]


def test_a_holding_with_no_entry_row_at_all_still_returns_a_shape():
    result = build_holding_why(None, None, None)
    assert result["symbol"] == ""
    assert "Not recorded." in result["lede"]
    assert result["readable"]["take_profit"]["acted_on"] is False


def test_an_admission_without_the_paid_finding_admits_it_lacks_date_and_price():
    """The admission payload carries the aggregate value and the accession
    list but NO transaction date and NO price — those live only on the
    seat's `finding`. When the finding is absent the fields must say so
    rather than borrow `last_price`, which is the market price at scan
    time and not what the insider paid."""
    result = build_holding_why(
        {"symbol": "ACME", "action": "BUY", "price": 50.0},
        [{
            "agent_name": "smart_money_analyst", "kind": "admission",
            "evidence_json": json.dumps({
                "owners": ["SOME HOLDINGS LLC"], "transaction_value_usd": 12_300_000,
                "last_price": 49.5, "temporary": True,
                "accessions": ["0001104659-26-999999"],
            }),
        }],
        [],
    )
    insider = result["readable"]["insider"]
    assert insider["average_price"] is None
    assert insider["first_transaction_date"] is None
    assert "$12.3 million" in insider["plain"]
    assert "49.5" not in insider["plain"]
    assert insider["not_recorded"] == [
        "the price the insider paid", "the date the insider bought",
    ]
    assert result["readable"]["primary_driver"] == "Smart money"


# --------------------------------------------------------------------
# the route
# --------------------------------------------------------------------

def test_route_serves_the_plain_language_answer_and_404s_on_an_unheld_symbol(
    tmp_path, monkeypatch,
):
    """End to end through the real router, against a real temp SQLite DB
    seeded through the writer (the API-safety invariant only forbids
    `src/api/*.py` from writing, not the fixtures)."""
    import json as _json

    from fastapi.testclient import TestClient

    import src.api.db_reads as db_reads
    from src.api.server import app
    from src.storage.db import Database

    db_path = tmp_path / "why.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_trade(
        symbol="ZZZ", action="BUY", qty=5, price=100.0,
        reasoning="entry thesis text", run_id="run-why0001",
        stop_loss=95.0, take_profit=112.0, fill_status="filled",
        expected_horizon_sessions=8, setup_type="range",
        thesis_invalid_if="closes below 95 on volume",
    )
    db.insert_specialist_evidence(
        run_id="run-why0001", agent_name="smart_money_analyst", kind="admission",
        scope="symbol", symbol="ZZZ",
        evidence_json=_json.dumps({
            "owners": ["BIG FUND LP"], "transaction_value_usd": 480_000_000,
            "temporary": True, "accessions": ["0001104659-26-123456"],
            "broker": {"eligible": True, "name": "Zzz Corp.", "symbol": "ZZZ"},
        }),
    )
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/holdings/zzz/why")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "ZZZ"
    assert body["company_name"] == "Zzz Corp."
    assert "Big Fund LP" in body["lede"] and "$480 million" in body["lede"]
    assert body["readable"]["take_profit"]["acted_on"] is False
    assert body["readable"]["horizon"]["sessions"] == 8
    assert "closes below 95 on volume" in body["readable"]["invalidation"]
    # The accession number is reachable, but only behind the toggle.
    assert "0001104659-26-123456" not in _json.dumps(body["readable"])
    assert "0001104659-26-123456" in body["raw_evidence"]["admission"]["accessions"]

    assert client.get("/holdings/NOSUCH/why").status_code == 404


def test_a_round_number_of_millions_keeps_its_zero():
    """`480000000` is "$480 million", not "$48 million". Regression: a
    blanket trailing-zero strip (there to turn "1.0 billion" into "1
    billion") ate a real digit."""
    assert humanize_dollars(480_000_000) == "$480 million"
    assert humanize_dollars(1_000_000_000) == "$1 billion"
