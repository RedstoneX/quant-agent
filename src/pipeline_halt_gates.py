"""The two gates that halt a run before it may decide: the kill switch and the evidence gate.

Moved VERBATIM out of `TradingPipeline` in `src/pipeline.py` (board: the
pipeline split). Function-only module in the `src.pipeline_sizing` shape: each
function takes the pipeline (or any stub carrying the attributes it reads) as
its first argument, so a gate can be built and exercised from stubs without
constructing a `TradingPipeline`. The bodies did not change by one character;
the parameter is still named `self` for that reason. `TradingPipeline` keeps a
one-line shim per name so every `self._x(...)` caller and every
`patch.object(TradingPipeline, "_x")` keeps working.

Attributes read off the first argument: `_kill_switch_path` (kill switch);
`db`, `_last_evidence_freshness`, `_last_decision_data_status` and
`_record_name_coverage` (evidence gate).

This module must not import `src.pipeline`.
"""

from __future__ import annotations

import logging

from src.recording_accessors import pinned_evidence
from src.pipeline_stages import _record_pipeline_event

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _kill_switch_halt_result(self, run_id: str, **extra) -> dict | None:
    """Guard 1's early, VISIBLE half (2026-09-02 operational safety
    guard). Returns an early-exit result dict when ops has halted the
    desk, else None.

    The broker-level check (`AlpacaBroker._kill_switch_active`) is what
    actually GUARANTEES no order reaches Alpaca while the flag file
    exists — it re-checks on every single submit/replace call, so it
    stays correct even if the file appears mid-session, after this
    early check already passed. This method exists only so a halted
    run (a) does not spend real broker calls and LLM budget on analysis
    that can place no order, and (b) produces exactly ONE clear alert
    on the channel the operator actually reads: the returned
    `status` flows through `format_session_result` to
    `TelegramNotifier.send()` in `main.py`, the SAME path every other
    session result already takes — no new alerting mechanism.

    UNLIKE `_paid_suspended_payload` above, nothing NEW is preserved:
    this is the one guard in the codebase that also blocks a
    risk-reducing order (see RiskConfig.kill_switch_path), so a new
    protective stop cannot go out either while it is active. A stop
    already resting at the broker from before the halt is untouched
    and keeps protecting its position — only new broker-bound order
    flow is refused.
    """
    if self._kill_switch_path is None or not self._kill_switch_path.exists():
        return None
    logger.error(
        "KILL SWITCH ACTIVE (%s exists) — halting run %s before any "
        "broker or LLM work. touch/rm that file to stop/resume the "
        "desk.", self._kill_switch_path, run_id,
    )
    payload = {
        "status": "kill_switch_halted", "run_id": run_id, "orders": [],
        "kill_switch_path": str(self._kill_switch_path),
    }
    payload.update(extra)
    return payload


def _evidence_gate_skip(
    self, ctx, run_id: str, *, session: str = "morning",
) -> dict | None:
    """docs/WORK.md item 20 — refuse to DECIDE on evidence that never
    arrived. Returns a terminal result dict when the run must skip, or
    None to proceed.

    The distinction it rests on is categorical and needs no threshold: a
    seat that had nothing to report answered; a seat whose answer was
    lost did not. See `src/evidence_gate.py` for why no count is used and
    why the counting half of the owner's design is deliberately unbuilt.

    WHICH LOST SEAT ACTUALLY STOPS THE RUN is an owner mandate decision
    of 2026-09-18 — "Only technical analysis can stop the desk" — and
    lives in `evidence_gate.BLOCKING_SEATS`, not here. A lost ADVISORY
    seat is recorded in the same durable rows, logged loudly, carried in
    the result so the unsuppressible data-quality alert still fires, and
    named in the freshness disclosure. It does not halt trading.

    EVERY DECISION DISCLOSES ITS OWN EVIDENCE FRESHNESS. With the other
    seats advisory a decision can rest on one freshly-read seat plus a
    carried-forward book, and every carried seat reports green; this is
    the one path every decision passes through, so the count of seats
    read on THIS tick is computed here and handed to the owner's message
    and the durable record. Disclosure, not a threshold — there is no
    minimum fresh count anywhere and none may be invented.

    THE SKIP IS LOUD, by three independent paths, because retired item 11
    was this desk producing nothing for a whole day with nobody noticing
    (docs/INCIDENT_HISTORY.md, closed 2026-09-13):
      - its own standalone owner alert, sent here — MORNING ONLY as of
        2026-09-18. On an intra_check tick the session message below is
        guaranteed to speak (`evidence_gate_skip` is actionable on the
        trader feed and is in none of its silent-status sets), so this
        alert only duplicated it, one minute apart, word for word;
      - `notifier.maybe_alert_data_quality`, which fires from main.py's
        finally block on the `data_status` carried in the result and
        cannot be suppressed by a mode's noise policy;
      - the session result message, whose `status` says it in one word.

    It drops no candidate and emits no target: it returns before any
    target exists, so it cannot produce the 0%-target-means-SELL shape.
    Every symbol that HAD reached a technical read still gets its own
    durable, machine-readable row saying why the desk never decided on
    it, alongside the run-level row.
    """
    from src import evidence_gate

    try:
        verdict = evidence_gate.evaluate(ctx.data_status)
    except Exception as exc:  # noqa: BLE001
        # A gate that can stop the desk trading must not stop it by
        # crashing. `evaluate` is documented never to raise; if it
        # somehow does, proceed and say so loudly.
        logger.error(
            "evidence gate raised (%s) — PROCEEDING with the decision. "
            "This is a bug in src/evidence_gate.py.", exc,
        )
        return None

    def _record(symbol, outcome, reason, **details):
        # Forensic persistence must never be able to break the trading
        # path it is reporting on (.claude/rules/trading-core.md).
        # `_persist_evidence` already swallows DB errors; this also
        # covers a caller with no `db` wired at all.
        try:
            _record_pipeline_event(
                self, ctx, symbol, "evidence_gate", outcome, reason,
                **details,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("evidence gate: event write failed: %s", exc)

    # Disclosure, carried out of here by `_attach_evidence_freshness` on
    # every return path of the session wrappers. Stored on the pipeline
    # as well as on ctx because the result dicts are built in dozens of
    # places and the wrappers are the two that see all of them.
    try:
        # Stamp the classification with WHEN and WHICH RUN before it is
        # persisted. Owner ruling 2026-10-01 (sell what fails the fresh
        # bar) makes "was this seat read in THIS run?" something a sell
        # can rest on, and it must be a recorded fact, not an inference
        # drawn from the shape of the row. Records only — no threshold,
        # nothing gated. Fail-soft on the prior-read lookup: an unknown
        # age is reported as unknown, never as fresh.
        prior = {}
        try:
            if getattr(self, "db", None) is not None:
                prior = self.db.last_fresh_seat_reads(
                    seats=list(verdict.freshness.data_status)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "evidence gate: prior seat-read lookup failed (%s) — "
                "carried seats will report an unknown age", exc,
            )
        stamped = verdict.freshness.stamped(
            run_id=pinned_evidence(ctx, "run_id"),
            mode=str(getattr(ctx, "session", "") or "") or None,
            prior_reads=prior,
        )
        self._last_evidence_freshness = stamped.to_evidence()
        self._last_decision_data_status = dict(verdict.data_status)
        ctx.evidence_freshness = dict(self._last_evidence_freshness)
    except Exception as exc:  # noqa: BLE001 — never break the decision
        logger.warning("evidence gate: freshness record failed: %s", exc)
    logger.info("EVIDENCE FRESHNESS — %s", verdict.freshness.summary)

    evidence = verdict.to_evidence()
    _record(None, evidence.pop("outcome"), evidence.pop("reason"), **evidence)
    self._record_name_coverage(ctx, _record)
    if not verdict.skip:
        if verdict.advisory_lost:
            # Owner mandate 2026-09-18: only the technical seat halts the
            # desk. An advisory seat losing its answer is still a real
            # fault and is still said out loud — here, in the durable row
            # above, and by `notifier.maybe_alert_data_quality`, which
            # reads the `data_status` the wrappers now attach to every
            # result. What it no longer does is stop trading.
            logger.error(
                "evidence gate: ADVISORY seat(s) lost their answer and the "
                "decision PROCEEDED (owner mandate 2026-09-18, only the "
                "technical seat blocks): %s",
                {s: verdict.data_status.get(s) for s in verdict.advisory_lost},
            )
        if verdict.unclassified:
            logger.error(
                "evidence gate: unclassified seat status this run: %s",
                {s: verdict.data_status.get(s) for s in verdict.unclassified},
            )
        return None

    logger.error("EVIDENCE GATE — %s", verdict.reason)
    for analysis in ctx.analyses or []:
        symbol = getattr(analysis, "symbol", None)
        if symbol:
            _record(
                symbol, "not_decided", "evidence_gate_skip",
                lost_seats=list(verdict.lost),
                blocking_lost_seats=list(verdict.blocking_lost),
                data_status=dict(verdict.data_status),
            )
    # Legit PM-less completion — same reason `no_data` records one: the
    # evening dead-man probe must not read "research rows, no PM row" as
    # a morning that was killed mid-run.
    from src import decision_checkpoint as _dc

    # Morning only: the evening dead-man probe keys off this
    # checkpoint. An intra_check skip must not overwrite a completed
    # morning's status with a later refusal.
    if session == "morning":
        _dc.write_status("morning", "evidence_gate_skip")
    # The owner was told the same skip TWICE, one minute apart, on
    # 2026-09-18 11:19 ET: once by this standalone alert and once by the
    # intraday tick's own message. On an intra_check tick the tick
    # message is guaranteed to speak — `evidence_gate_skip` is in
    # `trader_feed._intraday_tick_actionable`'s list and in neither
    # `_BASE_ONLY_STATUSES` nor `_INTRADAY_SILENT_STATUSES`, so the
    # "a quiet tick is silent" policy that this standalone alert exists
    # to defeat cannot apply to a skip. The tick message also carries
    # P&L and the book, which this one cannot. So the tick message
    # speaks for an intraday skip and this alert stays quiet; the skip
    # is not silenced anywhere, and the morning path (whose own session
    # message is a different renderer) keeps its alert unchanged.
    if session == "morning":
        try:
            from src.notifier import CATEGORY_OPERATIONAL, describe_skipped_decision, send_owner_alert

            # Plain words only — no run id, no seat key, no state token
            # and no `verdict.reason`. The machine reason is unchanged in
            # the result dict, the event rows and the log line above.
            send_owner_alert("\n".join(
                describe_skipped_decision(verdict.lost, verdict.data_status)
            ), category=CATEGORY_OPERATIONAL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evidence gate: owner alert failed: %s", exc)
    return {
        "status": "evidence_gate_skip", "orders": [], "run_id": run_id,
        "data_status": dict(ctx.data_status),
        "lost_seats": list(verdict.lost),
        "blocking_lost_seats": list(verdict.blocking_lost),
        "advisory_lost_seats": list(verdict.advisory_lost),
        "evidence_freshness": verdict.freshness.to_evidence(),
        "reason": verdict.reason,
    }
