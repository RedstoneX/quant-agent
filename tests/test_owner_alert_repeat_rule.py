"""docs/WORK.md item 188 — ONE rule for every owner page that can recur.

THE DEFECT, measured in production, in both directions at once:

  * `_alert_owner_no_stop` had no dedup of any kind. The coverage sweep runs
    every thirty minutes, so one position that stayed naked all session sent
    the identical page about a dozen times. The same shape is on the record
    for a sibling reconciliation page: `report_stop_level_mismatches` sent
    the byte-identical COP/EQNR message ten times on 2026-09-16 between
    13:30 and 17:00 UTC [measured, `quant_agent.log.3`, read-only], twice of
    them 90 ms apart from two processes racing.
  * `_alert_owner_delever_incomplete` had the opposite defect: it paged only
    on the TRANSITION into still-over-ceiling, so a book sitting over its
    limit on a second consecutive morning paged nobody.

THE RULE, one for the class, exercised by the four tests below:

  claimed per (condition, subject, ET calendar day);
  starts        -> pages;
  unchanged     -> silent inside that day;
  still true    -> pages again on the next trading day;
  clears+returns-> pages again immediately.

The boundary is the ET calendar date the desk already files alerts under
(`coverage_watchdog.repair_failure_alert_day`). No interval, repeat count or
backoff was invented — there is no number in this rule to pick.

WHAT IS DEDUPED IS THE MESSAGE ONLY. `test_the_record_is_written_on_every_
occurrence_even_when_the_page_is_suppressed` is the load-bearing negative:
a suppressed page must never make an unprotected position vanish from the
gap rows the session banner and the dashboard read.

Nothing here touches the network, a real broker, or a real chat.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import coverage_watchdog as cw

_DAY_ONE = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)   # 10:30 ET
_DAY_ONE_LATER = datetime(2026, 9, 24, 19, 30, tzinfo=timezone.utc)  # 15:30 ET
_DAY_TWO = datetime(2026, 9, 25, 14, 30, tzinfo=timezone.utc)   # next day


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "coverage_heartbeat.json"
    monkeypatch.setattr(cw, "STATE_PATH", path)
    return path


# ===========================================================================
# 1-4. the rule itself, on the shared primitive every caller uses
# ===========================================================================

def test_a_condition_that_starts_pages(state_path):
    assert cw.claim_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["vlo"], now=_DAY_ONE,
    ) == ["VLO"]


def test_the_same_condition_unchanged_inside_the_day_does_not_page_again(
    state_path,
):
    """The 30-minute sweep tick. Same name, same day, five hours later."""
    cw.claim_owner_alert(cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE)
    assert cw.claim_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE_LATER,
    ) == []


def test_the_same_condition_still_true_across_the_boundary_pages_again(
    state_path,
):
    """The half the de-lever page was missing: a book still over its ceiling
    on the next trading day is told again."""
    cw.claim_owner_alert(cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE)
    assert cw.claim_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_TWO,
    ) == ["VLO"]


def test_a_condition_that_clears_and_returns_pages_again_the_same_day(
    state_path,
):
    """Release is what makes this a dedup and not a mute."""
    cw.claim_owner_alert(cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE)
    assert cw.release_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE,
    ) == ["VLO"]
    assert cw.claim_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE_LATER,
    ) == ["VLO"]


# ===========================================================================
# 5-7. the traps the five hand-written predecessors each had to learn
# ===========================================================================

def test_one_subject_does_not_swallow_another(state_path):
    """A 10:00 naked position must never silence a DIFFERENT name at 14:00."""
    cw.claim_owner_alert(cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE)
    assert cw.claim_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["AAPL", "VLO"], now=_DAY_ONE_LATER,
    ) == ["AAPL"]


def test_case_folding_so_a_mixed_case_symbol_cannot_re_page(state_path):
    """The dedup silently not applying is how `claim_unreadable_stop_alert`
    would have paged on every tick for a broker that spells it `Vlo`."""
    cw.claim_owner_alert(cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE)
    assert cw.claim_owner_alert(
        cw.NO_STOP_CLAIM_KEY, [" vlo "], now=_DAY_ONE_LATER,
    ) == []


def test_two_conditions_cannot_silence_each_other(state_path):
    """Separate state keys, on purpose: a de-lever page and a naked position
    are two findings and the owner gets both."""
    cw.claim_owner_alert(
        cw.DELEVER_INCOMPLETE_CLAIM_KEY, [cw.ACCOUNT_SUBJECT], now=_DAY_ONE,
    )
    assert cw.claim_owner_alert(
        cw.FORCE_DELEVER_INCOMPLETE_CLAIM_KEY, [cw.ACCOUNT_SUBJECT],
        now=_DAY_ONE,
    ) == [cw.ACCOUNT_SUBJECT]


def test_releasing_something_never_claimed_is_a_no_op(state_path):
    assert cw.release_owner_alert(
        cw.NO_STOP_CLAIM_KEY, ["VLO"], now=_DAY_ONE,
    ) == []


# ===========================================================================
# 8-10. the two call sites, end to end through the real alert functions
# ===========================================================================

def _naked(sym="VLO"):
    return [{"symbol": sym, "held_qty": 1.66, "covered_qty": 0.0,
             "coverage": "none", "repair_refusal": "broker rejected"}]


def test_the_naked_position_page_repeats_only_across_the_day_boundary(
    state_path, monkeypatch,
):
    """`_alert_owner_no_stop` is the path that had NO dedup at all."""
    from src.pipeline import TradingPipeline

    sent: list[str] = []
    monkeypatch.setattr(
        "src.notifier.send_owner_alert",
        lambda text, symbols=None, **_kw: sent.append(text) or True,
    )

    days = iter([_DAY_ONE, _DAY_ONE_LATER, _DAY_TWO])
    monkeypatch.setattr(cw, "_utc_now", lambda: next(days))

    TradingPipeline._alert_owner_no_stop(_naked())
    TradingPipeline._alert_owner_no_stop(_naked())
    TradingPipeline._alert_owner_no_stop(_naked())

    assert len(sent) == 2, sent
    assert all("NO STOP AT ALL" in t for t in sent)


def test_a_covered_pass_releases_the_naked_claim_so_a_relapse_pages(
    state_path, monkeypatch,
):
    from src.pipeline import TradingPipeline

    sent: list[str] = []
    monkeypatch.setattr(
        "src.notifier.send_owner_alert",
        lambda text, symbols=None, **_kw: sent.append(text) or True,
    )
    monkeypatch.setattr(cw, "_utc_now", lambda: _DAY_ONE)

    TradingPipeline._alert_owner_no_stop(_naked())
    # the repair worked: this pass sees VLO covered
    TradingPipeline._alert_owner_no_stop([], cleared=["VLO"])
    # and it goes naked again the same day
    TradingPipeline._alert_owner_no_stop(_naked())

    assert len(sent) == 2, sent


def test_the_record_is_written_on_every_occurrence_even_when_suppressed(
    state_path, monkeypatch, caplog,
):
    """THE LOAD-BEARING NEGATIVE. Suppressing the second page must not make
    the unprotected position disappear — the log still carries it."""
    from src.pipeline import TradingPipeline

    monkeypatch.setattr(
        "src.notifier.send_owner_alert", lambda *a, **k: True,
    )
    monkeypatch.setattr(cw, "_utc_now", lambda: _DAY_ONE)

    TradingPipeline._alert_owner_no_stop(_naked())
    caplog.clear()
    with caplog.at_level("WARNING"):
        TradingPipeline._alert_owner_no_stop(_naked())
    assert any(
        "still true" in r.message and "VLO" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
