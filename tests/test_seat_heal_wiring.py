"""The heal DISPATCHER must actually reach an expired news seat.

The defect this file exists to prevent from recurring, in one paragraph.
PR #511 (2026-09-18 10:11 ET) made the intraday news carry-forward hand the
wire text it had already fetched to `ctx.heal_news_text`, so an `expired`
news seat could be re-asked with data the tick had already paid for. PR #535
(2026-09-18 17:24 ET, same day) moved `expired` out of `CATEGORY_LOST` into
its own `CATEGORY_EXPIRED` — correct on its own terms, because the desk
HOLDS an answer and holding an older answer is not the same as having none.
But `_heal_lost_research_seats` selected its work with
`STATUS_CATEGORY.get(status) == CATEGORY_LOST`, so from 17:24 that day the
refresh was unreachable. It stayed unreachable for five days: zero rows
matching `seat heal` in the production log, while the owner received 14
`news=expired` data-quality alerts over 2026-09-21/22.

`tests/test_intra_news_heal_feed.py` passed throughout, because it calls
`_try_one_paid_research_retry` DIRECTLY. The function worked. Nothing called
it. Every test here therefore goes through `_heal_lost_research_seats`, and
`test_every_status_that_can_carry_a_heal_input_is_healable` fails outright if
a future re-categorisation orphans the path the same way again.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import evidence_gate
from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext


# ── harness ──────────────────────────────────────────────────────────────

def _stored_report() -> dict:
    from src.models import MacroNarrative, NewsIntelligenceReport

    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-09-17", era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        state_changes=[],
        stock_news={"AAPL": [{
            "headline": "Apple beats",
            "sentiment": "bullish",
            "conviction": "high",
            "impact_summary": "beat",
        }]},
        pm_briefing="ok", market_sentiment="bullish", confidence="medium",
    ).model_dump()


class _NewsStore:
    def __init__(self, report=None, raw_headlines=None):
        self._report = report
        self._raw = raw_headlines or []

    def load_daily_report(self, session=None):
        return self._report

    def load_raw_headlines(self, session_date=None):
        return list(self._raw)


class _Provider:
    """Wire provider. `titles=None` models a fetch that raised."""

    def __init__(self, titles):
        self.titles = titles

    def fetch_news(self, symbols=None):
        if self.titles is None:
            raise RuntimeError("wire fetch failed")
        return [SimpleNamespace(title=t, summary="") for t in self.titles], None

    def format_for_prompt(self, items, max_items=50):
        if not items:
            return ""
        return "\n".join(f"- {i.title}" for i in items[:max_items])


class _Analyst:
    """Stands in for news_analyst / macro_analyst. Records what it was handed."""

    def __init__(self, *, outcome="ok"):
        self.seen = None
        self.calls = 0
        self.outcome = outcome

    def analyze(self, payload, *args, **kwargs):
        self.calls += 1
        self.seen = payload
        if self.outcome == "raise":
            raise RuntimeError("provider exploded on the re-ask")
        if self.outcome == "none":
            return None, None
        return SimpleNamespace(model_dump=lambda: {"pm_briefing": "re-asked"}), None


class _DayLedger:
    """Stands in for Database's durable per-ET-day paid-heal counter."""

    def __init__(self, counts=None):
        self.counts = dict(counts or {})

    def count_paid_seat_heals_today(self, seat):
        return int(self.counts.get(seat, 0))


def _bind(obj):
    for name in (
        "_carry_forward_news",
        "_news_has_newer_material_wire",
        "_watched_research_symbols",
        "_peek_news_headlines",
        "_peeked_news_wire_text",
        "_try_one_paid_research_retry",
        "_heal_lost_research_seats",
    ):
        fn = getattr(TradingPipeline, name, None)
        if fn is not None:
            setattr(obj, name, fn.__get__(obj))
    return obj


def _pipeline(titles, *, analyst=None, require=None, db=None, recorded=None):
    """A pipeline stub wired the way the intraday tick wires the real one."""
    obj = SimpleNamespace(
        news_store=_NewsStore(_stored_report(), raw_headlines=[{"title": "Apple beats"}]),
        news_provider=_Provider(titles),
        config=SimpleNamespace(news=SimpleNamespace(max_prompt_items=50)),
        news_analyst=analyst if analyst is not None else _Analyst(),
        macro_analyst=_Analyst(),
        db=db,
        _require_paid_analysis=require if require is not None else (lambda name: None),
    )
    log = recorded if recorded is not None else []
    obj._record_heal = lambda ctx, result, alert=False: log.append((result, alert))
    obj.recorded = log
    return _bind(obj)


def _ctx() -> RunContext:
    return RunContext(run_id="t1", session="intra_check")


#: The headline set that makes the remembered report superseded. "AAPL" is
#: named so the peek keeps it (an unnamed general-wire title is dropped).
_MOVED_WIRE = ["Apple beats", "AAPL guidance cut after close"]


def _expired_news_tick(**kw):
    """Run the carry-forward exactly as `_run_intraday_scan` does, and return
    (pipeline, ctx) with a genuinely expired news seat plus its wire text."""
    ctx = _ctx()
    obj = _pipeline(kw.pop("titles", _MOVED_WIRE), **kw)
    carried = obj._carry_forward_news(ctx)
    assert carried.status == "expired", "fixture did not produce an expired seat"
    ctx.data_status = {"tech": "ok", "news": carried.status}
    ctx.news_intel = carried.payload
    return obj, ctx


# ── the defect itself ────────────────────────────────────────────────────

def test_the_dispatcher_reaches_an_expired_news_seat_and_heals_it():
    """THE regression test. Fails on origin/main as of 2026-09-23."""
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst)

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1, "the heal dispatcher never re-asked the seat"
    assert "AAPL guidance cut after close" in (analyst.seen or "")
    assert ctx.data_status["news"] == "ok"
    assert ctx.news_intel is not None
    # A success is a durable log row, never an owner page.
    from src.seat_heal import HEAL_PAID_RETRY
    assert [alert for _r, alert in obj.recorded] == [False]
    assert obj.recorded[0][0].outcome == HEAL_PAID_RETRY
    assert obj.recorded[0][0].usable is True


def test_every_status_that_can_carry_a_heal_input_is_healable():
    """The anti-orphan guard.

    `_carry_forward_news` sets `ctx.heal_news_text` only on a status the
    reuse verdict called unusable. Whatever that status is, the dispatcher
    must be willing to act on its category — otherwise the desk pays to
    fetch wire text and then throws it away, which is exactly what happened
    between 2026-09-18 and 2026-09-23. Re-categorising a status is allowed;
    doing it without adding the new category here is not.
    """
    ctx = _ctx()
    obj = _pipeline(_MOVED_WIRE)
    carried = obj._carry_forward_news(ctx)

    assert ctx.heal_news_text, "fixture did not reach the heal-feed branch"
    assert evidence_gate.STATUS_CATEGORY[carried.status] in (
        evidence_gate.HEALABLE_CATEGORIES
    ), (
        f"status {carried.status!r} feeds ctx.heal_news_text but its category "
        f"is not in HEALABLE_CATEGORIES — the heal path is orphaned again"
    )


# ── the class of defect: every branch of the heal ────────────────────────

def test_a_failed_re_ask_leaves_the_seat_expired_and_pages_honestly():
    analyst = _Analyst(outcome="raise")
    obj, ctx = _expired_news_tick(analyst=analyst)

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1
    # Nothing was fabricated: the seat is still expired.
    assert ctx.data_status["news"] == "expired"
    alerts = [r for r, alert in obj.recorded if alert]
    assert len(alerts) == 1, "a paid re-ask that failed must page the owner"
    # ...but it must NOT claim a trade was withheld. The desk still holds the
    # morning's research for this seat (item 133's class of defect).
    body = alerts[0].owner_consequence
    assert "still holds" in body and "No trade was withheld" in body
    assert alerts[0].details["was_expired"] is True


def test_a_re_ask_that_returns_nothing_is_not_treated_as_an_answer():
    analyst = _Analyst(outcome="none")
    obj, ctx = _expired_news_tick(analyst=analyst)

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1
    assert ctx.data_status["news"] == "expired"
    assert ctx.news_intel is None


def test_absent_wire_data_means_no_re_ask_no_spend_and_no_noise():
    """An unchanged wire is not expired at all — nothing to heal, nothing paid."""
    ctx = _ctx()
    analyst = _Analyst()
    obj = _pipeline(["Apple beats"], analyst=analyst)  # same headline as stored

    carried = obj._carry_forward_news(ctx)
    assert carried.status != "expired"
    ctx.data_status = {"tech": "ok", "news": carried.status}

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 0
    assert ctx.heal_news_text is None
    assert obj.recorded == []


def test_an_expired_seat_with_no_heal_wired_is_not_re_asked_or_recorded():
    """Insider/earnings expire routinely and have no heal by design (#535).

    Before this change they were invisible to the dispatcher. They must stay
    silent rather than start writing a failure row on every tick.
    """
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst)
    ctx.data_status = {"tech": "ok", "smart_money": "expired", "earnings": "expired"}

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 0
    assert obj.recorded == []


def test_a_lost_seat_still_heals_exactly_as_before():
    """The change must widen the dispatcher, not redirect it."""
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst)
    # Same tick, but the seat is LOST rather than expired. The wire text is
    # present, so the pre-existing lost-seat heal must still fire.
    ctx.data_status = {"tech": "ok", "news": "parse_error"}

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1
    assert ctx.data_status["news"] == "ok"


def test_a_lost_seat_with_no_inputs_still_records_without_paying():
    obj, ctx = _expired_news_tick()
    ctx.heal_news_text = None
    ctx.data_status = {"tech": "ok", "news": "carry_forward_failed"}

    obj._heal_lost_research_seats(ctx)

    assert obj.news_analyst.calls == 0
    # A lost seat is recorded even when no retry was possible — that row is
    # the forensic trail for an absent answer. It is not a page.
    assert [alert for _r, alert in obj.recorded] == [False]


def test_an_empty_store_is_still_the_evidence_gates_page_not_a_second_one():
    obj, ctx = _expired_news_tick()
    ctx.data_status = {"tech": "ok", "news": "carry_forward_empty"}

    obj._heal_lost_research_seats(ctx)

    assert obj.news_analyst.calls == 0
    assert obj.recorded == []


# ── cost containment ─────────────────────────────────────────────────────

def test_an_expired_seat_cannot_be_healed_twice_in_one_session():
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst)

    obj._heal_lost_research_seats(ctx)
    assert analyst.calls == 1
    # Force the seat back to expired the way a second carry-forward would,
    # with the wire still moved, and run the dispatcher again.
    ctx.data_status = {"tech": "ok", "news": "expired"}
    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1, "the one-paid-retry cap did not hold"
    assert ctx.heal_paid_retries.get("news") == 1


def test_the_day_cap_stops_a_fresh_context_buying_the_seat_again():
    """intra_check fires every 30 minutes and each tick gets a NEW RunContext.

    The in-context counter therefore resets fourteen times a day, and an
    expired seat is still expired on the next tick. Without a durable cap,
    reconnecting the heal would have bought the news seat back fourteen
    times and called it "one retry".
    """
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(
        analyst=analyst, db=_DayLedger({"news": 1}),
    )
    assert ctx.heal_paid_retries in (None, {}), "fresh tick, fresh counter"

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 0, "the durable day cap did not hold"
    assert ctx.data_status["news"] == "expired"


def test_the_day_cap_allows_the_first_heal_of_the_day():
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=_DayLedger({"news": 0}))

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1


def test_a_broken_day_cap_read_never_blocks_a_legitimate_refresh():
    class _Broken:
        def count_paid_seat_heals_today(self, seat):
            raise RuntimeError("db locked")

    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=_Broken())

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1, "a forensic-store hiccup must not block the heal"


def test_the_day_cap_counts_the_rows_the_heal_path_actually_writes():
    """The cap and the row that feeds it must be tested TOGETHER.

    A stubbed counter proves nothing: if `HealResult.to_evidence()` stops
    emitting `paid_retry`, or the `kind` string changes, the real counter
    silently returns 0 forever and every stub-based test still passes. So
    this one writes real heal rows through the real persistence call and
    reads them back through the real SQL.
    """
    import json
    import tempfile
    from pathlib import Path
    from src.storage.db import Database
    from src.seat_heal import (
        HealResult, HEAL_CAP_BLOCKED, HEAL_FAILED, HEAL_PAID_RETRY,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "t.db"))
        db.initialize()
        assert db.count_paid_seat_heals_today("news") == 0

        def _write(result):
            db.insert_specialist_evidence(
                run_id="r1", agent_name="seat_heal", kind="seat_heal",
                scope="run",
                evidence_json=json.dumps(result.to_evidence(), sort_keys=True),
            )

        # A spend that happened.
        _write(HealResult(
            seat="news", outcome=HEAL_PAID_RETRY, reason="refreshed",
            paid_retry=True, usable=True,
        ))
        assert db.count_paid_seat_heals_today("news") == 1
        # A spend that happened and failed still counts — money left.
        _write(HealResult(
            seat="news", outcome=HEAL_FAILED, reason="raised", paid_retry=True,
        ))
        assert db.count_paid_seat_heals_today("news") == 2
        # A block is not a spend.
        _write(HealResult(
            seat="news", outcome=HEAL_CAP_BLOCKED, reason="cap", paid_retry=False,
        ))
        assert db.count_paid_seat_heals_today("news") == 2
        # Seats are counted separately.
        assert db.count_paid_seat_heals_today("macro") == 0
        _write(HealResult(
            seat="macro", outcome=HEAL_PAID_RETRY, reason="ok", paid_retry=True,
        ))
        assert db.count_paid_seat_heals_today("macro") == 1
        assert db.count_paid_seat_heals_today("news") == 2


def test_a_day_cap_refusal_leaves_a_durable_row_not_only_a_log_line():
    """Declining to spend is a decision about money, so it is recorded."""
    from src.seat_heal import HEAL_DAY_CAP
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=_DayLedger({"news": 1}))

    obj._heal_lost_research_seats(ctx)

    rows = [r for r, _alert in obj.recorded if r.outcome == HEAL_DAY_CAP]
    assert len(rows) == 1
    assert rows[0].details["spent_today"] == 1
    assert rows[0].paid_retry is False
    # The cap doing its job is not an incident.
    assert not any(alert for _r, alert in obj.recorded)


def test_the_spend_cap_alert_does_not_call_an_expired_seat_lost_or_empty():
    """The cap-blocked alert branch used to return before reading
    owner_consequence, so it told the owner the desk could not replace a
    "lost or empty" seat when the desk was holding that seat's research."""
    from src.seat_heal import HealResult, HEAL_CAP_BLOCKED, heal_failure_alert_text

    expired = HealResult(
        seat="news", outcome=HEAL_CAP_BLOCKED, reason="session cap bound",
        owner_consequence=(
            "The desk still holds this seat's earlier answer and will decide "
            "on it. No trade was withheld for this."
        ),
    )
    body = heal_failure_alert_text(expired, cap_blocked=True)
    assert "lost or empty" not in body
    assert "still holds" in body and "No trade was withheld" in body

    # A genuinely lost seat keeps the original wording.
    lost = HealResult(seat="macro", outcome=HEAL_CAP_BLOCKED, reason="cap bound")
    lost_body = heal_failure_alert_text(lost, cap_blocked=True)
    assert "lost or empty" in lost_body
    assert "will not decide on this seat" in lost_body


def test_the_re_ask_is_not_handed_morning_guidance_on_an_afternoon_tick():
    """A 14:00 re-ask told to "treat today as a fresh book" is mislabelled —
    the same defect audit round 2 #24 fixed for the close session."""
    seen = {}

    class _SessionAnalyst(_Analyst):
        def analyze(self, payload, *args, **kwargs):
            seen.update(kwargs)
            return super().analyze(payload, *args, **kwargs)

    analyst = _SessionAnalyst()
    obj, ctx = _expired_news_tick(analyst=analyst)

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1
    assert seen.get("session") == "intra_check"

    # ...and passing the session must not be cosmetic. `_SESSION_GUIDANCE`
    # falls back to MORNING for any key it does not hold, so an absent
    # `intra_check` entry would have left the prompt exactly as wrong as
    # before while the assertion above passed.
    from src.agents.news_analyst import NewsAnalystAgent
    guidance = NewsAnalystAgent._SESSION_GUIDANCE
    assert "intra_check" in guidance, (
        "intra_check has no session guidance, so it silently falls back to "
        "MORNING — the re-ask is told to treat an afternoon tick as a fresh "
        "book. Same defect as audit round 2 #24."
    )
    assert guidance["intra_check"] != guidance["morning"]
    assert "fresh book" not in guidance["intra_check"].lower()


def test_a_suspended_cost_circuit_blocks_the_heal_and_pages_the_owner():
    analyst = _Analyst()

    def _require(name):
        raise PaidAnalysisSuspended("session spend cap bound")

    obj, ctx = _expired_news_tick(analyst=analyst, require=_require)

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 0, "the heal spent money past a bound cost cap"
    assert ctx.data_status["news"] == "expired"
    from src.seat_heal import HEAL_CAP_BLOCKED
    outcomes = [r.outcome for r, _alert in obj.recorded]
    assert HEAL_CAP_BLOCKED in outcomes
    assert any(alert for _r, alert in obj.recorded)
    # A block is not a spend, so it must not consume the day's allowance.
    assert (ctx.heal_paid_retries or {}).get("news") in (None, 0)


# ── the category split the fix must NOT undo ─────────────────────────────

def test_expired_is_still_its_own_category_and_still_not_lost():
    assert evidence_gate.STATUS_CATEGORY["expired"] == evidence_gate.CATEGORY_EXPIRED
    assert evidence_gate.CATEGORY_EXPIRED != evidence_gate.CATEGORY_LOST


def test_expired_still_does_not_halt_the_desk_and_still_counts_as_degraded():
    v = evidence_gate.evaluate({"news": "expired", "tech": "ok"})
    assert v.skip is False
    assert evidence_gate.counts_as_degraded("expired") is True
    # Even on the one blocking seat, expired is not a halt.
    assert evidence_gate.evaluate({"tech": "expired"}).skip is False


def test_expired_is_still_carried_not_fresh_in_the_disclosure():
    assert evidence_gate.STATUS_FRESHNESS["expired"] == evidence_gate.FRESHNESS_CARRIED


@pytest.mark.parametrize("status,category", [
    ("ok", evidence_gate.CATEGORY_REPORTED),
    ("carried_from_morning", evidence_gate.CATEGORY_REPORTED),
    ("remembered", evidence_gate.CATEGORY_REPORTED),
    ("empty", evidence_gate.CATEGORY_NOTHING_TO_REPORT),
    ("release_overdue", evidence_gate.CATEGORY_NOTHING_TO_REPORT),
    ("chose_not_to_refetch", evidence_gate.CATEGORY_NOTHING_TO_REPORT),
    ("not_run_intraday", evidence_gate.CATEGORY_NOTHING_TO_REPORT),
    ("failed", evidence_gate.CATEGORY_LOST),
    ("parse_error", evidence_gate.CATEGORY_LOST),
    ("provider_error", evidence_gate.CATEGORY_LOST),
    ("truncated", evidence_gate.CATEGORY_LOST),
    ("content_missing", evidence_gate.CATEGORY_LOST),
    ("carry_forward_empty", evidence_gate.CATEGORY_LOST),
    ("carry_forward_failed", evidence_gate.CATEGORY_LOST),
    ("expired", evidence_gate.CATEGORY_EXPIRED),
])
def test_no_status_changed_category(status, category):
    assert evidence_gate.STATUS_CATEGORY[status] == category


def test_healable_categories_is_only_read_by_the_heal_path():
    """It decides whether to go and look again. It must decide nothing else.

    If a future change makes the evidence gate itself consult this set, the
    "expired is not lost" mandate becomes reachable from a constant whose
    stated purpose is cost, not consequence.
    """
    import inspect
    from src import evidence_gate as gate
    source = inspect.getsource(gate)
    # The whole module, not just the text after the definition — a read
    # placed ABOVE the definition line would otherwise pass.
    uses = source.count("HEALABLE_CATEGORIES")
    assert uses == 1, (
        f"evidence_gate mentions HEALABLE_CATEGORIES {uses} times; it is "
        f"defined once and must be read nowhere in this module. It is a "
        f"refresh hint about cost, not a trading consequence."
    )
    # And no other module may read it into a verdict either: the only
    # readers anywhere are the heal dispatcher and this file.
    import subprocess
    from pathlib import Path
    root = Path(gate.__file__).resolve().parent.parent
    hits = subprocess.run(
        ["grep", "-rl", "--include=*.py", "HEALABLE_CATEGORIES", "src", "tests"],
        cwd=root, capture_output=True, text=True,
    ).stdout.split()
    assert sorted(hits) == [
        "src/evidence_gate.py", "src/pipeline.py",
        "tests/test_seat_heal_wiring.py",
    ], f"a new module reads HEALABLE_CATEGORIES: {hits}"
