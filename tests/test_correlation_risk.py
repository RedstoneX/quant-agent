"""Correlation-cluster risk rule + matrix construction."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src.config import RiskConfig
from src.data.correlation import (
    build_correlation_matrix,
    cluster_peers,
    correlation_clusters,
    highly_correlated_peers,
)
from src.models import OHLCV, Position, TradeDecision
from src.risk.rules import RiskRuleEngine


def _bars(prices: list[float]) -> list[OHLCV]:
    start = date(2026, 1, 1)
    return [
        OHLCV(date=start + timedelta(days=i), open=p, high=p, low=p, close=p, volume=1_000_000)
        for i, p in enumerate(prices)
    ]


def test_correlation_matrix_detects_parallel_moves():
    """Two series with identical daily returns should correlate +1.0."""
    # Same multiplicative returns → corr = 1.0
    base = [
        100.0,
        101.0,
        102.0,
        103.5,
        102.0,
        104.5,
        105.0,
        106.0,
        104.0,
        107.0,
        108.5,
        109.0,
        110.0,
        108.0,
        111.0,
        112.0,
        113.0,
        111.5,
        114.0,
        115.0,
        116.0,
        117.0,
        116.0,
        118.0,
        119.0,
    ]
    parallel = [p * 2.5 for p in base]  # same pct returns, different price level
    matrix = build_correlation_matrix({"A": _bars(base), "B": _bars(parallel)})
    assert matrix["A"]["B"] == pytest.approx(1.0, abs=0.01)
    assert matrix["B"]["A"] == pytest.approx(1.0, abs=0.01)


def test_correlation_matrix_skips_sparse_symbols():
    """Symbols with <10 bars are dropped (no returns). Healthy pair with enough
    overlap (30 bars → 29 returns ≥ the 20 min_periods) does appear."""
    healthy_a = _bars([100.0 + i for i in range(30)])
    healthy_b = _bars([200.0 + i * 2 for i in range(30)])
    matrix = build_correlation_matrix(
        {
            "A": healthy_a,
            "B": healthy_b,
            "SPARSE": _bars([100.0, 101.0]),  # only 2 bars — dropped upstream
        }
    )
    assert "SPARSE" not in matrix
    assert "A" in matrix
    assert "B" in matrix["A"]  # the pair correlation exists


def test_correlation_matrix_logs_excluded_symbols(caplog):
    """When a symbol gets silently dropped (insufficient bars), the operator
    + the downstream Risk Manager need to see which holdings lost correlation
    coverage. Without this WARN log, a freshly-listed ETF could bypass the
    cluster check unnoticed — exactly the kind of silent gap the audit
    flagged after the 2026-05-11 universe expansion added CHPX (a newly
    launched ETF with very short price history).
    """
    import logging

    healthy_a = _bars([100.0 + i for i in range(30)])
    sparse_b = _bars([100.0, 101.0])  # only 2 bars
    sparse_c = _bars([200.0])  # only 1 bar
    with caplog.at_level(logging.WARNING, logger="src.data.correlation"):
        build_correlation_matrix(
            {
                "A": healthy_a,
                "BAD_B": sparse_b,
                "BAD_C": sparse_c,
            }
        )

    warning_lines = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("excluded from matrix" in m for m in warning_lines), (
        f"expected a WARN log naming excluded symbols; got {warning_lines}"
    )
    # The dropped symbols must appear in the warning so they can be
    # correlated against the trade book.
    joined = " ".join(warning_lines)
    assert "BAD_B" in joined and "BAD_C" in joined


def test_highly_correlated_peers_threshold():
    """Only pairs at or above threshold are returned."""
    matrix = {
        "NVDA": {"AVGO": 0.82, "AAPL": 0.55, "JPM": 0.15},
    }
    peers = highly_correlated_peers("NVDA", ["AVGO", "AAPL", "JPM"], matrix, threshold=0.7)
    assert peers == ["AVGO"]


def test_correlation_cluster_advisory_fires():
    """NVDA + held AVGO + held GOOGL, all correlated 0.85, should flag the cluster
    advisory when combined exposure exceeds max_correlated_cluster_pct."""
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=30,
            max_total_position_pct=95,
            require_stop_loss=True,
        )
    )
    # Existing held positions: AVGO + GOOGL each 22% ($22k of $100k). Propose NVDA 15%.
    # Cluster total = 22 + 22 + 15 = 59% > 50% cap → advisory fires.
    positions = [
        Position(
            symbol="AVGO",
            qty=50,
            avg_entry=400,
            current_price=440,
            market_value=22000,
            unrealized_pnl=2000,
            sector="Technology",
        ),
        Position(
            symbol="GOOGL",
            qty=60,
            avg_entry=300,
            current_price=366,
            market_value=22000,
            unrealized_pnl=3960,
            sector="Communication Services",
        ),
    ]
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=15,
        entry_price=200,
        stop_loss=190,
        take_profit=220,
        reasoning="AI momentum continuing",
    )
    corr_matrix = {
        "NVDA": {"AVGO": 0.85, "GOOGL": 0.82},
        "AVGO": {"NVDA": 0.85, "GOOGL": 0.78},
        "GOOGL": {"NVDA": 0.82, "AVGO": 0.78},
    }

    with patch(
        "src.execution.broker._get_sector",
        side_effect=lambda s: {"NVDA": "Technology", "AVGO": "Technology", "GOOGL": "Communication Services"}.get(
            s, "Unknown"
        ),
    ):
        violations = engine.check(
            decision=decision,
            positions=positions,
            total_value=100_000,
            correlation_matrix=corr_matrix,
            max_correlated_cluster_pct=50.0,
        )

    rules = [v.rule for v in violations]
    assert "correlation_cluster" in rules, f"expected advisory, got {rules}"


def test_correlation_cluster_uses_gross_multiplier_for_leveraged_etfs():
    """Inverse / leveraged ETFs (SQQQ -3x, SDS -2x) consume their abs
    leverage of notional regardless of direction. The correlation cluster
    cap must count that gross exposure, same as sector and position caps
    already do.

    Pre-fix this rule used raw market_value (1x), undercounting any
    cluster that contained an inverse / leveraged ETF — for SQQQ that's
    a 3x undercount that could let a high-concentration tech cluster
    through unflagged while the LLM was making "diversified" claims.
    """
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=80,
            max_total_position_pct=300,
            require_stop_loss=True,
        )
    )
    # Held: SQQQ $10k (3x inverse → gross 30k), SDS $5k (2x inverse →
    # gross 10k), GOOGL $20k (1x). Total raw 35k, total gross 60k.
    positions = [
        Position(
            symbol="SQQQ",
            qty=100,
            avg_entry=100,
            current_price=100,
            market_value=10_000,
            unrealized_pnl=0,
            sector="Broad",
        ),
        Position(
            symbol="SDS", qty=50, avg_entry=100, current_price=100, market_value=5_000, unrealized_pnl=0, sector="Broad"
        ),
        Position(
            symbol="GOOGL",
            qty=20,
            avg_entry=1000,
            current_price=1000,
            market_value=20_000,
            unrealized_pnl=0,
            sector="Communication Services",
        ),
    ]
    # GOOGL highly correlated with both inverse ETFs (by absolute return —
    # tech moves drive both index longs and inverse bets).
    corr_matrix = {
        "GOOGL": {"SQQQ": 0.85, "SDS": 0.82, "JPM": 0.3},
    }
    decision = TradeDecision(
        action="BUY",
        symbol="GOOGL",
        allocation_pct=10,
        entry_price=1000,
        stop_loss=950,
        take_profit=1100,
        reasoning="theme continuation",
    )
    with patch(
        "src.execution.broker._get_sector",
        side_effect=lambda s: {
            "GOOGL": "Communication Services",
            "SQQQ": "Broad",
            "SDS": "Broad",
        }.get(s, "Unknown"),
    ):
        violations = engine.check(
            decision=decision,
            positions=positions,
            total_value=100_000,
            correlation_matrix=corr_matrix,
            max_correlated_cluster_pct=40.0,
        )

    # Post-fix cluster math:
    #   peer_value = SQQQ × 3 + SDS × 2 = 30k + 10k = 40k
    #   gross_new = GOOGL × 1 = 10k
    #   cluster_pct = (40 + 10) / 100 = 50%
    # 50% > 40% cap → advisory fires.
    # Pre-fix would have computed peer_value = 10k + 5k = 15k (raw,
    # no gross_mul), cluster_pct = 25%, no advisory → silent miss.
    rules = [v.rule for v in violations]
    assert "correlation_cluster" in rules, (
        f"expected correlation_cluster advisory (cluster gross = 50% > 40% cap); got {rules}"
    )
    cluster_violation = next(v for v in violations if v.rule == "correlation_cluster")
    assert cluster_violation.value >= 45.0, (
        f"violation value should reflect gross sum (~50%), not raw market_value (~25%); got {cluster_violation.value}"
    )


def test_correlation_cluster_silent_when_below_threshold():
    """If peers are lightly correlated, no cluster advisory."""
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=30,
            max_total_position_pct=95,
            require_stop_loss=True,
        )
    )
    positions = [
        Position(
            symbol="JPM",
            qty=100,
            avg_entry=200,
            current_price=220,
            market_value=22000,
            unrealized_pnl=2000,
            sector="Financial Services",
        ),
    ]
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=15,
        entry_price=200,
        stop_loss=190,
        take_profit=220,
        reasoning="x",
    )
    # A two-name matrix has no structure to read and would now (correctly,
    # conservatively) come back as one cluster, so the book here is a real
    # one: a tight tech pair plus JPM sitting outside it.
    corr_matrix = {
        "NVDA": {"AVGO": 0.88, "JPM": 0.30},
        "AVGO": {"NVDA": 0.88, "JPM": 0.28},
        "JPM": {"NVDA": 0.30, "AVGO": 0.28},
    }  # JPM is not in NVDA's cluster
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = engine.check(
            decision=decision,
            positions=positions,
            total_value=100_000,
            correlation_matrix=corr_matrix,
        )
    assert not any(v.rule == "correlation_cluster" for v in violations)


# --- Structural clustering (item 186): no correlation cutoff anywhere ---


def _sym_matrix(pairs: dict, symbols: list[str]) -> dict:
    out: dict[str, dict[str, float]] = {s: {} for s in symbols}
    for (a, b), v in pairs.items():
        out[a][b] = v
        out[b][a] = v
    return out


def test_no_cutoff_constant_is_exported():
    """The module must not reintroduce a picked correlation level."""
    import src.data.correlation as corr_mod

    assert not hasattr(corr_mod, "CLUSTER_CORRELATION_THRESHOLD")


def test_clusters_read_a_theme_out_of_a_mixed_book():
    import itertools

    syms = ["OKLO", "CEG", "VST", "CCJ", "JPM", "KO", "XOM"]
    theme = {"OKLO", "CEG", "VST", "CCJ"}
    pairs = {(a, b): (0.88 if a in theme and b in theme else 0.12) for a, b in itertools.combinations(syms, 2)}
    assert correlation_clusters(syms, _sym_matrix(pairs, syms)) == [["CCJ", "CEG", "OKLO", "VST"]]


def test_structureless_book_falls_back_to_one_cluster():
    """No jump in the tree -> nothing to read -> ration as one bet, not none.

    MEASURED trade-off, pinned here so it cannot regress silently: a book with
    no structure (every pair alike) returns ONE cluster rather than none. That
    over-rations rather than under-rations, which is the safe direction, and
    it is what lets the cut work without any guard constant.
    """
    import itertools

    syms = ["JPM", "KO", "XOM", "PG"]
    pairs = {(a, b): 0.10 for a, b in itertools.combinations(syms, 2)}
    assert correlation_clusters(syms, _sym_matrix(pairs, syms)) == [["JPM", "KO", "PG", "XOM"]]


def test_themes_survive_a_realistic_book_and_do_not_flip_under_noise():
    """The case the old 0.7 cutoff existed for, and the stability measurement.

    An 11-name book: three real themes (within-theme corr 0.78-0.90), ordinary
    equity-beta correlation between them (0.35-0.50), and three loners. The
    partition must come out 3/3/2 AND must not move when every correlation is
    nudged by up to 0.03 — the sampling wobble between two sessions.

    MEASURED 2026-09-30: 0 flips in 400 draws as shipped. The earlier version
    of the cut, which also bracketed the edge lengths at d = 0, returned NO
    clusters on this same book and flipped in 11.8% of the same 400 draws.
    """
    import itertools
    import random

    syms = ["OKLO", "CEG", "VST", "NVDA", "AVGO", "AMD", "KO", "PG", "XOM", "JPM", "GLD"]
    theme = {"OKLO": "a", "CEG": "a", "VST": "a", "NVDA": "b", "AVGO": "b", "AMD": "b", "KO": "c", "PG": "c"}
    rnd = random.Random(3)
    base = {}
    for a, b in itertools.combinations(syms, 2):
        ta, tb = theme.get(a), theme.get(b)
        if ta and ta == tb:
            base[(a, b)] = rnd.uniform(0.78, 0.90)
        elif ta and tb:
            base[(a, b)] = rnd.uniform(0.35, 0.50)
        else:
            base[(a, b)] = rnd.uniform(0.05, 0.35)
    expected = [["AMD", "AVGO", "NVDA"], ["CEG", "OKLO", "VST"], ["KO", "PG"]]
    assert correlation_clusters(syms, _sym_matrix(base, syms)) == expected

    noise = random.Random(11)
    for _ in range(200):
        jittered = {k: v + noise.uniform(-0.03, 0.03) for k, v in base.items()}
        assert correlation_clusters(syms, _sym_matrix(jittered, syms)) == expected


def test_uniformly_related_book_is_one_cluster():
    import itertools

    syms = ["NVDA", "AVGO", "AMD", "MU"]
    pairs = {(a, b): 0.85 for a, b in itertools.combinations(syms, 2)}
    assert correlation_clusters(syms, _sym_matrix(pairs, syms)) == [["AMD", "AVGO", "MU", "NVDA"]]


def test_clustering_stays_transitive():
    """A~B and B~C puts A, B and C in one bet even though A and C are apart."""
    matrix = {
        "A": {"B": 0.90, "C": 0.10},
        "B": {"A": 0.90, "C": 0.90},
        "C": {"A": 0.10, "B": 0.90},
    }
    assert correlation_clusters(["A", "B", "C"], matrix) == [["A", "B", "C"]]


def test_cluster_peers_excludes_the_symbol_itself():
    import itertools

    syms = ["OKLO", "CEG", "JPM", "KO"]
    theme = {"OKLO", "CEG"}
    pairs = {(a, b): (0.92 if a in theme and b in theme else 0.10) for a, b in itertools.combinations(syms, 2)}
    peers = cluster_peers("OKLO", ["CEG", "JPM", "KO"], _sym_matrix(pairs, syms))
    assert peers == ["CEG"]


def test_cluster_peers_empty_without_a_matrix():
    assert cluster_peers("NVDA", ["AVGO"], {}) == []
