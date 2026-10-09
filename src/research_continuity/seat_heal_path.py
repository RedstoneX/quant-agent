"""src.research_continuity.seat_heal_path -- the one paid retry for a lost or expired research seat.

Bodies moved verbatim from src/pipeline_research_continuity.py (originally
src/pipeline.py), item 210 step 9, second research-continuity instalment.
The one-paid-retry decision and the heal dispatcher. Spends money
through the analyst it is handed, under the paid-analysis gate it is
handed, and records through the four `HealRecords` callables it is handed.

Every collaborator is an explicit keyword-only constructor argument; nothing
here imports src.pipeline. Storage and the evidence journal (anything with
`EventJournal.persist_evidence`, src/ports/event_journal.py) are handed in,
never reached for through a host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from src.sentinel.counted import record_swallowed

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class SeatHealer:
    """Heal a LOST or EXPIRED research seat with one paid retry."""

    def __init__(
        self,
        *,
        db,
        record_heal: Callable,
        persist_heal_call: Callable,
        persist_healed_macro_store: Callable,
        cover_healed_news_wire: Callable,
        require_paid_analysis: Callable | None = None,
        agent_for: Callable[[str], object] | None = None,
        try_one_paid_research_retry=None,
        heal_lost_research_seats=None,
    ) -> None:
        self.db = db
        self._record_heal = record_heal
        self._persist_heal_call = persist_heal_call
        self._persist_healed_macro_store = persist_healed_macro_store
        self._cover_healed_news_wire = cover_healed_news_wire
        self._require_paid_analysis = require_paid_analysis
        self._agent_for = agent_for if agent_for is not None else (lambda name: None)
        # A host that replaced one of these (a test double, an instance-level
        # override) is honoured; the host's own thin shim is never passed back
        # in, so the part keeps its own body (see cost_circuit/parts/shim_guard).
        if try_one_paid_research_retry is not None:
            self._try_one_paid_research_retry = try_one_paid_research_retry
        if heal_lost_research_seats is not None:
            self._heal_lost_research_seats = heal_lost_research_seats

    def _try_one_paid_research_retry(self, ctx, seat: str) -> bool:
        """One paid retry for a LOST or EXPIRED seat, once per ET day.

        False if we cannot honestly retry. An EXPIRED seat is a REFRESH, not
        a recovery: the desk holds the earlier answer and will decide on it
        either way, so every owner-facing sentence out of here must say that
        rather than claim the seat was lost.

        Intra often has no FRED/news stack. A retry without inputs would
        invent the seat — refuse that, and do not burn the retry slot.
        Cost-cap blocks alert the owner. Success is a durable log, not a page.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.seat_heal import (
            HealResult,
            HEAL_CAP_BLOCKED,
            HEAL_DAY_CAP,
            HEAL_FAILED,
            HEAL_PAID_RETRY,
            can_paid_retry,
            record_paid_retry,
        )
        from src import evidence_gate as _gate

        # An EXPIRED seat is being REFRESHED, not recovered: the desk holds
        # the earlier answer and will decide on it whatever happens here. Any
        # owner page from this function must say so, because the default
        # sentence ("the desk will not decide on this seat as if it had
        # answered") is true of a lost seat and false of this one — the
        # owner-facing-lie class of defect item 133 closed.
        _incoming = (getattr(ctx, "data_status", None) or {}).get(seat)
        _expired_seat = _gate.STATUS_CATEGORY.get(_incoming) == _gate.CATEGORY_EXPIRED
        _consequence = (
            (
                "The desk still holds this seat's earlier answer and will decide "
                "on it, labelled as carried rather than read this tick. No trade "
                "was withheld for this."
            )
            if _expired_seat
            else ""
        )
        retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
        if not can_paid_retry(retries, seat):
            return False
        require = self._require_paid_analysis
        agent_name = {
            "macro": "macro_analyst",
            "news": "news_analyst",
            "tech": "tech_analyst",
        }.get(seat)
        if agent_name is None or not callable(require):
            return False
        agent = self._agent_for(agent_name)
        analyze = getattr(agent, "analyze", None) if seat != "tech" else getattr(agent, "analyze_batch", None)
        if not callable(analyze):
            return False
        # The analyst already spent the one paid retry on its own parse.
        if getattr(agent, "_heal_retry_used", False) is True:
            return False
        # No inputs → would invent the seat. Do not consume the retry.
        if seat == "macro" and not (ctx.macro_summary or {}):
            return False
        if seat == "news":
            # Fresh wire text is required. Morning parse-site retry lives
            # on the analyst; intra has no honest news_text unless a hook
            # supplied one.
            news_text = getattr(ctx, "heal_news_text", None)
            if not (isinstance(news_text, str) and news_text.strip()):
                return False
        if seat == "tech":
            return False
        # Cross-tick cap, checked HERE — after the honest-inputs checks
        # above, never before them. A tick that has no wire text would have
        # refused anyway, and a day-cap row on that tick would record the cap
        # as the binding constraint when it was not. That row's whole purpose
        # is to be the evidence that later settles whether one refresh a day
        # is the right number, so it must only be written when the cap is
        # what actually stopped the spend.
        #
        # `retries` above is per-RunContext and a RunContext is one tick;
        # intra_check runs every 30 minutes and an expired seat is still
        # expired on the next tick, so without this the "one paid retry" is
        # one per tick. It was: production recorded EIGHT paid news heals on
        # 2026-09-18. See Database.count_paid_seat_heals_today.
        db = getattr(self, "db", None)
        counter = getattr(db, "count_paid_seat_heals_today", None)
        if callable(counter):
            try:
                spent_today = counter(seat)
            except Exception as e:  # noqa: BLE001
                logger.warning("seat heal: day-cap read failed for %s: %s", seat, e)
                spent_today = None
            if spent_today is None:
                # Could not find out, which is NOT the same as nothing spent.
                # Allowed through on purpose: the cost circuit below is the
                # fail-closed authority for spend and still runs, so a sick
                # forensic store cannot silently stop the desk buying fresher
                # news. Logged at WARNING so the degradation is visible
                # rather than assumed.
                logger.warning(
                    "seat heal: could not read %s's day allowance; allowing "
                    "the retry and leaving the spend to the cost circuit",
                    seat,
                )
            elif not can_paid_retry({seat: int(spent_today)}, seat):
                logger.info(
                    "seat heal: %s already had its one paid retry today (%d spent); not re-asking",
                    seat,
                    spent_today,
                )
                # Durable, not just a log line. "The desk declined to pay for
                # fresher research on this tick" is a decision about money,
                # and it is the only record that could ever show whether one
                # refresh a day is the right number. Not an owner page: the
                # cap doing its job is not an incident.
                self._record_heal(
                    ctx,
                    HealResult(
                        seat=seat,
                        outcome=HEAL_DAY_CAP,
                        reason=(
                            f"seat already had its one paid heal this ET day ({spent_today} recorded); not re-asking"
                        ),
                        paid_retry=False,
                        details={
                            "spent_today": int(spent_today),
                            "was_expired": _expired_seat,
                        },
                        owner_consequence=_consequence,
                    ),
                    alert=False,
                )
                return False
        try:
            require(agent_name)
        except PaidAnalysisSuspended as exc:
            blocked = HealResult(
                seat=seat,
                outcome=HEAL_CAP_BLOCKED,
                reason=f"spend cap blocked the one paid retry: {exc}",
                paid_retry=False,
                owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, blocked, alert=True)
            return False
        except Exception as exc:  # noqa: BLE001
            record_swallowed("research_continuity.seat_heal_path._try_one_paid_research_retry", exc, log=logger)
            return False
        ctx.heal_paid_retries = record_paid_retry(retries, seat)
        try:
            if seat == "macro":
                analysis, call_result = analyze(ctx.macro_summary)
            else:
                # Pass this run's session. The analyst's session guidance
                # defaults to MORNING ("treat today as a fresh book... this
                # report sets the tone for the day's trading"), which is
                # false on a 14:00 intra_check — the same mislabelling audit
                # round 2 #24 already fixed for the close session. The other
                # arguments stay at their defaults: a heal re-ask genuinely
                # has no universe or prior-session baseline to offer, and
                # inventing one would be worse than admitting it.
                analysis, call_result = analyze(
                    getattr(ctx, "heal_news_text", ""),
                    session=getattr(ctx, "session", None) or "intra_check",
                )
        except Exception as exc:  # noqa: BLE001
            failed = HealResult(
                seat=seat,
                outcome=HEAL_FAILED,
                reason=f"paid heal retry raised: {exc}",
                paid_retry=True,
                owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, failed, alert=True)
            return False
        if analysis is None:
            failed = HealResult(
                seat=seat,
                outcome=HEAL_FAILED,
                reason="paid heal retry returned no usable output",
                paid_retry=True,
                owner_consequence=_consequence,
                details={"was_expired": _expired_seat},
            )
            self._record_heal(ctx, failed, alert=True)
            return False
        payload = analysis.model_dump() if hasattr(analysis, "model_dump") else analysis
        if seat == "macro":
            ctx.macro_analysis = payload
        elif seat == "news":
            ctx.news_intel = analysis
        # KEEP WHAT COSTS MONEY. Until 2026-09-23 this function spent real
        # dollars on a research call and then kept neither the answer nor the
        # price: `call_result` was discarded as `_raw`, no `agent_logs` row
        # was written, and `HealResult.to_evidence()` omits `payload`. All 8
        # paid heals in production (2026-09-18, news seat) left the desk with
        # a row saying "paid_retry / usable" and nothing else — no model, no
        # tokens, no cost, not one word the model actually said. The owner's
        # own per-session cost line sums `agent_logs.cost_usd` by `run_id`
        # (`src/notifier.py`), so those eight calls read as free.
        self._persist_heal_call(ctx, seat, agent_name, analysis, call_result)
        if seat == "news":
            self._cover_healed_news_wire(ctx)
        elif seat == "macro":
            self._persist_healed_macro_store(ctx, payload)
        status = dict(ctx.data_status or {})
        status[seat] = "ok"
        ctx.data_status = status
        self._record_heal(
            ctx,
            HealResult(
                seat=seat,
                outcome=HEAL_PAID_RETRY,
                reason=(
                    "one paid retry refreshed a superseded seat"
                    if _expired_seat
                    else "one paid retry replaced a lost seat"
                ),
                payload=payload,
                paid_retry=True,
                usable=True,
                details={"was_expired": _expired_seat},
            ),
            alert=False,
        )
        return True

    def _heal_lost_research_seats(self, ctx) -> None:
        """After carry-forward: log unhealed seats. Don't page empty-store gaps
        (the evidence gate already pages those). Attempt a paid retry only
        when the seat's inputs actually exist on this run.

        Selects work by `evidence_gate.HEALABLE_CATEGORIES`, which covers a
        LOST seat (no answer) and an EXPIRED one (an answer the desk knows is
        superseded). Those two are deliberately different categories to the
        evidence gate and stay different: this loop reads the set only to
        decide whether to go and LOOK again, and changes no verdict, no skip,
        no degraded count and no freshness label. Testing for CATEGORY_LOST
        here is what orphaned the expired-news refresh on 2026-09-18.
        """
        from src import evidence_gate
        from src.seat_heal import HealResult, HEAL_FAILED

        data_status = ctx.data_status or {}
        for seat, status in list(data_status.items()):
            category = evidence_gate.STATUS_CATEGORY.get(status)
            if category not in evidence_gate.HEALABLE_CATEGORIES:
                continue
            # Empty store: nothing to heal. Gate skip is the owner page.
            if status == "carry_forward_empty":
                continue
            was_expired = category == evidence_gate.CATEGORY_EXPIRED
            attempted = self._try_one_paid_research_retry(ctx, seat)
            still = (ctx.data_status or {}).get(seat)
            if evidence_gate.STATUS_CATEGORY.get(still) not in evidence_gate.HEALABLE_CATEGORIES:
                continue
            # A LOST seat is recorded whether or not a retry was possible —
            # that row is the forensic trail for an absent answer. An EXPIRED
            # seat is not absent, and most expired seats (insider, earnings)
            # have no heal wired at all by design (#535 dissolved that
            # asymmetry rather than repairing it), so recording every one of
            # them would bury the real rows in noise. Record an expired seat
            # only when the desk actually tried and failed.
            if was_expired and not attempted:
                continue
            # An expired seat that could not be refreshed is NOT a seat the
            # desk will refuse to decide on — it still holds the earlier
            # answer. Saying otherwise in the alert would repeat the
            # owner-facing lie item 133 fixed, so the consequence sentence is
            # overridden rather than defaulted.
            consequence = (
                (
                    "The desk still holds this seat's earlier answer and will "
                    "decide on it, labelled as carried rather than read this "
                    "tick. No trade was withheld for this."
                )
                if was_expired
                else ""
            )
            result = HealResult(
                seat=seat,
                outcome=HEAL_FAILED,
                reason=f"seat still {still} after mechanical heal",
                details={
                    "status": still,
                    "paid_retry_attempted": attempted,
                    "was_expired": was_expired,
                },
                owner_consequence=consequence,
            )
            # Page only when we actually paid a retry and it failed.
            # Kind-expiry / empty-store without inputs is the evidence
            # gate's skip, not a second owner page.
            self._record_heal(ctx, result, alert=bool(attempted))
