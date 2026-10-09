import logging

from src.sessions.termination import SessionTerminated

logger = logging.getLogger(__name__)


def _install_sigterm_unwind(pipeline, context: str):
    """Make the wrapper's SIGTERM raise instead of killing silently.

    Returns whatever handler was installed before, for the caller to
    restore. Returns None — and changes nothing — when signals cannot be
    set here (not the main thread, or a platform without SIGTERM), which
    is the ordinary case under pytest's worker threads.
    """
    import signal

    try:
        return signal.signal(
            signal.SIGTERM,
            lambda *_: (_ for _ in ()).throw(SessionTerminated(f"{context}: SIGTERM from the run wrapper")),
        )
    except (ValueError, OSError, AttributeError, RuntimeError) as exc:
        logger.debug("SIGTERM unwind not installed for %s: %s", context, exc)
        return None


def _repair_stops_on_kill(pipeline, context: str) -> None:
    """FIRST act of a SIGTERM unwind: ADD the stops a kill left owed.

    Execution sends every buy, then places stops in a second loop, so a
    kill between the two leaves a filled buy with no stop until the next
    scheduled coverage check, ~25-30 minutes later.

    ADD-ONLY, deliberately not the session-start `_reconcile_stop_coverage`:
    that pass first applies owed stop levels, which can cancel a good stop
    and resubmit it, and the SIGKILL 30s later can land between the two
    and leave a position with no stop that had one. This runs only the
    coverage sweep's gap repair (`uncovered_positions` +
    `replace_missing_stops`): it re-reads the broker's resting stops per
    name, places only the shortfall, and never cancels or replaces.

    Time is NOT bounded and no cap is invented. Clean passes measured a
    max of 4.7s in production; passes that place stops are unmeasured and
    unbounded (per-name reads, up to three submits with pauses, 30s HTTP
    timeouts). Stops go first on purpose: because the repair only adds,
    a SIGKILL mid-repair loses only the stops not yet placed, which the
    next scheduled check covers, and never removes one. A failure is
    logged with its traceback and the rest of the unwind still runs.
    """
    logger.warning("%s: SIGTERM unwind — adding owed stops first (add-only)", context)
    try:
        outcomes = pipeline._add_missing_stops()
    except Exception:  # noqa: BLE001 — logged with traceback; the unwind must go on
        logger.exception(
            "%s: SIGTERM unwind stop-coverage repair FAILED — continuing the unwind",
            context,
        )
    else:
        pipeline._report_kill_repair(context, outcomes)


def _report_kill_repair(context: str, outcomes: list) -> None:
    """Each stop the kill repair could not add is an error, by name."""
    for o in outcomes:
        if not o.placed:
            logger.error(
                "%s: SIGTERM unwind could NOT add the stop owed on %s (%.4f): %s",
                context,
                o.symbol,
                o.qty,
                o.detail,
            )
    logger.warning(
        "%s: SIGTERM unwind placed %d of %d owed stop(s)",
        context,
        sum(1 for o in outcomes if o.placed),
        len(outcomes),
    )


def _add_missing_stops(pipeline) -> list:
    """The coverage sweep's add-only gap repair, against this desk's book."""
    from src.coverage_watchdog import replace_missing_stops, uncovered_positions

    pending = {r.get("symbol") for r in pipeline.db.get_pending_protection_restores()}
    sweeper = pipeline._sweeper()
    sweep_symbol = sweeper.symbol if sweeper is not None else pipeline._retired_cash_park_symbol()
    gaps, error = uncovered_positions(
        pipeline.broker,
        sweep_symbol=sweep_symbol,
        skip_symbols=pending,
        db=pipeline.db,
    )
    if error:
        raise RuntimeError(error)
    return replace_missing_stops(
        pipeline.broker,
        gaps,
        sweep_symbol=sweep_symbol,
        db=pipeline.db,
        last_buy=lambda sym, action="BUY": pipeline.db.get_symbol_last_buy(
            sym,
            include_in_flight=True,
            action=action,
        ),
    )


def _restore_sigterm(pipeline, previous) -> None:
    if previous is None:
        return
    import signal

    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, AttributeError, RuntimeError):
        pass
