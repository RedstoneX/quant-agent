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
