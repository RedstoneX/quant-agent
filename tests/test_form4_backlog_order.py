"""Form 4 discovery spends its budget on watched names first.

2026-09-18: `intra_check` expired the `smart_money` seat and the evidence
gate refused to decide. The trigger was not a new filing on a name the desk
watches — it was the desk's own unread backlog. `_discover` broke on
`max_filings_per_refresh` inside the freshest day slice, so watched filings
sat unread behind filings from companies the desk does not trade, and
`peek_accessions` treated a market-wide cache ticker as relevant.

2026-09-23: the fix for that shipped an exit condition — `len(priority) >=
cap` whenever watched names were supplied — that production could never
reach, so `_discover` ran until the refresh deadline and the market-wide
read loop raised on an expired one. The watched-first guarantee this file
was written for now belongs to the watched-name drain (#539); what stays
here is the ORDERING, the reachable cap, and the alarm for a market-wide
pass that reads nothing at all.
"""

import json
from datetime import timedelta
from unittest.mock import Mock

from src.data.congressional_trading import CombinedSmartMoneyProvider
from src.data.smart_money import SECForm4Provider


def _hit(accession: str, cik: str) -> dict:
    return {"_source": {"adsh": accession, "form": "4", "ciks": [cik]}}


def _efts(pages: dict[tuple[str, int], list[dict]]):
    """Fake EFTS: (startdt, from) -> hits. Missing key means no hits."""

    def _get(url, *, params, deadline):
        key = (str(params.get("startdt")), int(params.get("from") or 0))
        hits = pages.get(key, [])
        total = sum(
            len(v) for (day, _from), v in pages.items()
            if day == str(params.get("startdt"))
        )
        response = Mock()
        response.json.return_value = {
            "hits": {"hits": hits, "total": {"value": total}},
        }
        return response

    return _get


def _provider(tmp_path, **kwargs):
    provider = SECForm4Provider(data_dir=str(tmp_path), **kwargs)
    provider.session.get = Mock(side_effect=AssertionError("no live SEC calls"))
    return provider


def test_watched_filings_already_found_are_emitted_first_when_the_cap_binds(
    tmp_path, monkeypatch,
):
    """Watched-first ORDERING, which is all `priority_ciks` still buys.

    Until 2026-09-23 this test asserted more: that the scan kept going past
    a full market-wide bucket so a watched filing later in the stream could
    displace a non-watched one. That guarantee moved to the watched-name
    drain (#539), which asks each watched issuer directly on its own
    budget — and buying it here cost `_discover` its only reachable exit
    condition, which is the regression this file's sibling test covers.
    What `_discover` still owes the desk is that whatever watched filings it
    DID find are handed back first, so the submission downloads that follow
    are spent on the desk's own names before anything else.
    """
    provider = _provider(tmp_path, max_filings_per_refresh=2, lookback_days=1)
    listed = {
        "1045810": {"NVDA": "Nasdaq"},
        "9000001": {"ZZZA": "NYSE"},
        "9000002": {"ZZZB": "NYSE"},
        "9000003": {"ZZZC": "NYSE"},
    }
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    # One unwatched filing, then the watched one, then more unwatched — the
    # order EFTS returns them in is not ours to choose.
    monkeypatch.setattr(provider, "_get", _efts({
        (day0, 0): [
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000004-26-000001", "1045810"),
            _hit("0000000002-26-000001", "9000002"),
            _hit("0000000003-26-000001", "9000003"),
        ],
    }))

    stats: dict = {}
    priority = provider._ciks_for_symbols(listed, ["NVDA"])
    found = provider._discover(listed, float("inf"), set(), priority, stats)

    assert len(found) == 2, found
    # The watched filing is FIRST, ahead of the unwatched one that arrived
    # before it.
    assert found[0]["accession"] == "0000000004-26-000001"
    assert found[0]["cik"] == "1045810"
    # Non-watched filings are reordered, never dropped: external candidate
    # nomination still needs them.
    assert any(f["cik"] == "9000001" for f in found)
    # The residue is measured, not guessed.
    assert stats["candidates"] == 3
    assert stats["watched_candidates"] == 1
    assert stats["cap_reached"] is True


def test_no_watched_symbols_leaves_discovery_exactly_as_it_was(
    tmp_path, monkeypatch,
):
    provider = _provider(tmp_path, max_filings_per_refresh=2, lookback_days=1)
    listed = {"9000001": {"ZZZA": "NYSE"}, "9000002": {"ZZZB": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _efts({
        (day0, 0): [
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000002-26-000001", "9000002"),
            _hit("0000000003-26-000001", "9000001"),
        ],
    }))

    found = provider._discover(listed, float("inf"), set(), set(), {})
    assert [f["accession"] for f in found] == [
        "0000000001-26-000001", "0000000002-26-000001",
    ]


def test_peek_is_scoped_to_watched_names_not_the_market_wide_cache(
    tmp_path, monkeypatch,
):
    """A filing on a cached-but-unwatched ticker is not a change to
    remembered research. `refresh` caches the whole listed market, so
    unioning the cache in made the relevant set market-wide."""
    provider = _provider(tmp_path, max_filings_per_refresh=5, lookback_days=1)
    provider.observations_path.write_text(json.dumps([
        {"symbol": "ZZZA", "accession_number": "0000000001-26-000001"},
    ]))
    listed = {"1045810": {"NVDA": "Nasdaq"}, "9000001": {"ZZZA": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(provider, "_discover", lambda *_: [
        {"accession": "0000000009-26-000001", "form": "4", "cik": "9000001"},
    ])

    peeked = provider.peek_accessions(symbols=["NVDA"])

    assert "0000000009-26-000001" not in peeked
    assert peeked == {"0000000001-26-000001"}


def test_refresh_reports_the_unread_backlog_and_records_it(tmp_path, monkeypatch):
    provider = _provider(tmp_path, max_filings_per_refresh=1, lookback_days=1)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "9000001": {"ZZZA": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _efts({
        (day0, 0): [
            _hit("0000000003-26-000001", "1045810"),
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000002-26-000001", "9000001"),
        ],
    }))
    # Submission download fails: the backlog must still be counted, because
    # "read nothing, three waiting" is exactly the state that refuses a
    # midday decision tomorrow.
    monkeypatch.setattr(
        provider, "_submission",
        Mock(side_effect=RuntimeError("submission unavailable")),
    )

    result = provider.refresh(["NVDA"])

    assert result["processed_filings"] == 0
    # Two candidates were SEEN before the market-wide bucket filled: the
    # watched one and the first unwatched one. The third is behind the cap,
    # which is the cap doing its job.
    assert result["pending_filings"] == 2
    assert result["watched_pending_filings"] == 1
    assert result["discovery_cap_reached"] is True
    manifest = json.loads(provider.manifest_path.read_text())
    assert manifest["pending_filings"] == 2
    assert manifest["watched_pending_filings"] == 1
    assert manifest["discovery_cap_reached"] is True


def test_deadline_keeps_the_watched_filings_already_found(tmp_path, monkeypatch):
    """A deadline used to discard every filing discovered so far — which is
    the dangerous direction for `peek_accessions`: it returns the known set,
    the expiry detector sees no new filing, and superseded research is
    silently reused. The partial, watched-first set is handed back instead.
    """
    from src.data.smart_money import _RefreshDeadline

    provider = _provider(tmp_path, max_filings_per_refresh=50, lookback_days=3)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "9000001": {"ZZZA": "NYSE"}}
    calls = {"n": 0}

    def _get(url, *, params, deadline):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _RefreshDeadline("refresh_deadline_exceeded")
        response = Mock()
        response.json.return_value = {"hits": {"hits": [
            _hit("0000000004-26-000001", "1045810"),
            _hit("0000000001-26-000001", "9000001"),
        ], "total": {"value": 999}}}
        return response

    monkeypatch.setattr(provider, "_get", _get)
    stats: dict = {}
    found = provider._discover(
        listed, float("inf"), set(),
        provider._ciks_for_symbols(listed, ["NVDA"]), stats,
    )

    assert [f["accession"] for f in found] == [
        "0000000004-26-000001", "0000000001-26-000001",
    ]
    assert stats["deadline_hit"] is True


def test_combined_provider_passes_watched_names_and_surfaces_the_backlog():
    class _Form4:
        def __init__(self):
            self.seen = None

        def refresh(self, symbols=None):
            self.seen = symbols
            return {"status": "ok", "pending_filings": 7,
                    "watched_pending_filings": 2,
                    "discovery_cap_reached": True}

        def fetch(self, symbols):
            return [], None

    class _Congress:
        def refresh(self):
            return {"status": "ok"}

        def fetch(self, symbols):
            return [], None

    form4 = _Form4()
    combined = CombinedSmartMoneyProvider([form4, _Congress()])

    result = combined.refresh(["NVDA", "CRM"])

    assert form4.seen == ["NVDA", "CRM"]
    assert result["pending_filings"] == 7
    assert result["watched_pending_filings"] == 2
    assert result["discovery_cap_reached"] is True


# --- since-watermark freshness, and the drain that makes it honest ---------
#
# 2026-09-18 follow-up. The six lost decision windows were not caused by a new
# filing; they were caused by the tick asking a COMPLETENESS question ("is
# there a filing I have not read?") that only a full-text crawl can answer.
# The tick now asks a FRESHNESS question ("was anything filed on a watched
# name since our last confirmed read?"), answered from each issuer's own
# filing history. That is only sound because the drain below separately takes
# the watched residue to zero and refuses to advance the watermark otherwise.


def _submissions(by_cik: dict[str, list[tuple[str, str]]]):
    """Fake data.sec.gov submissions: CIK -> [(accession, filing_date)]."""

    def _get(url, *, params, deadline):
        cik = str(int(url.rsplit("CIK", 1)[1].split(".")[0]))
        rows = by_cik.get(cik, [])
        response = Mock()
        response.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["4"] * len(rows),
                    "filingDate": [d for _a, d in rows],
                    "accessionNumber": [a for a, _d in rows],
                },
            },
        }
        return response

    return _get


def test_freshness_reports_backlog_on_unread_names_without_a_crawl(
    tmp_path, monkeypatch,
):
    """THE ACCEPTANCE CONDITION for the six lost windows, restated per issuer.

    Backlog lives only on names never read through, and it is reported as
    exactly that — `unread_names`, not `new_filings` — without EFTS.

    REPLACES `test_freshness_ignores_backlog_and_never_runs_a_crawl`
    (2026-09-19). That test pinned "an unread filing dated on or before the
    single watermark is backlog". On a name that has been read through that
    rule is the defect `test_freshness_sees_a_filing_made_on_the_read_through_day`
    below reproduces: every filing made on the watermark's own date after the
    pre-market read was hidden as backlog. The no-crawl half is kept.
    """
    provider = _provider(tmp_path, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "320193": {"AAPL": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(
        provider, "_discover",
        Mock(side_effect=AssertionError("freshness must not run a crawl")),
    )
    from src.data.smart_money import et_today
    today = et_today().isoformat()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": [],
        "watched_read_through_by_cik": {"1045810": today},
    }))
    monkeypatch.setattr(provider, "_get", _submissions({
        "1045810": [],
        # AAPL was never read through and 000001 was never read: backlog.
        "320193": [("0000000001-26-000001", today)],
    }))

    verdict = provider.form4_freshness(["NVDA", "AAPL"])

    assert verdict["new_filings"] == []
    assert verdict["unread_names"] == ["320193"]
    assert verdict["unread_filings"] == 1
    assert verdict["covered"] == 1
    # Partial coverage is never "current".
    assert verdict["ok"] is False
    assert "not yet fully read" in verdict["reason"]


def test_freshness_sees_a_filing_made_on_the_read_through_day(
    tmp_path, monkeypatch,
):
    """The false-REUSE hole in the single watermark, reproduced.

    The pre-market drain ran at 08:00 ET and stamped today. A Form 4 filed
    at 14:00 ET the same day carries today's filing date, so `filed <=
    watermark` hid it as "backlog" on every intraday tick — the seat reused
    superseded findings all afternoon. On a name read through, an unread
    filing can only be new.
    """
    provider = _provider(tmp_path, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today
    today = et_today().isoformat()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": ["0000000001-26-000001"],
        # Both shapes written, so the single-watermark code reads its own.
        "watched_read_through": today,
        "watched_read_through_by_cik": {"1045810": today},
    }))
    monkeypatch.setattr(provider, "_get", _submissions({
        "1045810": [
            ("0000000002-26-000002", today),     # filed after the morning read
            ("0000000001-26-000001", today),
        ],
    }))

    verdict = provider.form4_freshness(["NVDA"])

    assert verdict["ok"] is True
    assert verdict["new_filings"] == ["0000000002-26-000002"]


def test_freshness_expires_on_a_filing_after_the_watermark(tmp_path, monkeypatch):
    provider = _provider(tmp_path, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": ["0000000001-26-000001"],
        "watched_read_through": "2026-09-15",
        "watched_read_through_by_cik": {"1045810": "2026-09-15"},
    }))
    monkeypatch.setattr(provider, "_get", _submissions({
        "1045810": [
            ("0000000002-26-000002", "2026-09-16"),   # after the watermark
            ("0000000001-26-000001", "2026-09-15"),
        ],
    }))

    verdict = provider.form4_freshness(["NVDA"])

    assert verdict["ok"] is True
    assert verdict["new_filings"] == ["0000000002-26-000002"]


def test_freshness_without_a_watermark_is_unknown_not_clean(tmp_path, monkeypatch):
    """No confirmed read means no honest freshness claim. Fail closed."""
    provider = _provider(tmp_path, lookback_days=365)
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: {})
    verdict = provider.form4_freshness(["NVDA"])
    assert verdict["ok"] is False
    assert verdict["new_filings"] == []


def test_freshness_partial_failure_is_unknown_not_clean(tmp_path, monkeypatch):
    """One unreadable name is exactly the one that might have filed."""
    provider = _provider(tmp_path, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "320193": {"AAPL": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": [], "watched_read_through": "2026-09-18",
        "watched_read_through_by_cik": {
            "1045810": "2026-09-18", "320193": "2026-09-18",
        },
    }))
    good = _submissions({"1045810": []})

    def _get(url, *, params, deadline):
        if "0000320193" in url:
            raise RuntimeError("SEC 503")
        return good(url, params=params, deadline=deadline)

    monkeypatch.setattr(provider, "_get", _get)

    verdict = provider.form4_freshness(["NVDA", "AAPL"])

    assert verdict["ok"] is False
    assert verdict["unchecked"] == ["320193"]


def test_drain_reads_watched_residue_and_advances_the_watermark(
    tmp_path, monkeypatch,
):
    """The other half. The watermark is only sound if the backlog is driven
    to zero, so the drain is bounded by the desk's own names, not by
    market-wide filing volume."""
    provider = _provider(tmp_path, max_filings_per_refresh=1, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "9000001": {"ZZZA": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    # The market-wide pass sees only a non-watched filing and its cap binds
    # there — exactly 2026-09-18's shape.
    efts = _efts({(day0, 0): [_hit("0000000009-26-000009", "9000001")]})
    subs = _submissions({
        "1045810": [("0000000003-26-000003", day0)],
        "9000001": [],
    })

    def _get(url, *, params, deadline):
        if "submissions" in url:
            return subs(url, params=params, deadline=deadline)
        return efts(url, params=params, deadline=deadline)

    monkeypatch.setattr(provider, "_get", _get)
    monkeypatch.setattr(
        provider, "_submission", lambda filing, deadline: ("<xml/>", "u"),
    )
    monkeypatch.setattr(provider, "_parse_submission", lambda *a, **k: [])

    result = provider.refresh(["NVDA"])

    # The watched filing the market-wide cap could not reach was read anyway.
    assert "0000000003-26-000003" in provider.known_accessions()
    assert result["watched_pending_filings"] == 0
    assert result["watched_read_through"] == day0
    assert provider.read_through_date() == day0


def test_watermark_does_not_advance_when_the_drain_cannot_finish(
    tmp_path, monkeypatch,
):
    """A drain that could not read every watched name must leave the
    watermark where it was — and the stale watermark is what makes the next
    tick refuse, plus what the pre-open check alerts on."""
    provider = _provider(tmp_path, max_filings_per_refresh=1, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": [], "watched_read_through": "2026-09-01",
    }))
    efts = _efts({})
    subs = _submissions({"1045810": [("0000000003-26-000003", day0)]})

    def _get(url, *, params, deadline):
        if "submissions" in url:
            return subs(url, params=params, deadline=deadline)
        return efts(url, params=params, deadline=deadline)

    monkeypatch.setattr(provider, "_get", _get)
    monkeypatch.setattr(
        provider, "_submission",
        Mock(side_effect=RuntimeError("submission unavailable")),
    )

    result = provider.refresh(["NVDA"])

    assert result["watched_pending_filings"] == 1
    assert result["watched_read_through"] == "2026-09-01"
    assert provider.read_through_date() == "2026-09-01"


def test_combined_provider_freshness_is_fail_closed_on_a_subprovider_error():
    class _Bad:
        def form4_freshness(self, symbols=None):
            raise RuntimeError("EDGAR unreachable")

    combined = CombinedSmartMoneyProvider.__new__(CombinedSmartMoneyProvider)
    combined.providers = [_Bad()]
    verdict = combined.form4_freshness(["NVDA"])
    assert verdict["ok"] is False


def test_pre_open_check_alerts_before_the_day_is_lost(monkeypatch):
    """`refresh` always computed these numbers; the pipeline only logged
    them. A returned value nobody catches is a check that does not exist —
    on 2026-09-18 the first anyone knew was six lost decision windows later.
    """
    import src.notifier as notifier
    from src.pipeline import TradingPipeline
    from src.util.time import et_today

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text: sent.append(text))
    check = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    # Clean morning: silence.
    check({
        "watched_read_through": et_today().isoformat(),
        "watched_pending_filings": 0, "watched_unchecked_names": [],
        "discovery_cap_reached": False,
    })
    assert sent == []

    # The 2026-09-18 shape: cap bound, watched filings unread, watermark stale.
    check({
        "watched_read_through": "2026-09-17",
        "watched_pending_filings": 4, "watched_unchecked_names": [],
        "discovery_cap_reached": True,
        "watched_names": 82, "watched_names_read_through": 60,
    })
    assert len(sent) == 1
    text = sent[0]
    # CHANGED 2026-09-19: this asserted "will not make a new trading
    # decision". PR #535 made the insider seat advisory, so that sentence
    # became false; the alert now says what actually happens, and how much
    # of the watched set IS read.
    assert "will not make a new trading decision" not in text
    assert "without complete insider evidence" in text
    assert "60 of our 82 companies" in text
    for jargon in ("Form 4", "accession", "watermark", "EDGAR", "peek", "cap"):
        assert jargon not in text, f"{jargon!r} is not plain language"


# ---------------------------------------------------------------------------
# 2026-09-19: the drain gets its own budget and keeps progress per issuer.
#
# Measured that day, read-only against SEC: 82 watched issuers, 5,431 unread
# Form 4s inside the 365-day window, 0.156 s per filing read — ~858 s of
# reading against the 180 s the drain SHARED with a market-wide pass that
# runs first and measured ~153 s on its own. So the single watermark was
# never written and the insider seat was stale on every tick.


class _Clock:
    """A monotonic clock the test advances; stands in for wall time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _deadline_aware(get, clock_provider):
    """Wrap a fake `_get` so it honours the deadline exactly like the real one."""

    def _get(url, *, params, deadline):
        clock_provider._remaining(deadline)   # raises _RefreshDeadline
        return get(url, params=params, deadline=deadline)

    return _get


def test_drain_has_its_own_deadline_after_the_market_wide_pass(
    tmp_path, monkeypatch,
):
    """The market-wide pass spending its whole budget must not starve the
    drain. Before 2026-09-19 both shared one deadline, so this refresh ended
    with the watched filing unread and no watermark."""
    import src.data.smart_money as sm

    clock = _Clock()
    monkeypatch.setattr(sm.time, "monotonic", clock)
    provider = _provider(
        tmp_path, lookback_days=365, refresh_deadline_s=180,
        watched_drain_deadline_s=859,
    )
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    day0 = sm.et_today().isoformat()

    def _slow_discover(listed_, deadline, processed, priority, stats):
        clock.now += 181   # the market-wide pass uses up its own 180 s
        stats.update(candidates=0, watched_candidates=0, cap_reached=False,
                     deadline_hit=True, busiest_day_total=0)
        return []

    monkeypatch.setattr(provider, "_discover", _slow_discover)
    monkeypatch.setattr(provider, "_get", _deadline_aware(
        _submissions({"1045810": [("0000000003-26-000003", day0)]}), provider,
    ))
    monkeypatch.setattr(
        provider, "_submission",
        lambda filing, deadline: (provider._remaining(deadline), ("<xml/>", "u"))[1],
    )
    monkeypatch.setattr(provider, "_parse_submission", lambda *a, **k: [])

    result = provider.refresh(["NVDA"])

    assert "0000000003-26-000003" in provider.known_accessions()
    assert result["watched_drain_deadline_hit"] is False
    assert result["watched_read_through"] == day0
    assert provider.read_through_by_cik() == {"1045810": day0}


def test_drain_keeps_each_finished_issuer_when_the_budget_runs_out(
    tmp_path, monkeypatch,
):
    """One unfinished issuer must not throw away the others' work, and the
    next morning must resume, not restart. Smallest residue goes first so
    the most issuers finish for any budget."""
    import src.data.smart_money as sm

    clock = _Clock()
    monkeypatch.setattr(sm.time, "monotonic", clock)
    provider = _provider(
        tmp_path, lookback_days=365, watched_drain_deadline_s=10,
    )
    listed = {"1045810": {"NVDA": "Nasdaq"}, "320193": {"AAPL": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(provider, "_discover", lambda *a, **k: [])
    day0 = sm.et_today().isoformat()
    history = {
        # NVDA: 3 unread. AAPL: 1 unread — read first.
        "1045810": [(f"0000000001-26-00000{n}", day0) for n in (1, 2, 3)],
        "320193": [("0000000002-26-000001", day0)],
    }
    monkeypatch.setattr(
        provider, "_get", _deadline_aware(_submissions(history), provider),
    )
    reads: list[str] = []

    def _submission(filing, deadline):
        provider._remaining(deadline)
        reads.append(filing["accession"])
        clock.now += 4   # each read costs 4 s of a 10 s budget
        return "<xml/>", "u"

    monkeypatch.setattr(provider, "_submission", _submission)
    monkeypatch.setattr(provider, "_parse_submission", lambda *a, **k: [])

    first = provider.refresh(["NVDA", "AAPL"])

    assert reads[0] == "0000000002-26-000001", "smallest residue first"
    assert first["watched_drain_deadline_hit"] is True
    assert provider.read_through_by_cik() == {"320193": day0}
    assert first["watched_names"] == 2
    assert first["watched_names_read_through"] == 1
    assert first["watched_names_unread"] == ["NVDA"]
    # The single all-names date must NOT advance on a partial drain.
    assert first["watched_read_through"] == ""
    assert first["watched_pending_filings"] == 1

    # Next morning: fresh budget, resumes with the one NVDA filing left.
    reads.clear()
    clock.now += 10_000
    second = provider.refresh(["NVDA", "AAPL"])
    assert reads == ["0000000001-26-000003"]
    assert provider.read_through_by_cik() == {"320193": day0, "1045810": day0}
    assert second["watched_names_unread"] == []
    assert second["watched_read_through"] == day0
    assert provider.form4_coverage()["unread"] == []


def test_combined_provider_surfaces_the_drain_outcome_to_the_pre_open_check(
    monkeypatch,
):
    """Production wraps the Form 4 provider in `CombinedSmartMoneyProvider`.
    Until 2026-09-19 that wrapper lifted only three counts to the top level,
    so the pre-open check read an empty read-through date and would have
    alerted every morning, drain or no drain."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline
    from src.util.time import et_today

    today = et_today().isoformat()

    class _Form4:
        def refresh(self, symbols=None):
            return {
                "status": "ok", "error": None, "pending_filings": 0,
                "watched_pending_filings": 0, "discovery_cap_reached": False,
                "watched_drain_ran": True, "watched_drain_read": 3,
                "watched_unchecked_names": [], "watched_drain_deadline_hit": False,
                "watched_read_through": today, "watched_names": 82,
                "watched_names_read_through": 82, "watched_names_unread": [],
                # Board item 126: EDGAR's own filing count, read and walked.
                "edgar_coverage": {
                    "known": True, "verified": True, "reasons": [],
                    "edgar_total": 900, "enumerated": 900, "ratio": 1.0,
                    "days_queried": 15, "days_in_window": 15,
                    "days_with_total": 15,
                },
            }

    combined = CombinedSmartMoneyProvider.__new__(CombinedSmartMoneyProvider)
    combined.providers = [_Form4()]
    result = combined.refresh(["NVDA"])
    assert result["watched_read_through"] == today
    assert result["watched_names_read_through"] == 82

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text: sent.append(text))
    TradingPipeline._alert_form4_backlog_before_open.__get__(object())(result)
    assert sent == []


def test_pre_market_backlog_is_recorded_where_the_desk_records_status():
    """`discovery_cap_reached` and `watched_pending_filings` were computed
    every morning and only ever logged. They are now a specialist-evidence
    row, so successive mornings can be compared from the database."""
    from types import SimpleNamespace
    from src.pipeline import TradingPipeline

    rows: list[dict] = []

    class _Db:
        def insert_specialist_evidence(self, **kwargs):
            rows.append(kwargs)

    obj = SimpleNamespace(db=_Db())
    TradingPipeline._record_form4_backlog.__get__(obj)("run-1", {
        "watched_pending_filings": 4, "discovery_cap_reached": True,
        "watched_names": 82, "watched_names_read_through": 60,
        "watched_names_unread": ["WMT"],
    })
    assert len(rows) == 1
    assert rows[0]["kind"] == "form4_backlog"
    payload = json.loads(rows[0]["evidence_json"])
    assert payload["watched_pending_filings"] == 4
    assert payload["discovery_cap_reached"] is True
    assert payload["watched_names_read_through"] == 60


def test_drain_budget_fits_inside_the_job_that_runs_it():
    """The drain budget is derived from a measurement, and it must also fit
    inside the systemd job, or systemd kills the job mid-write and the
    earnings step after it never runs. Every term below is cited at
    src/config.py::SmartMoneyConfig.watched_drain_deadline_s."""
    import re
    from pathlib import Path

    import yaml

    from src.config import SmartMoneyConfig

    root = Path(__file__).resolve().parent.parent
    unit = (root / "scripts/systemd/quant-agent-earnings_preprocess.service").read_text()
    timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M).group(1))
    deployed = yaml.safe_load((root / "config/settings.yaml").read_text())["smart_money"]
    startup_s = 2             # 2026-09-18 journal, 12:00:39 -> 12:00:41 UTC
    after_refresh_max_s = 147  # 2026-09-17 journal, 12:03:14 -> 12:05:41 UTC
    for source in (deployed, SmartMoneyConfig().model_dump()):
        total = (
            startup_s + float(source["refresh_deadline_s"])
            + float(source["watched_drain_deadline_s"]) + after_refresh_max_s
        )
        assert total <= timeout, (total, timeout)
    # The config's own ceiling is exactly the room that sum leaves.
    field = SmartMoneyConfig.model_fields["watched_drain_deadline_s"]
    le = next(m.le for m in field.metadata if hasattr(m, "le"))
    assert le == timeout - startup_s - 180 - after_refresh_max_s


# ---------------------------------------------------------------------------
# 2026-09-23: the exit condition that could not be reached
# ---------------------------------------------------------------------------
#
# PR #513 (2026-09-18) made `_discover` stop on `len(priority) >= cap`
# whenever watched names were supplied. `priority` only ever holds the ~82
# watched issuers' filings, who file 13-31 a day, so with a cap of 1,000 it
# could never be reached — and production always supplies watched names.
# The only terminator left was `refresh_deadline_s`, so discovery spent the
# whole 180 s paginating EDGAR and the read loop then raised on an expired
# deadline. Market-wide reads: 1,000/run on 2026-09-15..18, then 0, 31 and
# 13 on 2026-09-21..23 — and those 31 and 13 were the watched drain's
# [measured: production log].


def test_discovery_stops_on_the_cap_when_watched_names_are_supplied(
    tmp_path, monkeypatch,
):
    """The regression itself: supplying watched names must not remove the
    scan's only reachable exit condition."""
    provider = _provider(tmp_path, max_filings_per_refresh=3, lookback_days=30)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    listed.update({f"90000{i:02d}": {f"ZZ{i:02d}": "NYSE"} for i in range(1, 40)})
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    # Every day in the window is full of market-wide filings, as the real
    # stream is, and one watched filing sits on the freshest day — so the
    # watched bucket is non-empty but can never reach the cap.
    pages = {}
    for days_ago in range(31):
        day = (et_today() - timedelta(days=days_ago)).isoformat()
        page = [
            _hit(f"{days_ago:04d}00000{i}-26-000001", f"90000{i:02d}")
            for i in range(1, 6)
        ]
        if days_ago == 0:
            page.insert(0, _hit("0000000004-26-000001", "1045810"))
        pages[(day, 0)] = page
    monkeypatch.setattr(provider, "_get", _efts(pages))

    stats: dict = {}
    priority = provider._ciks_for_symbols(listed, ["NVDA"])
    # A deadline that never expires, so the ONLY way out is the cap. Before
    # the fix this call did not return.
    found = provider._discover(listed, float("inf"), set(), priority, stats)

    assert stats["cap_reached"] is True
    assert stats["deadline_hit"] is False
    # One day slice was enough to fill the budget; the scan did not walk the
    # whole window.
    assert stats["edgar_days_queried"] == 1
    assert stats["edgar_days_in_window"] == 31
    assert len(found) == 3
    # Ordering is untouched: the watched filing is still handed back first.
    assert found[0]["accession"] == "0000000004-26-000001"


def test_watched_names_are_still_read_by_the_drain_when_the_cap_binds_at_once(
    tmp_path, monkeypatch,
):
    """Watched-name behaviour is unchanged. The market-wide bucket fills on
    the first page and the scan stops, and the watched filing is read anyway
    — by the drain (#539), which asks the issuer directly on its own budget.
    """
    provider = _provider(tmp_path, max_filings_per_refresh=1, lookback_days=1)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "9000001": {"ZZZA": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _efts({
        (day0, 0): [
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000002-26-000001", "9000001"),
            _hit("0000000004-26-000001", "1045810"),
        ],
    }))
    monkeypatch.setattr(
        provider, "watched_form4_index",
        lambda ciks, deadline: (
            {"1045810": [("0000000004-26-000001", day0)]}, [],
        ),
    )
    reads: list[str] = []

    def _submission(filing, deadline):
        reads.append(filing["accession"])
        raise RuntimeError("body not under test")

    monkeypatch.setattr(provider, "_submission", _submission)

    result = provider.refresh(["NVDA"])

    # The market-wide pass never saw the watched filing — the cap bound
    # first — and the drain read it regardless.
    assert "0000000004-26-000001" in reads
    assert result["watched_drain_ran"] is True


# ---------------------------------------------------------------------------
# the alarm: a market-wide pass that reads NOTHING must say so
# ---------------------------------------------------------------------------


def _blind_refresh(tmp_path, monkeypatch, *, submissions_fail: bool):
    provider = _provider(tmp_path, max_filings_per_refresh=5, lookback_days=1)
    listed = {"1045810": {"NVDA": "Nasdaq"}, "9000001": {"ZZZA": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    from src.data.smart_money import et_today

    day0 = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _efts({
        (day0, 0): [
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000002-26-000001", "9000001"),
        ],
    }))
    monkeypatch.setattr(
        provider, "watched_form4_index", lambda ciks, deadline: ({}, []),
    )
    if submissions_fail:
        monkeypatch.setattr(
            provider, "_submission",
            Mock(side_effect=RuntimeError("submission unavailable")),
        )
    else:
        monkeypatch.setattr(
            provider, "_submission",
            lambda filing, deadline: ("no parseable body", "url"),
        )
        monkeypatch.setattr(
            provider, "_parse_submission",
            lambda body, *, source_url, listed: [],
        )
    return provider, provider.refresh(["NVDA"])


def test_a_market_wide_pass_that_reads_nothing_reports_itself_blind(
    tmp_path, monkeypatch,
):
    """The five-session silence, closed. Zero market-wide reads with unread
    candidates outstanding is its own recorded fact, on the result and in
    the manifest, and it survives into `form4_coverage` for the morning seat
    to read without the network."""
    provider, result = _blind_refresh(tmp_path, monkeypatch, submissions_fail=True)

    assert result["market_wide_read"] == 0
    assert result["pending_filings"] > 0
    assert result["market_wide_blind"] is True
    manifest = json.loads(provider.manifest_path.read_text())
    assert manifest["market_wide_blind"] is True
    assert provider.form4_coverage()["market_wide_blind"] is True


def test_a_market_wide_pass_that_reads_normally_is_not_blind(
    tmp_path, monkeypatch,
):
    """The other half, and the one that decides whether this alarm is worth
    having: an ordinary pass must never fire it."""
    provider, result = _blind_refresh(tmp_path, monkeypatch, submissions_fail=False)

    assert result["market_wide_read"] == 2
    assert result["market_wide_blind"] is False
    assert provider.form4_coverage()["market_wide_blind"] is False


def test_a_quiet_day_with_nothing_unread_is_not_blind(tmp_path, monkeypatch):
    """Zero read because there was nothing to read is a fact, not a fault —
    the same rule board item 126 makes for a quiet EDGAR day."""
    provider = _provider(tmp_path, max_filings_per_refresh=5, lookback_days=1)
    listed = {"9000001": {"ZZZA": "NYSE"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(provider, "_get", _efts({}))
    monkeypatch.setattr(
        provider, "watched_form4_index", lambda ciks, deadline: ({}, []),
    )

    result = provider.refresh(["ZZZA"])

    assert result["market_wide_read"] == 0
    assert result["pending_filings"] == 0
    assert result["market_wide_blind"] is False


def test_blindness_from_an_earlier_pass_is_not_reported_as_todays(
    tmp_path, monkeypatch,
):
    """Coverage is a statement about the pass that ran today. A record left
    by an earlier one must not page again — the same ageing rule the EDGAR
    coverage record already follows."""
    provider, _result = _blind_refresh(tmp_path, monkeypatch, submissions_fail=True)
    manifest = json.loads(provider.manifest_path.read_text())
    manifest["coverage_as_of"] = "2026-01-02"
    provider.manifest_path.write_text(json.dumps(manifest))

    assert provider.form4_coverage()["market_wide_blind"] is False
