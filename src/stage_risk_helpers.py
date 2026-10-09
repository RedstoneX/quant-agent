"""Module-level helpers of the risk stage (lifted verbatim from src/stage_risk.py).

No behaviour change: ``src.stage_risk`` re-exports every name defined here, so
every existing import path and patch target still resolves to the same object.
"""

from __future__ import annotations

from src.pipeline_stages import (
    Any,
    DROP_CODE_UNSPECIFIED,
    _ANALYSIS_DROP_KIND,
    _persist_evidence,
    _record_pipeline_event,
    logger,
    parse_telemetry,
)


def _record_queued_earnings_refusals(
    pipeline,
    ctx,
    before: list,
    after: list,
) -> None:
    """One durable per-symbol row for every BUY the queued-earnings gate
    REFUSED (`TradingPipeline._refuse_queued_earnings_buys`).

    Board item 164 (2026-09-19) built this recording for the old 5%-of-book
    clamp, which could either drop a BUY or shrink it; board item 186
    (2026-10-01) removed the clamp, so the only outcome left is a refusal and
    the `modified` row no longer exists. A refused BUY is absent from the
    list the gate returned, so it is read by comparing the two lists. The
    size is stated before and after (after is always 0) because the symbol's
    `proposed_order` row, written earlier by DecisionStage, still carries the
    size that was asked for. The detail is the gate's own refusal string
    (`risk.rules.unread_filing_block_reason`), which carries its own prefix
    and is deliberately NOT the conviction bar's: this is missing evidence,
    not a seat's verdict, and entries only — nothing here reads or changes a
    held position. Never raises — a record failure must not stop the
    stage (`_persist_evidence`'s contract).
    """
    try:
        from src.risk.rules import unread_filing_block_reason

        after_symbols = {d.symbol.strip().upper() for d in (after or []) if d is not None and d.action == "BUY"}
        for d in before or []:
            if d is None or d.action != "BUY":
                continue
            if d.symbol.strip().upper() in after_symbols:
                continue
            _record_pipeline_event(
                pipeline,
                ctx,
                d.symbol,
                "deterministic_gate",
                "blocked",
                "queued_earnings_unread_filing",
                gate="queued_earnings_unread_filing",
                before_allocation_pct=d.allocation_pct,
                after_allocation_pct=0.0,
                detail=(f"BUY {d.symbol} REFUSED at {d.allocation_pct:.2f}%: " + unread_filing_block_reason(d.symbol)),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("queued-earnings refusal recording failed: %s", exc)


def _apply_sector_unresolved_alert(data_status: dict, violations: list) -> None:
    """Promote a `sector_unresolved_*` advisory (src/risk/rules.py rule 5)
    into `data_status["sector"]` — the same generic dict `notifier.py` /
    `trader_feed.py` already render as a plain "⚠️ degraded: ..." line in
    the session output, and that output IS the owner's alert (every
    session ends with a Telegram push). Matches the existing pattern
    instead of inventing a new alert channel.

    "degraded" (transient — self-heals) beats "partial" (may genuinely
    have no sector) if a run somehow surfaces both, and never downgrades
    an alert already raised earlier in the same run.
    """
    alerts = [v for v in violations if v.rule.startswith("sector_unresolved")]
    if not alerts:
        return
    status = "degraded" if any(v.rule == "sector_unresolved_lookup_failed" for v in alerts) else "partial"
    if data_status.get("sector") == "degraded":
        status = "degraded"
    data_status["sector"] = status
    logger.warning(
        "Sector cap: unresolved sector affected a trading decision — %s",
        "; ".join(dict.fromkeys(a.message for a in alerts)),
    )


#: The key `AnalysisParseTelemetry.record_dropped_item` records when the
#: malformed row's own symbol could not be read out of it — see
#: `src/agents/tech_analyst.py`, which passes `"?"` for a row whose `key` is
#: not in the submitted set and for a dict with no readable `symbol`.
UNIDENTIFIED_DROP_KEY = "?"


def _reconcile_parse_loss(
    dropped: dict[tuple[str, str], int],
    book_symbols: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Split recorded parse drops into RECOVERED and GENUINELY LOST.

    WHY THIS EXISTS. `parse_telemetry` records a drop the moment a row fails
    to parse and NOTHING un-records it when the retry succeeds — deliberately,
    because a recovered drop is otherwise invisible to the operator while
    still costing a paid LLM round-trip (the reasoning is written out at
    `src.models.AnalysisParseTelemetry.record_dropped_item` and at both record
    sites in `src/agents/tech_analyst.py`). The counter is therefore right and
    stays exactly as it is. What was WRONG was the sentence built from it: the
    advisory told the Risk Manager every recorded drop was "absent from the
    book below".

    Measured, 2026-09-21 `intra_check`: META was held with stops at 14:15:45,
    its row dropped at 14:16:35, META was re-analysed and re-sized by the
    constructor at 14:17:31, and at 14:22:05 the risk stage still reported it
    as discarded and absent. META was in the book. The Risk Manager then
    called the environment degraded on a data-quality red flag that had
    already repaired itself. The falsifiable clause is "absent from the book
    below", not the count, and that clause is what licensed the downgrade.

    So the reconciliation happens HERE, in the risk stage, and nowhere else:
    this is the only place that holds the book the seat is actually shown.
    The tech seat's own `analyses` dict proves a row PARSED; it does not prove
    the Portfolio Manager ranked the name or that the constructor kept it.

    `book_symbols` is the caller's set of symbols visible to the risk seat,
    upper-cased. Anything recorded under `UNIDENTIFIED_DROP_KEY` is counted as
    genuinely lost whatever the book contains: a row whose symbol could not be
    read cannot be matched against anything, and one such entry may aggregate
    several separate malformed rows, so it can never be PROVEN recovered.
    Treating it as lost is the conservative direction — it keeps the advisory
    over-reporting loss rather than under-reporting it.

    READ-ONLY with respect to the telemetry. This function takes a snapshot
    dict, mutates nothing global, and un-records nothing; the tech seat runs
    twice per morning run and research runs five seats in one
    `ThreadPoolExecutor`, all against the single global counter, so a writer
    here would be a new cross-thread hazard. There is none.

    Returns `(recovered, lost)`, each an ordered `{"Model:KEY": count}` map in
    the same display shape `describe_dropped` uses.
    """
    recovered: dict[str, int] = {}
    lost: dict[str, int] = {}
    for (model, key), count in sorted(dropped.items()):
        name = f"{model}:{key}"
        if key != UNIDENTIFIED_DROP_KEY and str(key).upper() in book_symbols:
            recovered[name] = count
        else:
            lost[name] = count
    return recovered, lost


def _parse_loss_advisories(
    dropped: dict[tuple[str, str], int],
    book_symbols: set[str],
    reasons: dict[tuple[str, str], str] | None = None,
) -> list:
    """The parse-loss advisories for this session, reconciled against the book.

    Two entries at most, and they say different things because they ARE
    different things:

      * `analysis_parse_loss` — dropped and still not in the book. Unchanged
        wording, unchanged severity, still the falsifiable "absent from the
        book below" clause, because for these it is true.
      * `analysis_parse_loss_recovered` — dropped, re-asked, and present in
        the book the seat is reading. A cost-and-quality note, NOT a claim of
        missing coverage, and it never says the symbol is absent.

    Both are raised, so the operator keeps the cost signal the counter exists
    to give and the seat is told the truth rather than told less.
    """
    from src.risk.rules import RiskViolation as _RV

    recovered, lost = _reconcile_parse_loss(dropped, book_symbols)
    # Board item 158: name each lost row WITH its recorded reason, not just
    # the symbol. `reasons` is keyed (model, key); re-key to the "Model:KEY"
    # display string `_reconcile_parse_loss` produces so the advisory the RM
    # reads carries the why, matching the row now persisted to the DB.
    reason_by_name = {f"{model}:{key}": why for (model, key), why in (reasons or {}).items()}

    def _with_reason(names) -> str:
        return ", ".join(f"{name} ({reason_by_name[name]})" if name in reason_by_name else name for name in names)

    out: list = []
    if lost:
        n_lost = sum(lost.values())
        out.append(
            _RV(
                rule="analysis_parse_loss",
                message=(
                    f"{n_lost} item(s) were discarded at parse this session "
                    f"and are absent from the book below: {_with_reason(lost)} "
                    f"(TechAnalysisResult = a candidate PM never saw; "
                    f"TargetPosition = a position PM asked for and the desk "
                    f"could not read). The plan was therefore built from, or "
                    f"reduced to, a SMALLER set than the seats produced — "
                    f"treat a thin list as possibly truncated rather than as a "
                    f"genuine absence of setups. An entry keyed "
                    f"`{UNIDENTIFIED_DROP_KEY}` is a row whose own symbol could "
                    f"not be read, so it cannot be matched against the book and "
                    f"is counted here."
                ),
                value=float(n_lost),
                limit=0.0,
            )
        )
    if recovered:
        n_recovered = sum(recovered.values())
        out.append(
            _RV(
                rule="analysis_parse_loss_recovered",
                message=(
                    f"{n_recovered} item(s) failed to parse and were RECOVERED by "
                    f"a retry: {', '.join(recovered)}. These names ARE in the book "
                    f"below — this is a cost and data-quality note, not missing "
                    f"coverage, and no name is missing from the book because of "
                    f"it. Do not treat it as degraded input: each one cost an "
                    f"extra paid model round-trip, which is what is worth "
                    f"reporting."
                ),
                value=float(n_recovered),
                limit=0.0,
            )
        )
    return out


#: `kind` for the per-stock parse-drop row written to `specialist_evidence`.
#: Board item 158: the reason a symbol was dropped at parse used to live only
#: in a log line and an aggregate count, so a later reader could not tell WHY
#: a name was absent without the rotated log. One row per dropped symbol is now
#: filed here, tied to that symbol and this run. Deliberately NOT
#: `kind='pipeline_event'`: the jam detector (`src/refusal_signature.py`) reads
#: every symbol-scoped `pipeline_event` row as "this session considered that
#: stock as a new idea", and a parse drop is not one — same reasoning as
#: `src/execution/exit_path_records.py`.
#:
#: An ALIAS of `src.models.ANALYSIS_DROP_KIND`, not a second literal: the
#: read-only API must name the same kind and may not import this module
#: (`tests/test_api_safety.py`), so the string has exactly one home.
ANALYSIS_DROP_KIND = _ANALYSIS_DROP_KIND


def _persist_dropped_reasons(
    db: Any,
    run_id: str | None,
    dropped: dict[tuple[str, str], int],
    reasons: dict[tuple[str, str], str],
    book_symbols: set[str],
    codes: dict[tuple[str, str], str] | None = None,
) -> int:
    """File one `specialist_evidence` row per dropped symbol, WITH its reason.

    This is the board-item-158 fix: the drop reason is stored alongside the
    stock it was dropped for (queryable by `symbol` + `run_id`), not only in
    the log. Rows whose own symbol could not be read (`UNIDENTIFIED_DROP_KEY`)
    are skipped — there is no stock to file them against.

    Each row carries BOTH a stable `reason_code` (one of
    `src.models.ANALYSIS_DROP_CODES`) and the human `reason`. `count` is taken
    from the `dropped` tally itself rather than re-derived, so the per-row
    reason and the aggregate count cannot disagree: they are the same numbers
    read from the same snapshot in the same pass.

    OBSERVABILITY ONLY. Never raises: a record that cannot be written must
    never change the risk decision it is recording. `recovered` marks whether
    the symbol reached the book despite the drop (a retry succeeded), the same
    split `_reconcile_parse_loss` makes for the advisory. Returns the number of
    rows written, for callers/tests.
    """
    if db is None or not dropped or not run_id:
        return 0
    import json as _json

    written = 0
    for (model, key), count in sorted(dropped.items()):
        if key == UNIDENTIFIED_DROP_KEY:
            continue
        symbol = str(key).strip().upper()
        if not symbol:
            continue
        recovered = symbol in book_symbols
        payload = {
            "stage": "analysis",
            "outcome": "recovered" if recovered else "dropped",
            "model": model,
            "reason_code": (codes or {}).get((model, key)) or DROP_CODE_UNSPECIFIED,
            "reason": reasons.get((model, key)) or "reason not recorded",
            "count": int(count),
            "recovered": recovered,
        }
        try:
            db.insert_specialist_evidence(
                run_id=str(run_id),
                agent_name="pipeline",
                kind=ANALYSIS_DROP_KIND,
                scope="symbol",
                symbol=symbol,
                evidence_json=_json.dumps(payload, sort_keys=True, default=str),
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 — a record is never authority
            logger.warning(
                "analysis-drop record for %s could not be written: %s",
                symbol,
                exc,
            )
    return written
