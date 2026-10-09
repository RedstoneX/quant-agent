"""Make the evening session's broad catch-alls LOUD, without changing them.

Each handler keeps swallowing exactly what it swallowed before. What changes
is what it records: the full traceback at ERROR plus one counted row on the
existing reconciliation channel (same one ``src.sentinel.guarded`` uses).

Three states stay distinct: no row means the step was never reached, an
``agreed`` row means it ran and swallowed nothing, a ``disagreed`` row means it
ran and swallowed a fault. When the session holds no ledger handle the
traceback is still logged and the row is skipped.
"""

from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.sessions.evening_session")


def record_evening_pass(session, where: str, exc: BaseException | None = None) -> None:
    """Record ONE pass through an evening catch-all; never raises."""
    try:
        record_guarded_outcome(
            db=getattr(session, "_db", None),
            where=f"evening.{where}",
            exc=exc,
            log=logger,
        )
    except Exception:  # noqa: BLE001
        logger.error("record_evening_pass could not record %s", where, exc_info=True)


def run_evening_housekeeping(session) -> None:
    """Prune old rows and files; each prune is isolated and counted."""
    # Housekeeping: drop agent_logs older than 2 years (full_response bloats the DB
    # but 730 days supports quarter-over-quarter learning), and trades older than
    # 5 years (keep a long audit tail but bound it).
    try:
        pruned = session._db.prune_agent_logs(keep_days=730)
        if pruned:
            logger.info("Pruned %d old agent_log rows", pruned)
    except Exception as e:
        record_evening_pass(session, "prune_agent_logs", e)
    else:
        record_evening_pass(session, "prune_agent_logs")
    try:
        pruned_t = session._db.prune_trades(keep_days=365 * 5)
        if pruned_t:
            logger.info("Pruned %d trades older than 5 years", pruned_t)
    except Exception as e:
        record_evening_pass(session, "prune_trades", e)
    else:
        record_evening_pass(session, "prune_trades")
    # Stage 4 (QAMC): specialist_evidence is forensic display detail for
    # the same agent calls agent_logs already prunes — same 730-day
    # retention, same never-block-housekeeping discipline.
    try:
        pruned_se = session._db.prune_specialist_evidence(keep_days=730)
        if pruned_se:
            logger.info("Pruned %d old specialist_evidence rows", pruned_se)
    except Exception as e:
        record_evening_pass(session, "prune_specialist_evidence", e)
    else:
        record_evening_pass(session, "prune_specialist_evidence")
    # Stale orphaned protection-restore rows accumulate when a
    # sell_order_id becomes unqueryable (broker GC) or position
    # gets liquidated by another path. Drain can't make progress on
    # them; 30d cutoff bounds the operational noise.
    try:
        pruned_p = session._db.prune_pending_protection_restores(keep_days=30)
        if pruned_p:
            logger.info("Pruned %d stale pending_protection_restores rows", pruned_p)
    except Exception as e:
        record_evening_pass(session, "prune_protection_restores", e)
    else:
        record_evening_pass(session, "prune_protection_restores")
    try:
        pruned_rp = session._db.prune_pending_repegs(keep_days=30)
        if pruned_rp:
            logger.info("Pruned %d stale pending_repegs rows", pruned_rp)
    except Exception as e:
        record_evening_pass(session, "prune_repegs", e)
    else:
        record_evening_pass(session, "prune_repegs")
    # File-store housekeeping: the news dated dirs + narrative backups grow
    # unbounded (the DB side prunes; the file-stores didn't). Nothing reads
    # news artifacts older than ~14 days, so 1000d is very safe headroom.
    try:
        pruned_n = session._news_store.prune(keep_days=1000)
        if pruned_n:
            logger.info("Pruned %d dated news artifact(s)", pruned_n)
    except Exception as e:
        record_evening_pass(session, "prune_news_store", e)
    else:
        record_evening_pass(session, "prune_news_store")
    try:
        pruned_e = session._earnings_provider.prune(keep_days=1000)
        if pruned_e:
            logger.info("Pruned %d old raw earnings filing(s)", pruned_e)
    except Exception as e:
        record_evening_pass(session, "prune_earnings_files", e)
    else:
        record_evening_pass(session, "prune_earnings_files")
