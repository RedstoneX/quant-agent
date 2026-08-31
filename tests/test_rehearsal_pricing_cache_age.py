"""The rehearsal harness's pricing-cache age is an input, not an inheritance.

THE DEFECT (recorded as open defect (g)). `ops/rehearsal/isolation.py` copies
production's `data/` tree with `cp -a`, timestamps and all, and
`src/cost_table.py` treats the OpenRouter cache's mtime as its freshness
signal. So a rehearsal inherited whatever age production's copy happened to
have. Nothing refreshed that file on a schedule (open defect (b), fixed in the
same change as this), so over a weekend it aged past its 24h freshness window
and then past the 24h grace on top — at which point EVERY rehearsal fails
closed at the pricing preflight, before a single model call.

That is fatal specifically for `tests/test_rehearsal_reproduces_cost_ceiling.py`,
which exists to prove the 2026-08-28 spending-limit failure still reproduces
on demand. A test that can fail for a reason unrelated to what it tests is a
test nobody can read an answer out of.

WHAT IS NOT BEING FIXED HERE: the fail-closed behaviour. A rehearsal that asks
for a stale cache still gets the suspension, and
`test_staleness_is_still_reachable_when_a_test_asks_for_it` below is the proof.
The change is that staleness became something a rehearsal declares rather than
something it catches from the calendar.

Runs anywhere. Nothing here reads or writes production — the sandboxes are
`tmp_path` trees and the "ancient" cache is one this test wrote itself.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# Comfortably past 24h fresh + 24h grace: the region where the circuit fails
# closed outright. Chosen to be an age a real production file HAS reached
# (the live cache was ~44h old on the morning of 2026-08-30) with margin.
ANCIENT_AGE_HOURS = 400.0

GRACE_HOURS = 24.0  # config/settings.yaml: openrouter_pricing_grace_period_hours


@pytest.fixture
def _restore_pricing():
    """Snapshot + restore `cost_table`'s mutable globals, so a refresh call
    made here cannot leak rates into the rest of the session."""
    from src import cost_table

    pricing_snap = {k: dict(v) for k, v in cost_table.PRICING.items()}
    unknown_snap = set(cost_table._UNKNOWN_MODELS)
    yield
    cost_table.PRICING.clear()
    cost_table.PRICING.update(pricing_snap)
    cost_table._UNKNOWN_MODELS.clear()
    cost_table._UNKNOWN_MODELS.update(unknown_snap)


def _sandbox(root: Path, *, cache_age_hours: float | None = ANCIENT_AGE_HOURS):
    """A `Sandbox` over a scratch tree holding a valid but aged pricing cache.

    The real dataclass, constructed directly rather than through `prepare` —
    `prepare` snapshots the production database, which this test has no
    business touching and no need for.
    """
    from ops.rehearsal.isolation import Sandbox
    from src import cost_table

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if cache_age_hours is not None:
        # Price exactly the accepted models, read from the module rather than
        # hardcoded, so this test keeps testing staleness and not a config
        # change that added a model.
        rates = {
            model: dict(value)
            for model, value in cost_table._PRICING_OPENROUTER.items()
        }
        cache = data_dir / "openrouter_pricing_cache.json"
        cache.write_text(json.dumps(rates))
        stamp = time.time() - cache_age_hours * 3600
        os.utime(cache, (stamp, stamp))
    return Sandbox(
        root=root,
        db_path=data_dir / "quant_agent.db",
        data_dir=data_dir,
        source_db=root / "never-read.db",
    )


def _write_grace_config(sandbox, grace_hours: float = GRACE_HOURS) -> None:
    """The minimum `_pricing_grace_hours` needs to read out of the sandbox."""
    import yaml

    config_dir = sandbox.root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {"llm_cost_circuit": {
                "openrouter_pricing_grace_period_hours": grace_hours,
            }}
        )
    )


# ---------------------------------------------------------------------------
# The load-bearing one
# ---------------------------------------------------------------------------

def test_an_ancient_pricing_cache_no_longer_fails_a_rehearsal_closed(
    tmp_path, monkeypatch, _restore_pricing,
):
    """Defect (g), closed, proved against the real cost circuit.

    Both halves matter. The FIRST assertion reproduces the defect: with the
    sandbox carrying production's real (ancient) mtime and the network down —
    which is what a rehearsal always is, by construction — the mandatory
    pricing preflight returns False, and both production call sites turn that
    into `mark_unavailable`, i.e. the rehearsal reports a suspended session
    that has nothing to do with what it was rehearsing.

    The SECOND assertion is the fix: after the harness stamps its declared
    age on the sandbox copy, the very same offline call succeeds.

    Without the first assertion this test would keep passing if the harness
    change were reverted, because a cache that was never stale to begin with
    proves nothing.
    """
    from ops.rehearsal.runner import (
        DEFAULT_PRICING_CACHE_AGE_HOURS, apply_pricing_cache_age,
    )
    from src import cost_table

    sandbox = _sandbox(tmp_path / "sandbox")
    # A rehearsal cannot reach openrouter.ai — `no_network()` blocks the
    # socket. Same condition, without installing the whole wall.
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)
    monkeypatch.setattr(
        cost_table, "_OPENROUTER_CACHE_PATH",
        sandbox.data_dir / "openrouter_pricing_cache.json",
    )

    assert cost_table.refresh_openrouter_pricing(
        grace_period_hours=GRACE_HOURS, max_stale_multiplier=1.5,
    ) is False, (
        "the inherited-age condition this test reproduces no longer produces "
        "a fail-closed preflight, so the second half proves nothing"
    )

    note = apply_pricing_cache_age(sandbox, DEFAULT_PRICING_CACHE_AGE_HOURS)

    assert cost_table.refresh_openrouter_pricing(
        grace_period_hours=GRACE_HOURS, max_stale_multiplier=1.5,
    ) is True, (
        "a rehearsal is still inheriting the live pricing cache's real age — "
        "defect (g) has regressed"
    )
    assert "set to 1h by the harness" in note


def test_the_declared_age_is_fresh_and_lands_on_the_file(tmp_path):
    """The stamped age is the one asked for, and it is inside the freshness
    window — not merely 'newer than it was'."""
    from ops.rehearsal.runner import (
        DEFAULT_PRICING_CACHE_AGE_HOURS, apply_pricing_cache_age,
    )
    from src.cost_table import OPENROUTER_CACHE_FRESH_HOURS

    sandbox = _sandbox(tmp_path / "sandbox")
    cache = sandbox.data_dir / "openrouter_pricing_cache.json"
    before = (time.time() - cache.stat().st_mtime) / 3600
    assert before > OPENROUTER_CACHE_FRESH_HOURS  # the setup is what it claims

    apply_pricing_cache_age(sandbox, DEFAULT_PRICING_CACHE_AGE_HOURS)

    after = (time.time() - cache.stat().st_mtime) / 3600
    assert after == pytest.approx(DEFAULT_PRICING_CACHE_AGE_HOURS, abs=0.01)
    assert DEFAULT_PRICING_CACHE_AGE_HOURS < OPENROUTER_CACHE_FRESH_HOURS


# ---------------------------------------------------------------------------
# The safety property that must NOT have been traded away
# ---------------------------------------------------------------------------

def test_staleness_is_still_reachable_when_a_test_asks_for_it(
    tmp_path, monkeypatch, _restore_pricing,
):
    """Rehearsing the fail-closed path deliberately still works.

    This is the whole difference between fixing the coupling and weakening
    the circuit. An explicit age past 24h + grace must still produce exactly
    the refusal it produced before.
    """
    from ops.rehearsal.runner import _pricing_cache_note, apply_pricing_cache_age
    from src import cost_table

    sandbox = _sandbox(tmp_path / "sandbox", cache_age_hours=1.0)
    _write_grace_config(sandbox)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)
    monkeypatch.setattr(
        cost_table, "_OPENROUTER_CACHE_PATH",
        sandbox.data_dir / "openrouter_pricing_cache.json",
    )

    apply_pricing_cache_age(sandbox, 24 + GRACE_HOURS + 1)

    assert cost_table.refresh_openrouter_pricing(
        grace_period_hours=GRACE_HOURS, max_stale_multiplier=1.5,
    ) is False
    assert "suspend paid analysis" in _pricing_cache_note(sandbox)


def test_a_requested_age_inside_the_grace_band_still_reports_as_stale(tmp_path):
    """The middle band is not collapsed into either neighbour: a rehearsal
    that asks for 30h gets the grace-window note, not the fresh one and not
    the suspension one."""
    from ops.rehearsal.runner import _pricing_cache_note, apply_pricing_cache_age

    sandbox = _sandbox(tmp_path / "sandbox", cache_age_hours=1.0)
    _write_grace_config(sandbox)

    apply_pricing_cache_age(sandbox, 30.0)

    note = _pricing_cache_note(sandbox)
    assert "grace window" in note
    assert "widened reservation multiplier" in note


# ---------------------------------------------------------------------------
# It must never be able to reach the production file
# ---------------------------------------------------------------------------

def test_refuses_to_stamp_a_cache_outside_the_sandbox(tmp_path):
    """The production cache's mtime IS the live circuit's freshness signal.
    A harness able to write it could make production's pricing look current
    when it is not — faking a safety check on the live desk, which is worse
    than anything this change fixes."""
    from ops.rehearsal.isolation import Sandbox
    from ops.rehearsal.runner import apply_pricing_cache_age

    outside = tmp_path / "not-the-sandbox"
    (outside / "data").mkdir(parents=True)
    cache = outside / "data" / "openrouter_pricing_cache.json"
    cache.write_text("{}")
    ancient = time.time() - ANCIENT_AGE_HOURS * 3600
    os.utime(cache, (ancient, ancient))

    # A sandbox whose data_dir has been pointed outside its own root.
    sandbox = Sandbox(
        root=tmp_path / "sandbox",
        db_path=tmp_path / "sandbox" / "data" / "quant_agent.db",
        data_dir=outside / "data",
        source_db=tmp_path / "never-read.db",
    )
    (tmp_path / "sandbox").mkdir()

    with pytest.raises(ValueError, match="outside the rehearsal sandbox"):
        apply_pricing_cache_age(sandbox, 1.0)

    assert (time.time() - cache.stat().st_mtime) / 3600 > ANCIENT_AGE_HOURS - 1, (
        "the file outside the sandbox was touched"
    )


def test_inherit_leaves_the_mtime_exactly_as_copied(tmp_path):
    """`None` means the old behaviour, kept for the 'what would the desk do
    with the cache as it stands' question. It must not quietly stamp."""
    from ops.rehearsal.runner import apply_pricing_cache_age

    sandbox = _sandbox(tmp_path / "sandbox")
    cache = sandbox.data_dir / "openrouter_pricing_cache.json"
    before = cache.stat().st_mtime

    note = apply_pricing_cache_age(sandbox, None)

    assert cache.stat().st_mtime == before
    assert "inherited" in note


def test_a_missing_cache_is_left_missing(tmp_path):
    """Never fabricate the file. No cache means the circuit fails closed, and
    that is the correct answer to report — not one this harness invents a
    price list to avoid."""
    from ops.rehearsal.runner import _pricing_cache_note, apply_pricing_cache_age

    sandbox = _sandbox(tmp_path / "sandbox", cache_age_hours=None)

    assert apply_pricing_cache_age(sandbox, 1.0) is None
    assert not (sandbox.data_dir / "openrouter_pricing_cache.json").exists()
    assert "no OpenRouter pricing cache in the sandbox" in _pricing_cache_note(sandbox)


def test_negative_ages_are_rejected(tmp_path):
    """A cache dated in the future would read as fresh forever."""
    from ops.rehearsal.runner import apply_pricing_cache_age

    sandbox = _sandbox(tmp_path / "sandbox")
    with pytest.raises(ValueError, match="must not be negative"):
        apply_pricing_cache_age(sandbox, -1.0)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_run_rehearsal_defaults_to_the_declared_fresh_age():
    """The default is what protects every existing rehearsal caller,
    `tests/test_rehearsal_reproduces_cost_ceiling.py` included — none of them
    pass this argument."""
    import inspect

    from ops.rehearsal.runner import DEFAULT_PRICING_CACHE_AGE_HOURS, run_rehearsal
    from src.cost_table import OPENROUTER_CACHE_FRESH_HOURS

    default = inspect.signature(run_rehearsal).parameters[
        "pricing_cache_age_hours"
    ].default
    assert default == DEFAULT_PRICING_CACHE_AGE_HOURS
    assert 0 < default < OPENROUTER_CACHE_FRESH_HOURS


@pytest.mark.parametrize(
    "argv,expected",
    [
        ([], 1.0),
        (["--pricing-cache-age-hours", "72"], 72.0),
        (["--pricing-cache-age-hours", "inherit"], None),
    ],
)
def test_cli_exposes_the_age(argv, expected):
    """A staleness rehearsal has to be askable for from the command line, not
    only from Python."""
    from ops.rehearsal.run import _parse_pricing_age, build_parser

    args = build_parser().parse_args(
        ["--source-db", "/nonexistent.db", *argv]
    )
    assert _parse_pricing_age(args.pricing_cache_age_hours) == expected


def test_cli_rejects_nonsense_ages():
    from ops.rehearsal.run import _parse_pricing_age

    with pytest.raises(SystemExit):
        _parse_pricing_age("yesterday")
    with pytest.raises(SystemExit):
        _parse_pricing_age("-5")
