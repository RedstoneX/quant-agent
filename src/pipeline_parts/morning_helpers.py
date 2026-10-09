import logging

from src.sessions.name_coverage_session import NameCoverageRecordSession
from src.trading_calendar import session_date_key

logger = logging.getLogger(__name__)


def _record_name_coverage(pipeline, ctx, _record) -> None:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/name_coverage_session.py)."""
    return NameCoverageRecordSession(
        config=pipeline._collab("config"),
    ).run(ctx, _record)


def _attach_evidence_freshness(pipeline, result) -> None:
    """Carry this run's evidence-freshness disclosure out to the owner.

    Owner mandate 2026-09-18 made every seat but the technical one
    advisory, so a decision can now rest on ONE freshly-read seat plus a
    carried-forward book — and every carried seat reports green. The
    disclosure is computed once, by the evidence gate, on the single
    path every decision passes through; this hands it to the message
    renderer and to the durable session report.

    Disclosure only. It states how much was read on this tick; it never
    judges the count and there is no minimum — that number is the
    owner's (docs/WORK.md item 20). Fail-soft: a problem here costs the
    disclosure line, never the session.
    """
    if not isinstance(result, dict):
        return
    record = getattr(pipeline, "_last_evidence_freshness", None)
    if isinstance(record, dict) and "evidence_freshness" not in result:
        result["evidence_freshness"] = dict(record)
    # A lost ADVISORY seat no longer halts the run, so the one alert
    # that cannot be silenced by a mode's noise policy
    # (`notifier.maybe_alert_data_quality`, fired from main.py's finally
    # block) must be able to see it. It reads `result["data_status"]`,
    # which the intra_check result paths never carried — before the
    # mandate change they did not have to, because a lost seat there
    # halted the run instead.
    status = getattr(pipeline, "_last_decision_data_status", None)
    if isinstance(status, dict) and status and "data_status" not in result:
        result["data_status"] = dict(status)


def _record_account_snapshot(pipeline, total_value, last_equity) -> None:
    """Remember the account read this session already made, so the P&L
    block can be built from it on EVERY return path.

    The session takes exactly one broker account snapshot and then may
    leave by any of a dozen returns (no_trades, pm_agent_failure,
    paid_analysis_suspended, executed, ...). Before 2026-09-23 only the
    position-review and intra-check happy paths bothered to carry the
    P&L keys out, so the morning message the owner actually reads said
    "not available" while the same message printed the book it had just
    read. Recording the snapshot here, and attaching in the wrapper,
    makes the figure a property of "the account was read", not of which
    exit the run happened to take.
    """
    try:
        pipeline._last_account_snapshot = (
            float(total_value),
            float(last_equity),
        )
    except (TypeError, ValueError):
        pipeline._last_account_snapshot = None


def _attach_pnl(pipeline, result) -> None:
    """Fill the owner-facing P&L keys from this run's own account read.

    SAME basis and SAME source as the path that already worked — the
    broker's day-over-day change against `last_equity`, and
    `_total_pnl_since_reset` for the dated baseline (see
    `trader_feed._pnl_section_lines` for why "total" is dated). No
    second way to compute P&L is introduced here, and nothing is
    computed where the account was not read: a run with no snapshot
    sets the REASON instead, so the message can say something true.

    Never overwrites a figure a body already set, and never raises — a
    P&L fault must not cost the push.
    """
    if not isinstance(result, dict):
        return
    keys = (
        "daily_pnl",
        "daily_return_pct",
        "total_pnl",
        "total_return_pct",
        "total_pnl_since",
    )
    if any(k in result for k in keys):
        return
    snapshot = getattr(pipeline, "_last_account_snapshot", None)
    if not snapshot:
        result.setdefault(
            "pnl_unavailable_reason",
            "ended_before_account_read",
        )
        return
    try:
        total_value, last_equity = snapshot
        if last_equity > 0:
            daily_pnl = total_value - last_equity
            result["daily_pnl"] = daily_pnl
            result["daily_return_pct"] = daily_pnl / last_equity * 100
        total_pnl, total_return_pct, total_pnl_since = pipeline._total_pnl_since_reset(total_value)
        if total_pnl is not None:
            result["total_pnl"] = total_pnl
            result["total_return_pct"] = total_return_pct
            result["total_pnl_since"] = total_pnl_since
    except Exception as exc:  # noqa: BLE001 — never break the push
        logger.warning("P&L attach failed (non-fatal): %s", exc)
    if not any(k in result for k in keys):
        # The account WAS read; what is missing is a usable prior close
        # (and no dated baseline row exists either). Say that, rather
        # than claiming an account read that demonstrably happened did
        # not.
        result.setdefault("pnl_unavailable_reason", "no_prior_close")


def _persist_session_report(pipeline, mode: str, result: dict) -> None:
    """Write a morning/midday/close result dict verbatim, keyed by
    trading day + mode. No field is defaulted or filled in here.
    """
    if not isinstance(result, dict):
        return
    try:
        pipeline.db.save_session_report(
            mode=mode,
            date=session_date_key(),
            run_id=result.get("run_id"),
            payload=result,
        )
    except Exception as exc:  # noqa: BLE001 — never break the push
        logger.warning(
            "%s report persistence failed (non-fatal): %s",
            mode,
            exc,
        )


def run_morning(pipeline) -> dict:
    """The morning session, plus the durable record of its own output.

    `leverage` (the §11.2 gross-ceiling snapshot) and
    `stop_coverage_gaps` (the broker-truth stop audit) are computed
    fresh from live broker state every call and, before this wrapper,
    were handed to the notifier and dropped — no other durable
    table holds them (unlike PM/RM reasoning and orders, already kept
    via `specialist_evidence`/`trades`). The body below is unchanged;
    this wrapper persists EVERY return path so the message can be
    re-read without paying for a fresh run. Fail-soft — a storage
    problem costs the audit record, never the morning push.
    """
    pipeline._last_evidence_freshness = None
    pipeline._last_account_snapshot = None
    result = pipeline._run_morning_body()
    pipeline._attach_pnl(result)
    pipeline._attach_evidence_freshness(result)
    pipeline.admission._attach_universe_changes(result)
    pipeline._persist_session_report("morning", result)
    return result
