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
        self._raw = list(raw_headlines or [])
        #: Every `save_daily_report` this stub ever saw. It must stay EMPTY
        #: on a heal: `full_report.json` is walked ACROSS days by
        #: `recent_state_changes` and the missed-ops scans, so a heal
        #: writing there would push a baseline-less state change into a
        #: multi-week catalyst memory and delete the morning's from it.
        self.saved: list[tuple] = []

    def load_daily_report(self, session=None):
        return self._report

    def save_daily_report(self, report, session=None):
        self.saved.append((report, session))

    def load_raw_headlines(self, session_date=None):
        return list(self._raw)

    def append_raw_headlines(self, headlines):
        seen = {str((i or {}).get("title") or "").strip() for i in self._raw}
        added = 0
        for item in headlines or []:
            title = str((item or {}).get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            self._raw.append(item)
            added += 1
        return added


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

    def __init__(self, *, outcome="ok", call_result=None):
        self.seen = None
        self.calls = 0
        self.outcome = outcome
        self.call_result = call_result if call_result is not None else _Recorded()

    def analyze(self, payload, *args, **kwargs):
        self.calls += 1
        self.seen = payload
        if self.outcome == "raise":
            raise RuntimeError("provider exploded on the re-ask")
        if self.outcome == "none":
            return None, None
        return (
            SimpleNamespace(
                model_dump=lambda: {"pm_briefing": "re-asked"},
                model_dump_json=lambda: '{"pm_briefing": "re-asked"}',
            ),
            self.call_result,
        )


class _DayLedger:
    """Stands in for Database's durable per-ET-day paid-heal counter.

    Also captures the two rows a PAID heal now writes — `agent_logs` and
    `specialist_evidence` — because until 2026-09-23 it wrote neither, and
    every production paid heal's model, tokens, cost and answer were lost.
    """

    def __init__(self, counts=None):
        self.counts = dict(counts or {})
        self.agent_logs: list[dict] = []
        self.evidence: list[dict] = []

    def count_paid_seat_heals_today(self, seat):
        return int(self.counts.get(seat, 0))

    def insert_agent_log(self, **kw):
        self.agent_logs.append(kw)

    def insert_specialist_evidence(self, **kw):
        self.evidence.append(kw)

    def latest_news_analysis_today(self, trading_day=None):
        """The newest news answer on record today — heal or scheduled read.

        The real one is an ET-day-bounded query over the SAME
        `agent_name='news_analyst', kind='analysis', scope='run'` row the
        ordinary sessions already write; newest-last here stands in for
        newest-first there.
        """
        for row in reversed(self.evidence):
            if (row.get("kind") == "analysis"
                    and row.get("agent_name") == "news_analyst"
                    and row.get("scope") == "run"):
                return row.get("evidence_json")
        return None


class _Recorded:
    """A stand-in for `AgentResult` carrying what a paid call costs."""

    def __init__(self):
        self.user_message = "the heal prompt"
        self.raw_text = '{"pm_briefing": "re-asked"}'
        self.model = "claude-sonnet-4-6"
        self.tokens_used = 4321
        self.input_tokens = 4000
        self.output_tokens = 321
        self.cost_usd = 0.0412
        self.provider_requests = 1
        self.latency_s = 2.5
        self.truncated = False


def _bind(obj):
    for name in (
        "_carry_forward_news",
        "_latest_news_read_today",
        "_cover_healed_news_wire",
        "_news_has_newer_material_wire",
        "_watched_research_symbols",
        "_peek_news_headlines",
        "_peeked_news_wire_text",
        "_try_one_paid_research_retry",
        "_heal_lost_research_seats",
        "_persist_heal_call",
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
        db=db if db is not None else _DayLedger(),
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

        # And a read that cannot answer must say so rather than report a
        # clean zero — the caller treats those two differently.
        db.conn.close()
        assert db.count_paid_seat_heals_today("news") is None


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
        # The heal path writes both of these; the wording keys on the FACT
        # in details, not on whether someone set the sentence.
        details={"was_expired": True},
        owner_consequence=(
            "The desk still holds this seat's earlier answer and will decide "
            "on it. No trade was withheld for this."
        ),
    )
    body = heal_failure_alert_text(expired, cap_blocked=True)
    assert "lost or empty" not in body
    assert "still holds" in body and "No trade was withheld" in body
    # "not treated as green-empty" is lost-seat reassurance and is
    # meaningless for a seat that was never empty.
    assert "green-empty" not in body

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


def test_an_unreadable_day_ledger_is_not_the_same_as_nothing_spent():
    """The counter returns None when it could not find out.

    A shared 0 for "nothing spent" and "read failed" forced one global
    policy on two different situations. The heal allows the retry through
    and leaves the spend to the cost circuit, but it must not silently
    record that as a clean zero.
    """
    class _Unreadable:
        def count_paid_seat_heals_today(self, seat, **kw):
            return None

    from src.seat_heal import HEAL_DAY_CAP
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=_Unreadable())

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1
    assert not [r for r, _a in obj.recorded if r.outcome == HEAL_DAY_CAP]


def test_a_day_cap_row_is_only_written_when_the_cap_is_what_stopped_the_spend():
    """The row is the evidence that could settle whether one-a-day is right,
    so a tick that would have refused anyway must not be recorded as capped."""
    from src.seat_heal import HEAL_DAY_CAP
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=_DayLedger({"news": 1}))
    # No wire text: this tick had nothing to re-ask with, cap or no cap.
    ctx.heal_news_text = None

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 0
    assert not [r for r, _a in obj.recorded if r.outcome == HEAL_DAY_CAP], (
        "a tick with no inputs was recorded as blocked by the day cap"
    )


def test_the_day_boundary_the_cap_uses_is_the_et_trading_day():
    """`_et_day_utc_bounds` is the half most likely to be wrong on a host in
    another timezone, and it is unreachable without a day to ask about."""
    import json
    import tempfile
    from datetime import date, timedelta
    from pathlib import Path
    from src.storage.db import Database
    from src.seat_heal import HealResult, HEAL_PAID_RETRY

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "t.db"))
        db.initialize()
        db.insert_specialist_evidence(
            run_id="r1", agent_name="seat_heal", kind="seat_heal", scope="run",
            evidence_json=json.dumps(HealResult(
                seat="news", outcome=HEAL_PAID_RETRY, reason="r",
                paid_retry=True,
            ).to_evidence(), sort_keys=True),
        )
        from src.storage.db import et_today
        today = et_today()
        assert db.count_paid_seat_heals_today("news", trading_day=today) == 1
        # Yesterday's allowance is a different allowance.
        assert db.count_paid_seat_heals_today(
            "news", trading_day=today - timedelta(days=1),
        ) == 0
        assert db.count_paid_seat_heals_today(
            "news", trading_day=today + timedelta(days=1),
        ) == 0


def test_every_session_the_news_analyst_can_be_asked_for_has_its_own_guidance():
    """The mechanism, not just today's missing key.

    `_SESSION_GUIDANCE` used to fall back to MORNING for any unknown
    session. That produced audit round 2 #24 (close) and then produced the
    intra_check defect five months later. Asserting `"intra_check" in
    guidance` would not have caught either one before it shipped.
    """
    import typing
    from src.agents.news_analyst import NewsAnalystAgent
    from src import pipeline_context

    sessions = set(typing.get_args(pipeline_context.SessionType))
    guidance = NewsAnalystAgent._SESSION_GUIDANCE
    missing = sorted(sessions - set(guidance))
    # A session with no entry must not silently look like morning. Either it
    # has its own entry, or the neutral fallback claims nothing about the
    # time of day — and the neutral fallback must exist.
    neutral = NewsAnalystAgent._UNKNOWN_SESSION_GUIDANCE
    assert "fresh book" not in neutral.lower()
    assert neutral != guidance["morning"]
    for session in missing:
        # Documented as deliberately unguided, not silently morning-shaped.
        assert session in {"earnings_preprocess"}, (
            f"{session!r} reaches the news analyst with no guidance entry"
        )


def test_an_unknown_session_does_not_get_the_morning_fresh_book_instruction():
    from src.agents.news_analyst import NewsAnalystAgent
    agent = NewsAnalystAgent.__new__(NewsAnalystAgent)
    message = agent.build_user_message(
        news_text="- something crossed the wire", session="not_a_session",
    )
    assert "fresh book" not in message.lower()
    assert "MORNING mode" not in message


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
    # And no other module may READ it into a verdict either. Checked on
    # code, not on filenames: an earlier version of this test matched any
    # mention, which pushed a legitimate docstring cross-reference in
    # src/storage/db.py into being reworded vaguely to keep the list short.
    # A comment naming the constant is documentation and is welcome; a
    # statement consulting it outside the heal dispatcher is the defect.
    import ast as _ast
    from pathlib import Path
    root = Path(gate.__file__).resolve().parent.parent
    code_readers = set()
    scanned = 0
    for path in sorted(root.glob("src/**/*.py")) + sorted(root.glob("tests/**/*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "HEALABLE_CATEGORIES" not in text:
            continue
        scanned += 1
        tree = _ast.parse(text)
        for node in _ast.walk(tree):
            # A real reference in code, not a mention in a docstring or a
            # comment. `x.HEALABLE_CATEGORIES` and a bare name both count.
            name = None
            if isinstance(node, _ast.Attribute):
                name = node.attr
            elif isinstance(node, _ast.Name):
                name = node.id
            if name != "HEALABLE_CATEGORIES":
                continue
            rel = str(path.relative_to(root))
            if rel == "src/evidence_gate.py":
                continue  # the definition itself
            code_readers.add(rel)
    assert scanned >= 3, "the scan found too few files — the check is broken"
    assert code_readers == {
        "src/pipeline.py", "tests/test_seat_heal_wiring.py",
    }, (
        f"HEALABLE_CATEGORIES is read in code outside the heal dispatcher: "
        f"{sorted(code_readers)}. It is a refresh-cost hint, not a trading "
        f"consequence."
    )


# ── the desk paid for research and kept neither the answer nor the price ──
#
# Verified against production on 2026-09-23 before the fix: `specialist_
# evidence` holds 22 `kind='seat_heal'` rows, all 2026-09-18, 14 `failed` and
# 8 `paid_retry` — every paid one on the news seat. The union of JSON keys
# across all 22 rows is {gate, seat, outcome, reason, mechanical, paid_retry,
# usable, details}: no model, no tokens, no cost, not one word the model said.
# `agent_logs` holds ZERO `news_analyst%` rows for the whole of 2026-09-18,
# so the owner's per-session cost line (which sums `agent_logs.cost_usd` by
# `run_id`) reported those eight paid calls as free.

def test_a_paid_heal_records_its_cost_the_way_an_ordinary_paid_call_does():
    """THE regression test for the thrown-away spend."""
    ledger = _DayLedger()
    analyst = _Analyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=ledger)

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 1
    assert len(ledger.agent_logs) == 1, (
        "a paid heal wrote no agent_logs row — its cost is invisible to the "
        "owner's per-session cost line, which sums agent_logs.cost_usd"
    )
    row = ledger.agent_logs[0]
    assert row["cost_usd"] == 0.0412
    assert row["model"] == "claude-sonnet-4-6"
    assert row["tokens_used"] == 4321
    assert row["input_tokens"] == 4000
    assert row["output_tokens"] == 321
    assert row["run_id"] == ctx.run_id


def test_a_paid_heal_keeps_what_the_model_actually_said():
    ledger = _DayLedger()
    obj, ctx = _expired_news_tick(db=ledger)

    obj._heal_lost_research_seats(ctx)

    row = ledger.agent_logs[0]
    assert "re-asked" in row["full_response"], "the model's answer was dropped"
    assert row["input_message"] == "the heal prompt", "the prompt was dropped"
    # And the structured answer lands in the SAME evidence row an ordinary
    # read writes, not in a second invented shape.
    analyses = [e for e in ledger.evidence if e["kind"] == "analysis"]
    assert len(analyses) == 1
    assert analyses[0]["agent_name"] == "news_analyst"
    assert analyses[0]["scope"] == "run"
    assert "re-asked" in analyses[0]["evidence_json"]


def test_the_heal_row_carries_the_seats_ordinary_agent_name_marked_as_a_heal():
    """Not a new agent name: that would hide the spend from every per-seat
    query that exists today. The desk's two other paid re-asks (exit-trigger,
    candidate-accounting) already mark themselves in `input_summary` and keep
    the ordinary name; this follows them. News keeps its `_{session}` suffix
    because that IS its ordinary name (`news_analyst_{session}`)."""
    ledger = _DayLedger()
    obj, ctx = _expired_news_tick(db=ledger)

    obj._heal_lost_research_seats(ctx)

    row = ledger.agent_logs[0]
    assert row["agent_name"] == "news_analyst_intra_check"
    assert "seat heal" in row["input_summary"]
    assert "news" in row["input_summary"]


def test_a_failed_heal_bills_nothing_because_there_is_nothing_to_bill():
    """Only a heal that came back with an answer writes the paid-call row.
    A raise or a None answer must not leave a row of zeroes reading as a
    completed call."""
    ledger = _DayLedger()
    obj, ctx = _expired_news_tick(analyst=_Analyst(outcome="none"), db=ledger)

    obj._heal_lost_research_seats(ctx)

    assert ledger.agent_logs == []
    assert [e for e in ledger.evidence if e["kind"] == "analysis"] == []


def test_a_broken_forensic_store_never_undoes_a_heal_that_worked():
    """A log write is bookkeeping. Losing it must not cost the desk the
    fresher research it already paid for."""
    class _Exploding(_DayLedger):
        def insert_agent_log(self, **kw):
            raise RuntimeError("disk full")

        def insert_specialist_evidence(self, **kw):
            raise RuntimeError("disk full")

    obj, ctx = _expired_news_tick(db=_Exploding())

    obj._heal_lost_research_seats(ctx)

    assert ctx.data_status["news"] == "ok"
    assert ctx.news_intel is not None


# ── the write-back: what the desk paid for must survive the next tick ─────
#
# THE DEFECT THIS SECTION EXISTS TO PREVENT FROM RECURRING.
# Reconnecting the dispatcher (above) made the desk BUY fresher news. It did
# not make the desk KEEP it. The paid answer was written to `ctx` and to
# `specialist_evidence` and nowhere the next tick reads, and the wire the
# desk paid the model to read was never recorded as read. So 30 minutes
# later the carry-forward re-loaded the superseded morning file, the same
# RSS titles compared as newly-moved all over again, and the seat expired a
# second time — at which point the durable per-ET-day cap correctly refused
# to buy what the desk had already bought. Pay, discard, then decline to
# re-buy. Production, 2026-09-18, before the cap existed: EIGHT paid news
# heals, 15:19 to 19:46 UTC, roughly one per tick, every one thrown away
# [measured 2026-09-23 over a read-only copy of the production DB].

def _report_with(stock_news: dict) -> dict:
    from src.models import MacroNarrative, NewsIntelligenceReport
    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-09-23", era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        state_changes=[], stock_news=stock_news,
        pm_briefing="re-asked", market_sentiment="bullish", confidence="medium",
    ).model_dump()


class _RealAnalyst(_Analyst):
    """Returns a report that actually validates, so the write-back is real."""

    def __init__(self, stock_news=None, **kw):
        super().__init__(**kw)
        self._payload = _report_with(
            stock_news if stock_news is not None
            else {"AAPL": [{"headline": "Apple guidance cut", "sentiment": "bearish",
                            "conviction": "high", "impact_summary": "cut"}]},
        )

    def analyze(self, payload, *args, **kwargs):
        import json as _json
        self.calls += 1
        self.seen = payload
        return (
            SimpleNamespace(
                model_dump=lambda: dict(self._payload),
                model_dump_json=lambda: _json.dumps(self._payload),
            ),
            self.call_result,
        )


def _healed_desk(analyst=None, titles=None):
    """Run ONE tick that heals, and hand back the stores it wrote to."""
    analyst = analyst if analyst is not None else _RealAnalyst()
    obj, ctx = _expired_news_tick(analyst=analyst, titles=titles or _MOVED_WIRE)
    obj._heal_lost_research_seats(ctx)
    assert ctx.data_status["news"] == "ok", "first tick did not heal"
    return obj, ctx, analyst


def _next_tick(obj, titles=None):
    """The tick 30 minutes later: same stores, brand-new RunContext."""
    obj.news_provider = _Provider(titles if titles is not None else _MOVED_WIRE)
    obj.recorded.clear()
    ctx = _ctx()
    carried = obj._carry_forward_news(ctx)
    ctx.data_status = {"tech": "ok", "news": carried.status}
    ctx.news_intel = carried.payload
    return ctx, carried


def test_the_next_tick_finds_the_research_the_desk_just_paid_for():
    """THE regression test for the write-back. Fails on origin/main.

    One heal, then the tick 30 minutes later. The seat must be usable and
    must NOT be expired — the desk already holds this session's freshest
    paid read of the wire.
    """
    obj, _ctx1, analyst = _healed_desk()

    ctx2, carried = _next_tick(obj)

    assert carried.status != "expired", (
        "the paid answer was discarded; the seat expired again on the next tick"
    )
    assert carried.status == "carried_from_morning"
    assert ctx2.news_intel is not None
    assert analyst.calls == 1, "the second tick must not re-ask a seat already bought"


def test_the_seat_is_not_bought_twice_and_is_not_left_degraded_either():
    """The two failure modes are one defect and both must go.

    Before this change the second tick either re-bought the seat (production,
    2026-09-18, eight times) or — once the per-ET-day cap landed — left it
    `expired` for the rest of the day having already been paid for. A single
    degraded seat is invisible to the `data_degraded` advisory, which needs
    two, so nothing would have said so.
    """
    obj, _c1, analyst = _healed_desk()

    ctx2, carried = _next_tick(obj)
    obj._heal_lost_research_seats(ctx2)

    assert analyst.calls == 1, "the desk paid for the same research twice"
    from src import evidence_gate
    assert evidence_gate.STATUS_CATEGORY[carried.status] == \
        evidence_gate.CATEGORY_REPORTED
    assert not evidence_gate.counts_as_degraded(carried.status)


def test_the_wire_the_model_was_paid_to_read_is_recorded_as_read():
    """Keeping the ANSWER is not enough; the QUESTION has to be kept too.

    Expiry compares live RSS titles against the analyst's REWRITTEN
    headlines union `raw_headlines.json`, and those are not the same string
    — the news store says so in its own docstring. Without recording the
    wire, the healed report gets compared against titles it never claimed to
    contain and the seat expires again immediately.
    """
    obj, _c1, _a = _healed_desk()

    recorded = {i["title"] for i in obj.news_store.load_raw_headlines()}

    assert "AAPL guidance cut after close" in recorded
    assert "Apple beats" in recorded, "the morning's own titles were not dropped"


def test_a_headline_the_model_never_saw_can_still_expire_the_seat():
    """The bound is what the model WAS SHOWN, not what the peek fetched.

    Marking a headline covered suppresses it for the rest of the session, so
    covering one the re-ask never read would be buying silence rather than
    research. The prompt is truncated to `news.max_prompt_items`, so the two
    sets genuinely differ.
    """
    from src.seat_heal import wire_titles_shown_to_model

    shown = wire_titles_shown_to_model(
        ["- seen one", "never in the prompt"], "- seen one\n",
    )

    assert shown == ["- seen one"]
    assert wire_titles_shown_to_model(["anything"], "") == []


def test_a_thin_heal_does_not_lose_the_mornings_per_symbol_coverage():
    """The re-ask gets general wire text, no universe and no stock_mentions.

    It is the fresher answer about the wire and a narrower one about the
    book. A name the morning covered and the afternoon wire never mentioned
    must keep its coverage rather than silently vanish.
    """
    obj, _c1, _a = _healed_desk(analyst=_RealAnalyst(stock_news={}))

    _ctx2, carried = _next_tick(obj)

    assert carried.payload is not None
    assert "AAPL" in carried.payload.stock_news, (
        "the morning's only covered symbol was dropped by a thinner re-ask"
    )


def test_a_heal_never_rewrites_the_file_the_cross_day_scans_walk():
    """`full_report.json` is read ACROSS days — `recent_state_changes` and
    the missed-ops/thesis scans all open it for a multi-week window. A heal
    writing there would push its baseline-less `state_changes` into that
    catalyst memory and delete the morning's from it. The refresh is
    within-day; the file is not."""
    obj, _c1, _a = _healed_desk()

    assert obj.news_store.saved == [], (
        "a within-day refresh was written to a file read across days"
    )


def test_a_stored_answer_that_will_not_parse_never_costs_the_desk_the_file():
    """Demoting an `expired` seat to a LOST one is strictly worse. A row
    that cannot be read must fall back to the day's report, not to nothing."""
    ledger = _DayLedger()
    ledger.evidence.append({
        "agent_name": "news_analyst", "kind": "analysis", "scope": "run",
        "evidence_json": '{"pm_briefing": "half a report"}',
    })
    obj = _pipeline(_MOVED_WIRE, db=ledger)
    ctx = _ctx()

    carried = obj._carry_forward_news(ctx)

    assert carried.status == "expired", "fell through to lost instead of the file"
    assert ctx.heal_news_text, "the wire text handoff survived the fallback"


def test_an_unreachable_forensic_store_still_leaves_the_day_report_readable():
    class _Broken(_DayLedger):
        def latest_news_analysis_today(self, trading_day=None):
            raise RuntimeError("db locked")

    obj = _pipeline(["Apple beats"], db=_Broken())

    carried = obj._carry_forward_news(_ctx())

    assert carried.payload is not None
    assert carried.status == "carried_from_morning"


def test_recording_the_wire_appends_and_is_idempotent(tmp_path):
    """The REAL store, not the stub. `save_raw_headlines` replaces, which
    would wipe the morning's titles and re-arm the very compare this is
    quieting; repeating must not grow the file either."""
    from src.data.news_store import NewsStore
    store = NewsStore(data_dir=str(tmp_path))
    store.save_raw_headlines([{"title": "Apple beats"}])

    first = store.append_raw_headlines([{"title": "Apple beats"}, {"title": "new one"}])
    second = store.append_raw_headlines([{"title": "new one"}])

    assert (first, second) == (1, 0)
    assert [i["title"] for i in store.load_raw_headlines()] == [
        "Apple beats", "new one",
    ]


def test_the_day_cap_still_binds_after_the_write_back():
    """The write-back must reduce demand on the cap, never defeat it. A seat
    that has had its paid heal today is still refused one, and the cap is
    still counted from the durable rows rather than from anything new."""
    analyst = _RealAnalyst()
    obj, ctx = _expired_news_tick(analyst=analyst, db=_DayLedger({"news": 1}))

    obj._heal_lost_research_seats(ctx)

    assert analyst.calls == 0
    assert ctx.data_status["news"] == "expired"
    assert obj.news_store.load_raw_headlines() == [{"title": "Apple beats"}], (
        "a refused heal recorded wire coverage it never paid to read"
    )
