"""Company profiles — the store, the PM facts block, and the Telegram line.

`src/data/company.py` promises two things above everything else: it never
raises, and a missing profile degrades to nothing rather than to a broken
section. These tests hold both wiring points to that promise.

No test here may touch the network. `CompanyProfileStore._fetch` is a
staticmethod over `yfinance`, so every test that exercises a cold path
patches it (or patches `yfinance` itself) and points the cache at tmp_path.
"""

import json
import time
from unittest.mock import patch

from src.data.company import (
    CompanyProfile,
    CompanyProfileStore,
    format_profiles_block,
)
from src.notifier import _append_trade_session_body
from src.pipeline_context import PMFacts

CAMECO = CompanyProfile(
    symbol="CCJ",
    name="Cameco Corporation",
    sector="Energy",
    industry="Uranium",
    country="Canada",
    employees=4200,
    market_cap=3.1e10,
    summary="Cameco provides uranium for the generation of electricity.",
)

UIPATH = CompanyProfile(
    symbol="PATH",
    name="UiPath Inc.",
    sector="Technology",
    industry="Software - Infrastructure",
    country="United States",
    employees=3800,
    market_cap=6.7e9,
    summary="UiPath offers an end-to-end automation platform.",
)


# === format_profiles_block / PMFacts render ===

def test_pm_block_renders_both_profiles_with_identity_and_summary():
    f = PMFacts(company_profiles=[UIPATH, CAMECO])
    out = f.render()

    assert "## Who These Companies Are" in out
    assert "**CCJ — Cameco Corporation**" in out
    assert "**PATH — UiPath Inc.**" in out
    # Industry, non-US country and cap ride the header line.
    assert "Uranium" in out
    assert "Canada" in out
    assert "$31.0B cap" in out
    # The PM — unlike Telegram — does get the business description.
    assert "uranium for the generation of electricity" in out
    # Sorted by symbol, so CCJ precedes PATH regardless of input order.
    assert out.index("CCJ — Cameco") < out.index("PATH — UiPath")


def test_pm_block_absent_entirely_when_no_profiles():
    out = PMFacts().render()
    assert "Who These Companies Are" not in out
    # The neighbouring sections must be undisturbed.
    assert "### Book State (current)" in out


def test_pm_block_absent_rather_than_empty_heading_when_all_unknown():
    """A cold cache with allow_fetch=False yields identity-less profiles.

    Rendering a heading over a list of "no company profile available" is
    worse than rendering nothing — it teaches PM the section is noise.
    """
    f = PMFacts(company_profiles=[
        CompanyProfile(symbol="CCJ"), CompanyProfile(symbol="PATH"),
    ])
    out = f.render()
    assert "Who These Companies Are" not in out
    assert "no company profile available" not in out


def test_pm_block_keeps_the_known_profiles_when_some_are_unknown():
    f = PMFacts(company_profiles=[CAMECO, CompanyProfile(symbol="ZZZZ")])
    out = f.render()
    assert "**CCJ — Cameco Corporation**" in out
    assert "ZZZZ" not in out


def test_pm_facts_render_survives_a_broken_profile_object():
    """A junk entry must cost the section, never the whole facts block."""
    class Exploding:
        name = "boom"

        def render(self):
            raise RuntimeError("kaboom")

    out = PMFacts(company_profiles=[Exploding()], invested_pct=61.5).render()
    assert "Who These Companies Are" not in out
    assert "invested=61.5%" in out


def test_format_profiles_block_returns_empty_string_for_nothing():
    assert format_profiles_block([]) == ""
    assert format_profiles_block([None]) == ""


# === CompanyProfileStore ===

def test_store_reads_a_fresh_cache_without_fetching(tmp_path):
    cache = tmp_path / "profiles.json"
    cache.write_text(json.dumps({"CCJ": {
        **CAMECO.as_dict(), "_fetched_at": time.time(),
    }}))
    store = CompanyProfileStore(cache_path=str(cache))

    with patch.object(CompanyProfileStore, "_fetch") as fetch:
        profile = store.get("ccj")

    fetch.assert_not_called()
    assert profile.name == "Cameco Corporation"


def test_store_returns_bare_profile_when_fetch_is_not_allowed(tmp_path):
    store = CompanyProfileStore(cache_path=str(tmp_path / "profiles.json"))

    with patch.object(CompanyProfileStore, "_fetch") as fetch:
        profile = store.get("CCJ", allow_fetch=False)

    fetch.assert_not_called()
    assert profile == CompanyProfile(symbol="CCJ")


def test_store_degrades_silently_when_yfinance_raises(tmp_path):
    """The documented contract: every path degrades to None, nothing raises."""
    store = CompanyProfileStore(cache_path=str(tmp_path / "profiles.json"))

    fake_yf = type("Mod", (), {
        "Ticker": staticmethod(
            lambda s: (_ for _ in ()).throw(RuntimeError("network down"))
        ),
    })
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        profile = store.get("CCJ")

    assert profile.symbol == "CCJ"
    assert profile.name is None
    assert profile.summary is None
    # And the empty result still renders as nothing in PM's facts.
    assert "Who These Companies Are" not in PMFacts(
        company_profiles=[profile],
    ).render()


def test_store_maps_yfinance_info_onto_the_dataclass(tmp_path):
    store = CompanyProfileStore(cache_path=str(tmp_path / "profiles.json"))

    class _Ticker:
        def __init__(self, symbol):
            self.info = {
                "longName": "Cameco Corporation",
                "sector": "Energy",
                "industry": "Uranium",
                "country": "Canada",
                "fullTimeEmployees": 4200,
                "marketCap": 3.1e10,
                "longBusinessSummary": "Cameco provides uranium.",
                "quoteType": "EQUITY",
            }

    fake_yf = type("Mod", (), {"Ticker": _Ticker})
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        profile = store.get("CCJ")

    assert profile.name == "Cameco Corporation"
    assert profile.industry == "Uranium"
    assert profile.employees == 4200
    assert profile.is_etf is False


# === Telegram trade alerts ===

def _trade_body(orders, profiles):
    lines: list[str] = []
    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: profiles,
    ):
        _append_trade_session_body(lines, {"orders": orders})
    return lines


def test_telegram_alert_names_the_companies_it_traded():
    lines = _trade_body(
        [{"symbol": "CCJ", "action": "BUY", "qty": 40},
         {"symbol": "PATH", "action": "SELL", "qty": 12}],
        {"CCJ": CAMECO, "PATH": UIPATH},
    )
    body = "\n".join(lines)
    assert "CCJ — Cameco Corporation · Uranium" in body
    assert "PATH — UiPath Inc. · Software - Infrastructure" in body


def test_telegram_identity_line_stays_compact():
    """One line per symbol, no business summary, no paragraph wrap."""
    lines = _trade_body(
        [{"symbol": "CCJ", "action": "BUY", "qty": 40}], {"CCJ": CAMECO},
    )
    identity = [ln for ln in lines if "Cameco" in ln]
    assert len(identity) == 1
    assert len(identity[0]) < 100
    # The full PM-only summary must never reach Telegram.
    assert "generation of electricity" not in "\n".join(lines)
    assert CAMECO.summary not in "\n".join(lines)


def test_telegram_alert_never_fetches_over_the_network():
    """allow_fetch=False: an operator alert must not wait on yfinance."""
    seen = {}

    def _capture(self, symbols, allow_fetch=True):
        seen["allow_fetch"] = allow_fetch
        return {}

    lines: list[str] = []
    with patch.object(CompanyProfileStore, "get_many", _capture), \
            patch.object(CompanyProfileStore, "_fetch") as fetch:
        _append_trade_session_body(
            lines, {"orders": [{"symbol": "CCJ", "action": "BUY"}]},
        )

    assert seen["allow_fetch"] is False
    fetch.assert_not_called()


def test_telegram_body_intact_when_the_profile_lookup_fails():
    """The order list is the point of the alert; identity is a garnish."""
    lines: list[str] = []
    with patch.object(
        CompanyProfileStore, "get_many",
        lambda self, symbols, allow_fetch=True: (_ for _ in ()).throw(
            RuntimeError("cache exploded"),
        ),
    ):
        _append_trade_session_body(
            lines, {"orders": [{"symbol": "CCJ", "action": "BUY", "qty": 40}]},
        )

    body = "\n".join(lines)
    assert "orders: 1" in body
    assert "CCJ" in body
    assert "who:" not in body


def test_telegram_omits_symbols_the_cache_does_not_know():
    lines = _trade_body(
        [{"symbol": "CCJ", "action": "BUY"}, {"symbol": "ZZZZ", "action": "BUY"}],
        {"CCJ": CAMECO, "ZZZZ": CompanyProfile(symbol="ZZZZ")},
    )
    body = "\n".join(lines)
    assert "Cameco" in body
    assert "ZZZZ —" not in body


def test_telegram_adds_no_identity_section_on_a_no_trade_day():
    lines: list[str] = []
    _append_trade_session_body(lines, {"orders": []})
    assert "orders: 0" in lines
    assert "who:" not in lines


# === PM facts assembly ===

def test_build_pm_facts_degrades_silently_when_the_store_explodes():
    """A profile failure must not cost the session its facts block."""
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    with patch.object(
        CompanyProfileStore, "__init__",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("no disk")),
    ):
        facts = _pm_facts_with_stubs(pipeline)

    assert facts.company_profiles == []
    assert "Who These Companies Are" not in facts.render()


def test_build_pm_facts_only_looks_up_symbols_in_scope():
    """Held + candidates, never the configured universe."""
    from src.models import Position
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    asked = {}

    def _get_many(self, symbols, allow_fetch=True):
        asked["symbols"] = list(symbols)
        return {s: CompanyProfile(symbol=s) for s in symbols}

    position = Position(
        symbol="CCJ", qty=40, avg_entry=50, current_price=58,
        market_value=2320, unrealized_pnl=320, sector="Energy",
    )
    with patch.object(CompanyProfileStore, "get_many", _get_many):
        _pm_facts_with_stubs(pipeline, positions=[position])

    assert asked["symbols"] == ["CCJ"]


def _pm_facts_with_stubs(pipeline, positions=None):
    """Run _build_pm_facts with every other data source stubbed out."""
    from unittest.mock import MagicMock

    pipeline.db = MagicMock()
    pipeline.db.compute_trade_calibration.return_value = {}
    pipeline.db.get_recent_agent_outputs.return_value = []
    with patch.object(
        type(pipeline), "_build_position_history", lambda self, p: {},
    ), patch.object(
        type(pipeline), "_build_portfolio_heat", lambda self, p, tv: None,
    ):
        return pipeline._build_pm_facts(
            positions=positions or [], analyses=[],
            total_value=100000.0, cash=40000.0,
            recent_performance={},
        )
