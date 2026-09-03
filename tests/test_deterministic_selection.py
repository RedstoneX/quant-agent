"""Pins the docs/WORK.md item 18b measurement so it cannot rot silently.

No LLM call: everything here is the frozen run-64290730 fixture run through
the desk's own stated rules. If a rule, a config number or the fixture
changes, these fail and the write-up gets re-derived rather than believed.
"""
import re
from pathlib import Path

import pytest
import yaml

from ops.model_policy import scenarios as S
from ops.model_policy.deterministic_selection import (
    AGREEMENT_CEILING_PCT,
    MAX_POSITION_RISK_PCT,
    catalyst_symbols,
    evaluate,
    summarise,
)

_SETTINGS = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
)


@pytest.fixture(scope="module")
def rows():
    return evaluate(
        S._SELECTION,
        S._SELECTION_ANALYSES,
        S._SELECTION_POSITIONS,
        S._SELECTION_NEWS,
    )


def test_constants_match_production_config():
    """The audit must gate on the desk's numbers, not a second opinion."""
    assert AGREEMENT_CEILING_PCT == _SETTINGS["risk"]["agreement_ceiling_pct"]
    assert MAX_POSITION_RISK_PCT == float(_SETTINGS["risk"]["max_position_risk_pct"])


def test_catalyst_parsing_reads_only_the_arrow_list():
    text = (
        "- [2026-08-27] Nvidia revenue forecast of 70% growth → NVDA, SMH, AMD\n"
        "- [2026-08-31] Anthropic signs deal with Lambda → NVDA\n"
    )
    # "Nvidia" and "Anthropic" appear in prose and must NOT become catalysts.
    assert catalyst_symbols(text) == {"NVDA", "SMH", "AMD"} | {"NVDA"}


def test_rules_admit_twelve_names_and_rank_none_of_them(rows):
    """THE finding. The rules gate and ceiling; they never choose."""
    summary = summarise(rows)
    assert summary["analysed"] == 59
    assert summary["eligible"] == 12
    assert summary["clears_rr_floor_alone"] == ["CHPX", "FLNC", "NKE", "PFE", "XLE"]
    assert summary["enters_via_catalyst_door"] == [
        "COP", "CRM", "CVX", "MSFT", "NVDA", "PATH", "TSM",
    ]
    # 13.5% of a 25% total-risk budget: every eligible name fits at once, so
    # no cap forces the desk to drop any of them. Nothing in the rule set
    # narrows twelve permitted names to one pick.
    assert summary["total_max_risk_pct"] == 13.5
    assert summary["total_max_risk_pct"] < 25.0
    assert summary["rules_name_a_single_pick"] is False


def test_subfloor_catalyst_door_is_reachable_only_by_news_covered_names(rows):
    """Why the door skews famous: state-change rows are written about the
    names the wires cover, so the sub-floor exception is available to
    mega-caps and effectively nobody else."""
    by_door = {r["symbol"] for r in rows if r["eligible"] and r["subfloor_catalyst"]}
    assert {"NVDA", "MSFT", "TSM"} <= by_door
    # Both famous-and-weak names the benchmark's `familiarity_bias` check
    # penalises are ADMITTED by the desk's own rules, at 0.5% risk.
    for symbol in ("NVDA", "MSFT"):
        row = next(r for r in rows if r["symbol"] == symbol)
        assert row["eligible"] is True
        assert row["max_risk_pct"] == 0.5


def test_three_of_the_five_qualified_shorts_are_refused_by_the_net_rule(rows):
    """GEV/UNH/NEE clear the R/R floor and are still refused deterministically:
    the §9.4 signed score nets a bullish earnings stance off the bearish
    technical one. The benchmark faults the model for passing them over."""
    refused = {}
    for symbol in ("GEV", "UNH", "NEE"):
        row = next(r for r in rows if r["symbol"] == symbol)
        assert row["rr"] >= 1.5
        assert row["eligible"] is False
        assert any(b.startswith("R5") for b in row["blocked_by"]), row["blocked_by"]
        refused[symbol] = row["net_sources"]
    assert refused == {"GEV": -1, "UNH": 0, "NEE": 0}
    # NKE and FLNC survive, so `takes_a_qualified_short` remains satisfiable.
    for symbol in ("NKE", "FLNC"):
        assert next(r for r in rows if r["symbol"] == symbol)["eligible"] is True


def test_block_reason_census(rows):
    census = {}
    for row in rows:
        for reason in row["blocked_by"]:
            key = re.match(r"R\d", reason).group(0)
            census[key] = census.get(key, 0) + 1
    assert census == {"R2": 21, "R4": 41, "R5": 14}
