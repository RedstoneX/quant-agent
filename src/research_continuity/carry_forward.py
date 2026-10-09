"""src.research_continuity.carry_forward -- may yesterday's paid research be reused on this tick?

Bodies moved verbatim from src/pipeline_research_continuity.py (originally
src/pipeline.py), item 210 step 9, second research-continuity instalment.
The carry-forward readers for the macro, news and earnings seats and
the `CarryForward` answer every reader returns (the insider reader lives
in insider_memory.py with the Form 4 reads it is built from). Read-only:
writes no journal row. The change detectors it asks are handed in as
callables (a `ResearchChangeDetectors`'s bound methods, or a host's).

Every collaborator is an explicit keyword-only constructor argument; nothing
here imports src.pipeline. Storage and the evidence journal (anything with
`EventJournal.persist_evidence`, src/ports/event_journal.py) are handed in,
never reached for through a host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


@dataclass(frozen=True)
class CarryForward:
    """Morning evidence reused on an intraday tick, with an honest status.

    `payload` is the stored object when a reusable answer exists; otherwise
    None. `status` is the data_status word the caller must write — never
    inferred from payload truthiness. `same_session` is True only when the
    stored answer is from today's session and carries a trustworthy date —
    an undated snapshot is not same-session. Holding-discipline uses that
    to refuse treating a cross-day remembered regime as proof about today.
    """

    payload: object | None
    status: str
    same_session: bool = True

    def __bool__(self) -> bool:
        """True only when a payload is present.

        Status must still be read from `.status` — truthiness is only a
        safety net so a leftover `if carried` cannot treat an empty or
        failed lookup as a successful carry.
        """
        return self.payload is not None


class CarryForwardReaders:
    """Remembered-research readers for the macro, news and earnings seats."""

    def __init__(
        self,
        *,
        db,
        macro_store,
        news_store,
        earnings_provider,
        load_earnings_analyses: Callable | None = None,
        macro_regime_or_print_changed: Callable | None = None,
        news_has_newer_material_wire: Callable | None = None,
        peeked_news_wire_text: Callable | None = None,
        carry_forward_macro=None,
        latest_news_read_today=None,
        carry_forward_news=None,
        carry_forward_earnings=None,
    ) -> None:
        self.db = db
        self.macro_store = macro_store
        self.news_store = news_store
        self.earnings_provider = earnings_provider
        self._load_earnings_analyses = load_earnings_analyses
        self._macro_regime_or_print_changed = macro_regime_or_print_changed
        self._news_has_newer_material_wire = news_has_newer_material_wire
        self._peeked_news_wire_text = peeked_news_wire_text
        # A host that replaced one of these (a test double, an instance-level
        # override) is honoured; the host's own thin shim is never passed back
        # in, so the part keeps its own body (see cost_circuit/parts/shim_guard).
        if carry_forward_macro is not None:
            self._carry_forward_macro = carry_forward_macro
        if latest_news_read_today is not None:
            self._latest_news_read_today = latest_news_read_today
        if carry_forward_news is not None:
            self._carry_forward_news = carry_forward_news
        if carry_forward_earnings is not None:
            self._carry_forward_earnings = carry_forward_earnings

    def _carry_forward_macro(self) -> CarryForward:
        """Remembered macro regime, keyed by kind+expiry event.

        GOOD same-session reuse stays `carried_from_morning` (PR #430).
        A GOOD prior-day regime is `remembered` until a real regime/print
        change — not `carry_forward_empty`. A blank snapshot with no
        regime is lost. A same-day `{date, regime}` trim is a regime
        snapshot for holding-discipline; it is not a full MacroAnalysis.
        PM still refuses a chain-less dict via `_macro_analysis_as_dict`.
        Holding-discipline must read `.same_session`, not payload
        truthiness, so a cross-day remember cannot falsify today's exit
        claim. An undated snapshot is not same-session — that claim
        needs a trustworthy date.
        """
        from src.evidence_kind import macro_reuse, same_session_from_date
        from src.seat_heal import coerce_macro_shape

        try:
            state = self.macro_store.load_last_state() or None
        except Exception as e:  # noqa: BLE001 — never fail a tick on carry-forward
            logger.warning("Intraday scan: macro carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        if not state:
            return CarryForward(None, "carry_forward_empty", same_session=False)
        stored_date = str(state.get("date") or state.get("as_of") or "").strip()[:10]
        same_session = same_session_from_date(stored_date)
        # A regime snapshot is reusable research for holding-discipline and
        # kind-reuse. Full MacroAnalysis validation is the PM path
        # (`_macro_analysis_as_dict`) — requiring a chain here turned a
        # same-day {date, regime} read into carry_forward_failed and made
        # a provably-false exit claim look unverifiable.
        if not isinstance(state, dict) or not str(state.get("regime") or "").strip():
            return CarryForward(None, "carry_forward_failed", same_session=same_session)
        payload, _fixes = coerce_macro_shape(dict(state))
        verdict = macro_reuse(
            payload,
            same_session=same_session,
            regime_or_print_changed=self._macro_regime_or_print_changed(payload),
        )
        if not verdict.usable:
            return CarryForward(None, verdict.status, same_session=same_session)
        return CarryForward(payload, verdict.status, same_session=same_session)

    def _latest_news_read_today(self) -> dict | None:
        """Today's news report, INCLUDING an answer a paid heal bought.

        The scheduled sessions write `data/news/<ET day>/full_report.json`.
        A paid heal does not, and must not: four readers walk that file
        ACROSS days — `NewsStore.recent_state_changes` and the three
        missed-ops/thesis scans in this module — so overwriting it would
        push a heal's baseline-less `state_changes` into a multi-week
        catalyst memory and delete the morning's from it. A file written for
        a cross-day window is the wrong place to put a within-day refresh.

        So the file stays the base, and the freshest PAID read of the day is
        layered over it from `specialist_evidence`, which is already written
        for every news answer (heal and scheduled alike) and is already
        ET-day scoped. Nothing is written here.

        LIFETIME, because this is the whole question: unchanged. Both
        sources are bounded by the same ET trading day, and what expires the
        result is still `evidence_kind.news_reuse` — the next material wire,
        or the session ending. No clock, no N-minute refresh, no new number.
        What changes is only WHICH of today's paid reads the desk finds.

        Per-symbol coverage the newer read was never asked about is kept
        (`seat_heal.merge_carried_stock_news`). Returns the file alone when
        there is no newer row, when it will not parse, or when the store is
        unreachable — a sick forensic table must never cost the desk the
        news it already has on disk.
        """
        report = self.news_store.load_daily_report()
        fetch = getattr(getattr(self, "db", None), "latest_news_analysis_today", None)
        if not callable(fetch):
            return report
        try:
            raw = fetch()
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: latest news answer read failed: %s", e)
            return report
        if not raw:
            return report
        try:
            import json as _json
            from src.models import NewsIntelligenceReport
            from src.seat_heal import merge_carried_stock_news

            fresher = _json.loads(raw)
            if not isinstance(fresher, dict):
                return report
            # Must still be a real report. A row that cannot parse is not
            # allowed to demote a file that can — that would turn an
            # `expired` seat into a LOST one, which is strictly worse.
            NewsIntelligenceReport(**fresher)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Intraday scan: stored news answer would not parse; using the day's report file: %s",
                e,
            )
            return report
        if not report:
            return fresher
        return merge_carried_stock_news(fresher, report)

    def _carry_forward_news(self, ctx=None) -> CarryForward:
        """This session's news intelligence, re-validated from its stored dump.

        Same-session GOOD reuse is #430. A newer material wire expires it.
        Parse failure is lost, never reused as research.

        When the wire DID move, the peek that proved it holds current wire
        text. Hand that to the heal path (`ctx.heal_news_text`) so the
        expired seat is re-asked with the data this tick already paid for,
        instead of being lost while fresh headlines are thrown away.

        The evidence gate is untouched by this, but NOT because expired is
        lost — it stopped being lost when #535 split `CATEGORY_EXPIRED` out
        on 2026-09-18, which is what orphaned this hand-off for five days.
        The gate is untouched because the re-ask changes only whether a
        fresher answer exists, never what the gate does with the answer the
        desk already holds. See `evidence_gate.HEALABLE_CATEGORIES`.
        """
        from src.evidence_kind import news_reuse

        try:
            report = self._latest_news_read_today()
            if not report:
                return CarryForward(None, "carry_forward_empty", same_session=False)
            from src.models import NewsIntelligenceReport

            payload = NewsIntelligenceReport(**report)
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: news carry-forward failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=False)
        # Both sources `_latest_news_read_today` can return are bounded by
        # the SAME ET trading day — the dated report directory and the
        # ET-day evidence-row filter — so a successful load is same-session
        # either way; there is no undated news snapshot on this path.
        # Empty/failed above cannot claim it.
        wire_moved = self._news_has_newer_material_wire(payload)
        verdict = news_reuse(
            payload,
            same_session=True,
            newer_material_wire=wire_moved,
        )
        if not verdict.usable:
            if wire_moved and ctx is not None:
                wire_text = self._peeked_news_wire_text()
                if wire_text:
                    ctx.heal_news_text = wire_text
            return CarryForward(None, verdict.status, same_session=True)
        return CarryForward(payload, verdict.status, same_session=True)

    def _carry_forward_earnings(self, ctx) -> CarryForward:
        """Remembered earnings write-ups until the next report / 8-K.

        Loads the cached analyses (no LLM). A `queued=True` placeholder is
        the expiry event — a new filing the preprocess has not written up.
        """
        from src.evidence_kind import earnings_reuse

        provider = getattr(self, "earnings_provider", None)
        load = getattr(self, "_load_earnings_analyses", None)
        if provider is None or not callable(load):
            return CarryForward([], "not_run_intraday", same_session=True)
        try:
            _, results = self._load_earnings_analyses(
                ctx.run_id,
                session=ctx.session,
                ctx=ctx,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Intraday scan: earnings remember failed: %s", e)
            return CarryForward(None, "carry_forward_failed", same_session=True)
        results = list(results or [])
        new_report = any(isinstance(item, dict) and item.get("queued") for item in results)
        analyzed = [item for item in results if isinstance(item, dict) and isinstance(item.get("analysis"), dict)]
        payload = analyzed if analyzed else results
        verdict = earnings_reuse(
            payload if payload else [],
            same_session=True,
            new_report_or_8k=new_report,
        )
        # Placeholders for new filings are still handed to PM (it sizes
        # down); the status is `expired` only when we have nothing usable
        # AND a new filing. When we have cached write-ups plus a queued
        # name, keep the write-ups and label the seat partial via the
        # existing earnings classifier — do not drop remembered work.
        if analyzed and new_report:
            return CarryForward(results, "chose_not_to_refetch", same_session=True)
        if not verdict.usable and not results:
            return CarryForward(None, verdict.status, same_session=True)
        status = verdict.status
        if verdict.decision == "refetch" and not analyzed:
            status = "expired"
            return CarryForward(results, status, same_session=True)
        return CarryForward(results, status, same_session=True)
