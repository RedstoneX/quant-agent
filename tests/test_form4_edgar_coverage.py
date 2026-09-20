"""EDGAR's own filing count, used as the coverage signal — board item 126.

THE DEFECT
----------
`data_status["smart_money"]` was set to "ok" whenever no provider error was
raised, with no reference to how much was actually fetched. So these two
produced identical evidence — zero rows, no error, a clean seat:

  * a genuinely quiet day, on which nobody filed a Form 4 on a listed
    issuer; and
  * a fetch that came back broken — an empty body, a body with no usable
    count in it, a page that stopped short of what EDGAR said was there.

The denominator was already in hand: EDGAR's EFTS search returns
`hits.total.value`, its own count of the Form 4s filed on the queried day.
`_discover` read it for pagination and threw it away.

WHAT IS TESTED HERE
-------------------
Every case is driven through `_discover` / `refresh` against EFTS-shaped
bodies, not through a mocked-out coverage dict, because the thing under
test IS the reading of those bodies.

  * a quiet day stays CLEAN (the loudest requirement item 126 makes);
  * a scan bounded by the desk's own cap or deadline stays CLEAN, because
    those are choices, not failures, and their residue is reported
    elsewhere;
  * every broken shape reads as NOT verified, with a named reason.
"""

from unittest.mock import Mock

import pytest

from src.data.smart_money import (
    UNVERIFIED_EDGAR_REASONS,
    SECForm4Provider,
    edgar_coverage,
    et_today,
)

NVDA_CIK = "1045810"
LISTED = {NVDA_CIK: {"NVDA": "Nasdaq"}}


def _hit(accession: str, cik: str = NVDA_CIK, form: str = "4") -> dict:
    return {"_source": {"adsh": accession, "form": form, "ciks": [cik]}}


def _accession(n: int) -> str:
    return f"{n:010d}-26-000001"


def _provider(tmp_path, **kwargs):
    provider = SECForm4Provider(data_dir=str(tmp_path), **kwargs)
    provider.session.get = Mock(side_effect=AssertionError("no live SEC calls"))
    return provider


def _bodies(by_page):
    """Fake EFTS returning a caller-chosen body per (day, from) page.

    `by_page` maps (startdt, from) -> the JSON body. A page with no entry
    gets the empty-but-well-formed body EDGAR sends for a day with nothing
    on it, which is the case that must stay clean.
    """

    def _get(url, *, params, deadline):
        key = (str(params.get("startdt")), int(params.get("from") or 0))
        response = Mock()
        response.json.return_value = by_page.get(
            key, {"hits": {"hits": [], "total": {"value": 0}}},
        )
        return response

    return _get


def _scan(provider, monkeypatch, by_page, symbols=("NVDA",)):
    monkeypatch.setattr(provider, "_get", _bodies(by_page))
    stats: dict = {}
    priority = provider._ciks_for_symbols(LISTED, list(symbols))
    found = provider._discover(LISTED, float("inf"), set(), priority, stats)
    return found, stats, edgar_coverage(stats)


# ---------------------------------------------------------------------------
# the quiet day, which must stay clean
# ---------------------------------------------------------------------------


def test_a_genuinely_quiet_day_is_verified_and_clean(tmp_path, monkeypatch):
    """EDGAR says zero filings and hands back zero rows. That is an ANSWER,
    and reporting it as degraded is the harm item 126 names out loud: the
    evidence gate treats a degraded seat as a loss, and on 2026-09-16 that
    made Risk veto a whole intraday plan."""
    provider = _provider(tmp_path, lookback_days=2)
    found, stats, coverage = _scan(provider, monkeypatch, {})

    assert found == []
    assert coverage["verified"] is True
    assert coverage["known"] is True
    assert coverage["edgar_total"] == 0
    assert coverage["enumerated"] == 0
    # "0 of 0" is not a fraction; claiming 1.0 would assert a completeness
    # nothing measured.
    assert coverage["ratio"] is None
    assert coverage["days_queried"] == 3 == coverage["days_with_total"]
    assert not (set(coverage["reasons"]) & UNVERIFIED_EDGAR_REASONS)


def test_a_full_day_read_to_edgars_own_count_is_verified(tmp_path, monkeypatch):
    """The ordinary good day: one page, EDGAR's count matches what came
    back, ratio 1.0."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=50)
    day = et_today().isoformat()
    hits = [_hit(_accession(i)) for i in range(1, 8)]
    found, stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": hits, "total": {"value": 7}}},
    })

    assert len(found) == 7
    assert coverage["verified"] is True
    assert (coverage["edgar_total"], coverage["enumerated"]) == (7, 7)
    assert coverage["ratio"] == 1.0
    assert coverage["reasons"] == []


def test_realistic_multi_page_pagination_walks_to_the_count(tmp_path, monkeypatch):
    """EFTS pages at 100. A 250-filing day is three requests, and the scan
    is only finished when `from` reaches EDGAR's own count — not when a page
    happens to come back short."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=500)
    day = et_today().isoformat()

    def page(start, count):
        return {"hits": {
            "hits": [_hit(_accession(i)) for i in range(start, start + count)],
            "total": {"value": 250},
        }}

    found, stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): page(1, 100),
        (day, 100): page(101, 100),
        (day, 200): page(201, 50),
    })

    assert len(found) == 250
    assert coverage["verified"] is True
    assert (coverage["edgar_total"], coverage["enumerated"]) == (250, 250)
    assert coverage["ratio"] == 1.0


def test_edgar_total_given_as_a_bare_integer_is_read(tmp_path, monkeypatch):
    """EFTS has answered with both `{"total": {"value": N}}` and a bare
    `{"total": N}`. Both are EDGAR telling us the count; neither may be
    read as "it did not say"."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": [_hit(_accession(1))], "total": 1}},
    })
    assert coverage["verified"] is True
    assert coverage["edgar_total"] == 1


# ---------------------------------------------------------------------------
# the desk's OWN bounds are not failures
# ---------------------------------------------------------------------------


def test_a_cap_bound_scan_is_still_verified(tmp_path, monkeypatch):
    """`max_filings_per_refresh` binding is the budget working as designed,
    and its residue is already reported through `pending_filings` and the
    per-issuer read-through map. If it flipped coverage to unverified the
    seat would read degraded every single morning, which is worse than the
    defect being fixed."""
    provider = _provider(tmp_path, lookback_days=3, max_filings_per_refresh=2)
    day = et_today().isoformat()
    hits = [_hit(_accession(i)) for i in range(1, 11)]
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": hits, "total": {"value": 10}}},
    })

    assert "scan_cap_reached" in coverage["reasons"]
    assert "days_not_queried" in coverage["reasons"]
    assert coverage["verified"] is True, coverage["reasons"]
    # The ratio is honest about it: 10 rows walked out of 10 EDGAR
    # reported FOR THE DAYS QUERIED, and the days not reached are named.
    assert coverage["days_queried"] < coverage["days_in_window"]


def test_a_day_the_budget_never_reached_is_not_counted_as_queried(
    tmp_path, monkeypatch,
):
    """A day the scan never asked about must not count toward the days it
    did ask about — "I checked nothing, so nothing is outstanding" is the
    exact silent-degradation shape this record exists to remove."""
    provider = _provider(tmp_path, lookback_days=9, max_filings_per_refresh=1)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": [_hit(_accession(1))], "total": {"value": 1}}},
    })
    assert coverage["days_queried"] == 1
    assert coverage["days_in_window"] == 10


# ---------------------------------------------------------------------------
# every broken shape — the point of the item
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "body", "reason"),
    [
        # HTTP 200, body says nothing at all. The old code coerced the
        # missing count to 0, decided the day was finished, and reported a
        # quiet day.
        ("empty object", {}, "edgar_total_unreadable"),
        ("no hits block", {"other": 1}, "edgar_total_unreadable"),
        ("hits block is not a dict", {"hits": []}, "edgar_total_unreadable"),
        ("no total key", {"hits": {"hits": []}}, "edgar_total_unreadable"),
        ("total is null", {"hits": {"hits": [], "total": None}},
         "edgar_total_unreadable"),
        ("total value is a word",
         {"hits": {"hits": [], "total": {"value": "many"}}},
         "edgar_total_unreadable"),
        ("total value is negative",
         {"hits": {"hits": [], "total": {"value": -1}}},
         "edgar_total_unreadable"),
        ("total value is a bool",
         {"hits": {"hits": [], "total": {"value": True}}},
         "edgar_total_unreadable"),
        # A count is present and non-zero and NOTHING came back with it.
        ("count with no rows",
         {"hits": {"hits": [], "total": {"value": 412}}},
         "edgar_returned_no_hits_for_nonzero_total"),
        # `hits.hits` is not a list — a 200 whose body this code cannot walk.
        ("hits is a string",
         {"hits": {"hits": "oops", "total": {"value": 5}}},
         "edgar_hits_unreadable"),
    ],
)
def test_a_broken_fetch_is_never_verified(tmp_path, monkeypatch, name, body, reason):
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {(day, 0): body})

    assert coverage["verified"] is False, f"{name}: {coverage}"
    assert reason in coverage["reasons"], f"{name}: {coverage['reasons']}"


def test_a_page_of_garbage_rows_cannot_buy_a_clean_status(tmp_path, monkeypatch):
    """The gaming shape: HTTP 200, a technically non-empty body, a count
    that matches the number of rows returned. Counting rows alone would
    read ratio 1.0 and call it verified. A whole page of rows that could
    not have answered a `forms=4` query is a body dressed as an answer."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    junk = [{"_source": {"adsh": "not-an-accession", "form": "10-K"}}] * 5
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": junk, "total": {"value": 5}}},
    })

    assert coverage["verified"] is False
    assert "edgar_hits_malformed" in coverage["reasons"]


def test_one_odd_row_among_good_ones_does_not_flip_the_seat(tmp_path, monkeypatch):
    """The other side of that guard, stated so nobody tightens it by
    accident: a single malformed row is EDGAR's business. Firing on one
    would make the seat degraded on any one-off oddity, and a seat that
    cries wolf daily is a seat nobody reads."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    rows = [_hit(_accession(1)), {"_source": {"adsh": "junk", "form": "3"}}]
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": rows, "total": {"value": 2}}},
    })

    assert coverage["verified"] is True
    assert "edgar_hits_malformed" not in coverage["reasons"]


def test_deep_pagination_cut_off_short_of_the_count_is_not_verified(
    tmp_path, monkeypatch,
):
    """EFTS caps deep pagination. A page that comes back shorter than the
    page size while EDGAR's own count says there is more is a truncated
    read — and the old loop treated exactly that as "finished"."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=500)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {
            "hits": [_hit(_accession(i)) for i in range(1, 101)],
            "total": {"value": 250},
        }},
        (day, 100): {"hits": {
            "hits": [_hit(_accession(i)) for i in range(101, 141)],
            "total": {"value": 250},
        }},
    })

    assert coverage["verified"] is False
    assert "edgar_page_short_of_total" in coverage["reasons"]
    # The ratio is reported, and it is the fact a reader wants: 140 of the
    # 250 EDGAR said were there.
    assert (coverage["edgar_total"], coverage["enumerated"]) == (250, 140)
    assert coverage["ratio"] == 0.56


def test_one_broken_day_in_a_window_of_good_ones_is_not_verified(
    tmp_path, monkeypatch,
):
    """Coverage is only as verified as the least verified day slice. A
    window that mostly worked is still a window the desk cannot account
    for."""
    provider = _provider(tmp_path, lookback_days=3, max_filings_per_refresh=500)
    today = et_today()
    from datetime import timedelta

    broken_day = (today - timedelta(days=2)).isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (broken_day, 0): {"hits": {"hits": [], "total": {"value": 300}}},
    })

    assert coverage["verified"] is False
    assert "edgar_returned_no_hits_for_nonzero_total" in coverage["reasons"]
    assert coverage["days_queried"] == 4


# ---------------------------------------------------------------------------
# the record survives to the places that read it
# ---------------------------------------------------------------------------


def test_refresh_records_coverage_where_the_seat_reads_it(tmp_path, monkeypatch):
    """The seat reads coverage from the manifest with no network. A figure
    taken after `refresh`'s own retention prune would measure the prune;
    this one is recorded during the fetch."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=50)
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: LISTED)
    monkeypatch.setattr(provider, "watched_form4_index", lambda *a, **k: ({}, []))
    monkeypatch.setattr(
        provider, "_submission", lambda *a, **k: ("<ownershipDocument/>", "u"),
    )
    monkeypatch.setattr(provider, "_parse_submission", lambda *a, **k: [])
    day = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _bodies({
        (day, 0): {"hits": {
            "hits": [_hit(_accession(1)), _hit(_accession(2))],
            "total": {"value": 2},
        }},
    }))

    result = provider.refresh(["NVDA"])

    assert result["edgar_coverage"]["verified"] is True
    assert result["edgar_coverage"]["ratio"] == 1.0
    assert provider.form4_coverage()["edgar"]["verified"] is True


def test_a_broken_refresh_records_an_unverified_coverage(tmp_path, monkeypatch):
    """The whole point, end to end: nothing came back, no exception was
    raised, and the recorded coverage says so."""
    provider = _provider(tmp_path, lookback_days=0)
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: LISTED)
    monkeypatch.setattr(provider, "watched_form4_index", lambda *a, **k: ({}, []))
    day = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _bodies({(day, 0): {}}))

    result = provider.refresh(["NVDA"])

    # No provider error — this is precisely why the status alone could not
    # tell anyone anything.
    assert result["error"] is None
    assert result["status"] == "ok"
    assert result["edgar_coverage"]["verified"] is False
    assert "edgar_total_unreadable" in result["edgar_coverage"]["reasons"]
    assert provider.form4_coverage()["edgar"]["verified"] is False


def test_a_manifest_written_before_this_shipped_reads_as_unverified(tmp_path):
    """Never-recorded is not evidence of a clean fetch. A cache from before
    board item 126 must read as unverified, not as complete."""
    import json

    provider = _provider(tmp_path, lookback_days=0)
    provider.manifest_path.write_text(json.dumps({
        "coverage_as_of": et_today().isoformat(),
        "watched_names": 4, "watched_names_read_through": 4,
        "watched_names_unread": [],
    }))

    coverage = provider.form4_coverage()
    assert coverage["known"] is True
    assert coverage["edgar"]["verified"] is False
    assert coverage["edgar"]["reasons"] == ["never_recorded"]


def test_stats_from_a_stubbed_discover_read_as_unverified():
    """`edgar_coverage` is handed whatever `_discover` recorded. A stub or
    an older caller that recorded nothing answers no question at all, and
    must not be read as answering it cleanly."""
    for stats in ({}, None, {"candidates": 3}, "not a dict"):
        coverage = edgar_coverage(stats)
        assert coverage["verified"] is False, stats
        assert coverage["reasons"] == ["never_recorded"], stats


def test_combined_provider_is_only_as_verified_as_its_least_verified_half():
    from src.data.congressional_trading import CombinedSmartMoneyProvider

    good = {"known": True, "verified": True, "reasons": [], "edgar_total": 10,
            "enumerated": 10, "ratio": 1.0, "days_queried": 1,
            "days_in_window": 1, "days_with_total": 1}
    bad = {**good, "verified": False, "reasons": ["edgar_page_short_of_total"],
           "enumerated": 4, "ratio": 0.4}

    def _sub(edgar):
        class _P:
            def form4_coverage(self):
                return {"known": True, "as_of": "2026-09-21", "watched": 2,
                        "read_through": 2, "unread": [], "edgar": edgar}
        return _P()

    combined = CombinedSmartMoneyProvider.__new__(CombinedSmartMoneyProvider)
    combined.providers = [_sub(good), _sub(bad)]
    merged = combined.form4_coverage()

    assert merged["edgar"]["verified"] is False
    assert "edgar_page_short_of_total" in merged["edgar"]["reasons"]
    assert merged["edgar"]["enumerated"] == 14
    assert merged["edgar"]["edgar_total"] == 20

    combined.providers = [_sub(good), _sub(dict(good))]
    assert combined.form4_coverage()["edgar"]["verified"] is True

    # A sub-provider that reports no `edgar` key at all.
    combined.providers = [_sub(good), _sub(None)]
    assert combined.form4_coverage()["edgar"]["verified"] is False


def test_the_pre_open_alert_names_the_unreadable_count(monkeypatch):
    """Item 126's first half: the fetch's own counts must reach the OWNER
    surface, not only the log. The pre-open alert is that surface."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    today = et_today().isoformat()
    clean = {
        "watched_read_through": today, "watched_pending_filings": 0,
        "watched_unchecked_names": [], "discovery_cap_reached": False,
        "watched_names": 4, "watched_names_read_through": 4,
        "watched_drain_ran": True, "watched_drain_deadline_hit": False,
        "edgar_coverage": {
            "verified": True, "reasons": [], "enumerated": 900,
            "edgar_total": 900, "days_queried": 2, "days_in_window": 366,
        },
    }
    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    alert(clean)
    assert sent == []

    alert({**clean, "edgar_coverage": {
        "verified": False, "reasons": ["edgar_returned_no_hits_for_nonzero_total"],
        "enumerated": 0, "edgar_total": 412, "days_queried": 2,
        "days_in_window": 366,
    }})
    assert len(sent) == 1
    assert "how many filings" in sent[0]
    assert "edgar_returned_no_hits_for_nonzero_total" in sent[0]
    # The counts themselves reach the owner, not only the reason word.
    assert "0 of 412" in sent[0]
    assert "2 of 366 days" in sent[0]


def test_the_pre_open_alert_fails_closed_on_a_missing_coverage_record(monkeypatch):
    """The alert and the morning seat must agree about a missing record.
    Written after a review found them reading the SAME fact in opposite
    directions: the seat treated an absent `edgar_coverage` as unverified
    and this alert treated it as nothing to say."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    today = et_today().isoformat()
    base = {
        "watched_read_through": today, "watched_pending_filings": 0,
        "watched_unchecked_names": [], "discovery_cap_reached": False,
        "watched_names": 4, "watched_names_read_through": 4,
        "watched_drain_ran": True, "watched_drain_deadline_hit": False,
    }
    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    alert(base)  # a Form 4 pass ran and recorded no coverage at all
    assert len(sent) == 1
    assert "no coverage was recorded at all" in sent[0]

    # ...but a refresh with no Form 4 half at all has nothing to say about
    # EDGAR coverage. It still alerts for the reason it always did — no
    # confirmed read-through — and must not add a coverage complaint on top.
    sent.clear()
    alert({"status": "ok", "congressional": {}})
    assert len(sent) == 1
    assert "filing service did not account" not in sent[0]


# ---------------------------------------------------------------------------
# what a review of this change found, and what was done about it
# ---------------------------------------------------------------------------


def test_a_replayed_page_cannot_buy_full_coverage(tmp_path, monkeypatch):
    """A cache or proxy that answers every offset with the SAME page used to
    walk `from` all the way to EDGAR's own count and report ratio 1.0 on
    100 real filings. Coverage counts DISTINCT readable rows, so it now
    reports what it actually saw."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=5000)
    day = et_today().isoformat()
    same_page = {"hits": {
        "hits": [_hit(_accession(i)) for i in range(1, 101)],
        "total": {"value": 1000},
    }}
    _found, _stats, coverage = _scan(
        provider, monkeypatch, {(day, start): same_page for start in range(0, 1000, 100)},
    )

    assert coverage["edgar_total"] == 1000
    assert coverage["rows_received"] == 1000
    assert coverage["enumerated"] == 100
    assert coverage["ratio"] == 0.1


def test_junk_rows_do_not_count_as_coverage_even_beside_a_good_one(
    tmp_path, monkeypatch,
):
    """The earlier guard fired only when EVERY row on a page was junk, so
    99 junk rows plus one good one read as full coverage — a 100% rule
    wearing a structural shape, which this desk forbids. Readable rows are
    now counted directly, so the ratio tells the truth with no cut point in
    it anywhere."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=5000)
    day = et_today().isoformat()
    rows = [{"_source": {"adsh": "junk", "form": "10-K"}} for _ in range(99)]
    rows.append(_hit(_accession(1)))
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": rows, "total": {"value": 100}}},
    })

    assert coverage["rows_received"] == 100
    assert coverage["enumerated"] == 1
    assert coverage["ratio"] == 0.01
    # The read is still ACCOUNTED FOR — everything EDGAR offered came back
    # and one row of it was usable. That is a different statement from "the
    # read cannot be accounted for", and the ratio is what carries it.
    assert coverage["verified"] is True


def test_a_day_of_nothing_but_junk_is_not_verified(tmp_path, monkeypatch):
    """The garbage-body case that remains a coverage failure rather than a
    low ratio: rows came back for the day and not one of them could have
    answered a `forms=4` query."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    junk = [{"_source": {"adsh": "nope", "form": "10-K"}} for _ in range(5)]
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {"hits": junk, "total": {"value": 5}}},
    })

    assert coverage["verified"] is False
    assert "edgar_hits_malformed" in coverage["reasons"]


def test_a_total_that_changes_mid_day_is_named(tmp_path, monkeypatch):
    """A later page disagreeing with the first about how much exists used
    to end the slice early with no reason recorded. Pagination now uses the
    PINNED first count and the disagreement is named."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=5000)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {
        (day, 0): {"hits": {
            "hits": [_hit(_accession(i)) for i in range(1, 101)],
            "total": {"value": 250},
        }},
        (day, 100): {"hits": {
            "hits": [_hit(_accession(i)) for i in range(101, 201)],
            "total": {"value": 100},
        }},
    })

    assert coverage["verified"] is False
    assert "edgar_total_changed" in coverage["reasons"]
    assert coverage["edgar_total"] == 250


def test_a_production_shaped_window_reports_how_little_it_reached(
    tmp_path, monkeypatch,
):
    """The production window is 365 lookback days and the market-wide pass
    is bounded by a deadline measured at ~153 s, so it reaches only the
    first few day slices. A ratio of 1.0 over two days of a 366-day window
    is an honest statement about two days and a misleading one about the
    window — so the fraction is reported beside the ratio, everywhere the
    ratio goes."""
    from src.data.smart_money import _RefreshDeadline

    provider = _provider(tmp_path, lookback_days=365, max_filings_per_refresh=5000)
    calls = {"n": 0}

    def _get(url, *, params, deadline):
        calls["n"] += 1
        if calls["n"] > 2:
            raise _RefreshDeadline()
        response = Mock()
        response.json.return_value = {"hits": {
            "hits": [_hit(_accession(calls["n"]))], "total": {"value": 1},
        }}
        return response

    monkeypatch.setattr(provider, "_get", _get)
    stats: dict = {}
    provider._discover(
        LISTED, float("inf"), set(),
        provider._ciks_for_symbols(LISTED, ["NVDA"]), stats,
    )
    coverage = edgar_coverage(stats)

    assert coverage["ratio"] == 1.0
    assert coverage["days_queried"] == 2
    assert coverage["days_in_window"] == 366
    assert coverage["window_fraction"] == round(2 / 366, 4)
    assert "days_not_queried" in coverage["reasons"]
    assert "refresh_deadline_exceeded" in coverage["reasons"]
    # Deliberately still verified: the deadline is the desk's own bound,
    # and the fraction beside the ratio is what stops 1.0 being read as a
    # statement about the whole window.
    assert coverage["verified"] is True


def test_yesterdays_coverage_is_not_todays(tmp_path):
    """A coverage record from an earlier pass describes an earlier fetch.
    Accepting it would reintroduce this defect one day late: nothing
    fetched today, seat reports clean."""
    import json

    from datetime import timedelta

    provider = _provider(tmp_path, lookback_days=0)
    yesterday = (et_today() - timedelta(days=1)).isoformat()
    provider.manifest_path.write_text(json.dumps({
        "coverage_as_of": yesterday,
        "watched_names": 4, "watched_names_read_through": 4,
        "watched_names_unread": [],
        "edgar_coverage": {"known": True, "verified": True, "reasons": [],
                           "edgar_total": 9, "enumerated": 9, "ratio": 1.0,
                           "days_queried": 1, "days_in_window": 1,
                           "days_with_total": 1},
    }))

    edgar = provider.form4_coverage()["edgar"]
    assert edgar["verified"] is False
    assert "edgar_coverage_stale" in edgar["reasons"]
