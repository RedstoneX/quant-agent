"""The symbol->sector map is RUN-SCOPED: an early exit cannot leak it forward.

The map used to live on the long-lived pipeline instance
(`_last_symbol_sectors`), written only at the END of the projected-portfolio
preview. Every path that returned before that write — an empty book with no
BUY-rated candidate, a non-positive total value, or any earlier stage abort —
left the PREVIOUS run's sectors in place, and the decision stage then read
them as if they were this run's. Sector exposure feeds concentration limits,
so that is a money path.

The fix is structural rather than a cleanup call: the map is a field on
`RunContext`, which is built fresh for every session. There is no reset to
forget and no exit path that can carry it, because the next run never sees
the previous run's context at all.
"""
from types import SimpleNamespace

from src.models import Position
from src.pipeline_context import RunContext
from src.prompt_facts.projected import PromptProjected
from tests.sector_run_helper import tech_buy_analyses as _tech_buy_analyses


def _analyses():
    return _tech_buy_analyses()


def _positions():
    return [
        Position(symbol="MSFT", qty=10, avg_entry=400, current_price=400,
                 market_value=3000, unrealized_pnl=0, sector="Technology"),
    ]


def _part():
    return PromptProjected(portfolio_constructor=None, risk_engine=None)


def test_a_fresh_run_has_never_recorded_sectors_rather_than_empty_ones():
    """`None` (never exercised) must stay distinguishable from `{}` (ran, found none)."""
    ctx = RunContext.start("morning")
    assert ctx.symbol_sectors is None
    ctx.symbol_sectors = {}
    assert ctx.symbol_sectors is not None and ctx.symbol_sectors == {}


def test_an_early_exit_cannot_leak_the_previous_runs_sectors():
    """Run one records sectors; run two exits early and must see nothing of run one."""
    host = SimpleNamespace()
    part = _part()
    first = RunContext.start("morning")
    part._build_projected_portfolio(_positions(), _analyses(), 10000.0, run=first)
    assert first.symbol_sectors, "run one should have recorded the sectors it resolved"

    second = RunContext.start("midday")
    # The `total_value <= 0` early return — one of several paths that leave
    # the preview before it ever resolves a sector.
    assert part._build_projected_portfolio(_positions(), _analyses(), 0.0, run=second) == ""
    assert second.symbol_sectors is None, "an early-exit run must record nothing, not the last run's map"

    # The empty-book early return is the same story.
    third = RunContext.start("close")
    assert part._build_projected_portfolio([], [], 10000.0, run=third) == ""
    assert third.symbol_sectors is None

    # And nothing was ever parked on a long-lived object for a later run to
    # find: this is the attribute the leaking version wrote.
    assert not getattr(host, "_last_symbol_sectors", None)
    assert not getattr(part, "_last_symbol_sectors", None)


def test_nothing_outside_the_run_context_stores_the_sector_map():
    """No pipeline-instance sector cache survives anywhere in src."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    offenders = [
        str(f) for f in root.rglob("*.py")
        if "_last_symbol_sectors" in f.read_text()
    ]
    assert not offenders, f"instance-level sector cache is back in: {offenders}"
