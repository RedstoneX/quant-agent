"""src.research_continuity.heal_records -- keep what a paid heal bought.

Bodies moved verbatim from src/pipeline_research_continuity.py (originally
src/pipeline.py), item 210 step 9, second research-continuity instalment.
The durable heal log, the paid-call records (agent_logs + the
analysis evidence row), and the macro-store and news-wire keeps that stop
a healed seat expiring again. Writes storage and the journal it is handed.

Every collaborator is an explicit keyword-only constructor argument; nothing
here imports src.pipeline. Storage and the evidence journal (anything with
`EventJournal.persist_evidence`, src/ports/event_journal.py) are handed in,
never reached for through a host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from src.agents.base import agent_log_kwargs

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class HealRecords:
    """Records of a seat heal: the log row, the paid call, and what the heal bought."""

    def __init__(
        self,
        *,
        db,
        journal,
        macro_store,
        macro,
        news_store,
        peek_items_get: Callable[[], object] | None = None,
        record_heal=None,
        persist_heal_call=None,
        persist_healed_macro_store=None,
        cover_healed_news_wire=None,
    ) -> None:
        self.db = db
        self.journal = journal
        self.macro_store = macro_store
        self.macro = macro
        self.news_store = news_store
        self._peek_items_get = peek_items_get
        # A host that replaced one of these (a test double, an instance-level
        # override) is honoured; the host's own thin shim is never passed back
        # in, so the part keeps its own body (see cost_circuit/parts/shim_guard).
        if record_heal is not None:
            self._record_heal = record_heal
        if persist_heal_call is not None:
            self._persist_heal_call = persist_heal_call
        if persist_healed_macro_store is not None:
            self._persist_healed_macro_store = persist_healed_macro_store
        if cover_healed_news_wire is not None:
            self._cover_healed_news_wire = cover_healed_news_wire

    @property
    def last_news_peek_items(self):
        """The wire items the expiry peek fetched this tick, wherever the host keeps them."""
        if self._peek_items_get is not None:
            return self._peek_items_get()
        return None

    def _record_heal(self, ctx, result, *, alert: bool) -> None:
        """Durable heal log. Pages only on attempted-and-failed / cap-block."""
        import json as _json

        try:
            self.journal.persist_evidence(
                run_id=ctx.run_id,
                agent_name="seat_heal",
                kind="seat_heal",
                scope="run",
                evidence_json=_json.dumps(result.to_evidence(), sort_keys=True),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: evidence write failed: %s", e)
        if not alert:
            return
        try:
            from src.notifier import send_owner_alert
            from src.seat_heal import HEAL_CAP_BLOCKED, heal_failure_alert_text

            send_owner_alert(
                heal_failure_alert_text(
                    result,
                    cap_blocked=result.outcome == HEAL_CAP_BLOCKED,
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("seat heal: owner alert failed: %s", e)

    def _persist_heal_call(self, ctx, seat: str, agent_name: str, analysis, call_result) -> None:
        """Record a PAID heal exactly the way an ordinary paid call is recorded.

        Two rows, both of them the EXISTING path, neither of them new:

          * `agent_logs` — the model, the tokens, the cost, the raw answer and
            the prompt that produced it. Written under the seat's ORDINARY
            agent name and marked as a heal in `input_summary`, which is the
            convention the desk's two other paid re-asks already follow (the
            exit-trigger re-ask logs `position_reviewer`, the candidate-
            accounting re-ask logs `portfolio_manager`). A separate agent name
            would hide the spend from every per-seat query that exists today,
            which is a different corruption, not less of one. News keeps its
            `_{session}` suffix because that IS its ordinary name.
          * `specialist_evidence(kind="analysis")` — the model's answer as
            structured evidence, the same row `RiskStage` writes for an
            ordinary news or macro read.

        Never raises: a forensic-write failure must not undo a heal that
        succeeded, the same rule `_persist_evidence` and the two re-ask log
        writes above already follow.
        """
        session = getattr(ctx, "session", None) or "intra_check"
        # `news_analyst_{session}` is the ordinary name for the news seat
        # (`_run_news_analysis`); macro logs flat. Match each, don't invent.
        log_name = f"{agent_name}_{session}" if seat == "news" else agent_name
        if call_result is None:
            # An analyst that returned an answer but no call record. Nothing
            # to bill and nothing to quote — say so rather than writing a row
            # of zeroes that would read as a free call.
            logger.warning(
                "seat heal: %s returned no call result; cost and raw answer for this paid retry cannot be recorded",
                seat,
            )
        else:
            try:
                self.db.insert_agent_log(
                    agent_name=log_name,
                    run_id=ctx.run_id,
                    input_summary=f"seat heal re-ask | {seat} | session={session}",
                    input_message=getattr(call_result, "user_message", "") or "",
                    output_summary=f"seat heal refreshed {seat}",
                    full_response=getattr(call_result, "raw_text", "") or "",
                    model=getattr(call_result, "model", "") or "",
                    tokens_used=getattr(call_result, "tokens_used", 0) or 0,
                    input_tokens=getattr(call_result, "input_tokens", None),
                    output_tokens=getattr(call_result, "output_tokens", None),
                    cost_usd=getattr(call_result, "cost_usd", None),
                    **agent_log_kwargs(call_result),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("seat heal: paid-call log write failed: %s", e)
        try:
            dump = getattr(analysis, "model_dump_json", None)
            if callable(dump):
                evidence_json = dump()
            else:
                import json as _json

                evidence_json = _json.dumps(analysis, sort_keys=True, default=str)
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: could not serialise %s answer: %s", seat, e)
            return
        self.journal.persist_evidence(
            run_id=ctx.run_id,
            agent_name=agent_name,
            kind="analysis",
            scope="run",
            evidence_json=evidence_json,
        )

    def _persist_healed_macro_store(self, ctx, payload: dict) -> None:
        """Write a paid macro heal's answer back to the macro store.

        THE MACRO HALF OF THE SAME DEFECT the news heal already fixed.
        `_persist_heal_call` keeps the forensic rows (`agent_logs`,
        `specialist_evidence`); this keeps the WORKING macro state. Without it
        the paid read only ever reached `ctx.macro_analysis` for this one
        tick's PM, then vanished: `_carry_forward_macro` re-reads
        `macro_store.load_last_state()` every tick, and the evening
        thesis-health read and the 7-day regime history read the same store,
        so the stale morning snapshot — not the fresher regime the desk PAID
        for — was what every later reader saw. That is the KEEP WHAT COSTS
        MONEY class of defect, on the macro seat instead of the news seat.

        Persisted the SAME way the scheduled morning read persists
        (`MorningResearchStage`): `save_last_state(payload, series_prints)`,
        with the FRED fingerprint the heal call actually saw so a later tick's
        expiry compares against real prints rather than re-expiring blind. A
        summary with no prints simply stores none — `_macro_series_prints_
        changed` treats an absent fingerprint as "no change", never as churn.

        Never raises — a store-write failure must not undo a paid heal that
        succeeded, the same rule `_persist_heal_call` and
        `_cover_healed_news_wire` already follow.
        """
        store = getattr(self, "macro_store", None)
        save = getattr(store, "save_last_state", None)
        if not callable(save) or not isinstance(payload, dict):
            return
        try:
            from src.data.macro_store import series_prints_from_summary

            prints = series_prints_from_summary(
                getattr(ctx, "macro_summary", None) or {},
                freshness=getattr(getattr(self, "macro", None), "_run_freshness", None),
            )
            save(payload, series_prints=prints)
            logger.info(
                "seat heal: persisted the paid macro read to the macro store (regime=%s)",
                payload.get("regime"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: macro store-write failed: %s", e)

    def _cover_healed_news_wire(self, ctx) -> None:
        """Record the wire a paid news heal just read, so it stops expiring.

        THE SECOND HALF OF THE SAME DEFECT. `_persist_heal_call` keeps the
        ANSWER; this keeps the QUESTION. Without it the answer alone changes
        nothing, because `_news_has_newer_material_wire` compares live RSS
        titles against `covered_news_headlines(report)` — the analyst's own
        REWRITTEN headlines — union today's `raw_headlines.json`. Those two
        strings are not the same ID (`NewsStore.load_raw_headlines` says so
        outright), so a healed report is compared against titles it never
        claimed to contain, the same wire reads as newly moved on the next
        tick, and the seat expires again 30 minutes after the desk bought it.

        Only headlines the model was ACTUALLY SHOWN are recorded — measured
        off the prompt text, not the fetch (`seat_heal.wire_titles_shown_to_
        model`). A title the peek fetched but the prompt truncated away is
        left uncovered on purpose: it must still be able to expire the seat.
        That is the difference between recording research and buying silence.

        Appends, never replaces: overwriting would drop the morning's
        per-symbol titles and re-arm the very compare this is quieting.
        Never raises — a coverage write must not undo a paid heal.
        """
        from src.seat_heal import wire_titles_shown_to_model

        try:
            items = list(self.last_news_peek_items or [])
            titles: list[str] = []
            for item in items:
                title = getattr(item, "title", None)
                if title is None and isinstance(item, dict):
                    title = item.get("title") or item.get("headline")
                text = str(title or "").strip()
                if text:
                    titles.append(text)
            shown = wire_titles_shown_to_model(
                titles,
                getattr(ctx, "heal_news_text", "") or "",
            )
            if not shown:
                return
            append = getattr(
                getattr(self, "news_store", None),
                "append_raw_headlines",
                None,
            )
            if not callable(append):
                return
            added = append([{"title": t, "source": "seat_heal", "summary": ""} for t in shown])
            logger.info(
                "seat heal: recorded %d of %d peeked wire titles as read by the paid news re-ask",
                added,
                len(titles),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("seat heal: wire-coverage write failed: %s", e)
