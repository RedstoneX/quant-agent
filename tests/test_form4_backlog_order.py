"""Form 4 discovery spends its budget on watched names first.

2026-09-18: `intra_check` expired the `smart_money` seat and the evidence
gate refused to decide. The trigger was not a new filing on a name the desk
watches — it was the desk's own unread backlog. `_discover` broke on
`max_filings_per_refresh` inside the freshest day slice, so watched filings
sat unread behind filings from companies the desk does not trade, and
`peek_accessions` treated a market-wide cache ticker as relevant.
"""

import json
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


def test_watched_filing_is_discovered_even_when_the_cap_binds_on_other_names(
    tmp_path, monkeypatch,
):
    """The defect, reproduced: a full day slice of filings on names the desk
    does not trade must not push its own name out of the budget."""
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
    # Three unwatched filings come back FIRST, then the watched one — the
    # order EFTS returns them in is not ours to choose.
    monkeypatch.setattr(provider, "_get", _efts({
        (day0, 0): [
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000002-26-000001", "9000002"),
            _hit("0000000003-26-000001", "9000003"),
            _hit("0000000004-26-000001", "1045810"),
        ],
    }))

    stats: dict = {}
    priority = provider._ciks_for_symbols(listed, ["NVDA"])
    found = provider._discover(listed, float("inf"), set(), priority, stats)

    assert len(found) == 2, found
    # The watched filing is present AND first, so the submission downloads
    # that follow are spent on it before anything else.
    assert found[0]["accession"] == "0000000004-26-000001"
    assert found[0]["cik"] == "1045810"
    # Non-watched filings are reordered, never dropped: external candidate
    # nomination still needs them.
    assert any(f["cik"] == "9000001" for f in found)
    # The residue is measured, not guessed.
    assert stats["candidates"] == 4
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
            _hit("0000000001-26-000001", "9000001"),
            _hit("0000000002-26-000001", "9000001"),
            _hit("0000000003-26-000001", "1045810"),
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
    assert result["pending_filings"] == 3
    assert result["watched_pending_filings"] == 1
    assert result["discovery_cap_reached"] is True
    manifest = json.loads(provider.manifest_path.read_text())
    assert manifest["pending_filings"] == 3
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


def test_freshness_ignores_backlog_and_never_runs_a_crawl(tmp_path, monkeypatch):
    """THE ACCEPTANCE CONDITION for the six lost windows.

    An unread accession filed on or before the watermark is backlog, not new
    information, and the probe must reach that verdict without EFTS.
    """
    provider = _provider(tmp_path, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(
        provider, "_discover",
        Mock(side_effect=AssertionError("freshness must not run a crawl")),
    )
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": [],           # 000001 was NEVER read: backlog
        "watched_read_through": "2026-09-18",
    }))
    monkeypatch.setattr(provider, "_get", _submissions({
        "1045810": [("0000000001-26-000001", "2026-09-15")],
    }))

    verdict = provider.form4_freshness(["NVDA"])

    assert verdict["ok"] is True
    assert verdict["new_filings"] == []


def test_freshness_expires_on_a_filing_after_the_watermark(tmp_path, monkeypatch):
    provider = _provider(tmp_path, lookback_days=365)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "processed_accessions": ["0000000001-26-000001"],
        "watched_read_through": "2026-09-15",
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
    })
    assert len(sent) == 1
    text = sent[0]
    # Plain words: the owner is not a developer and must be able to act on it.
    assert "will not make a new trading decision" in text
    for jargon in ("Form 4", "accession", "watermark", "EDGAR", "peek", "cap"):
        assert jargon not in text, f"{jargon!r} is not plain language"
