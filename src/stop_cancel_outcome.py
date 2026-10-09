"""The outcome of cancelling a set of protective stops.

WHY THIS TYPE EXISTS (and why it refuses to be a bool).

``cancel_snapshotted_stops`` cancels the protective stops covering a
position so a SELL has free shares to work with. When one cancel fails it
rolls the already-cancelled ones back. That rollback is itself best-effort:
``_restore_stop_orders`` can come back with failed specs (broker rejection,
a dead order status, a kill-switch refusal). When it does, those shares are
cancelled at the broker and NOT re-covered — the position ends the call with
LESS protection than it started with.

The old signature was ``bool``. ``False`` meant both

  * "nothing moved, every stop is still resting" (benign — the SELL is
    skipped and the position is exactly as protected as before), and
  * "coverage SHRANK and some shares are now naked" (a live-money hole),

and every caller read it as the first. Two of them then DELETED the
write-ahead recovery row on that ``False`` — discarding the only durable
record that could have repaired the hole — because "the stops never left
the broker" was assumed, not established.

So this type deliberately raises on ``bool()``. A caller cannot collapse a
protection outcome to a truth value by accident; it has to ask which of the
two states it is in. That is the mechanical reason the class of defect is
gone rather than this one instance of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _qty(spec: dict) -> float:
    try:
        return abs(float(spec.get("qty", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _total_qty(specs: tuple[dict, ...]) -> float:
    return sum(_qty(s) for s in specs)


@dataclass(frozen=True)
class StopCancelOutcome:
    """What is, and is not, still protected after a cancel attempt.

    Every spec in ``requested`` lands in exactly one of three buckets:

    ``cancelled``      cancelled at the broker and still meant to be gone
                       (the SELL may proceed over these shares).
    ``still_resting``  the cancel failed, or the rollback put it back —
                       the stop is alive at the broker, the shares are
                       covered, and the SELL must not proceed over them.
    ``unprotected``    cancelled at the broker AND the rollback could not
                       put it back. These shares are naked right now.
    """

    symbol: str
    requested: tuple[dict, ...] = ()
    cancelled: tuple[dict, ...] = ()
    still_resting: tuple[dict, ...] = ()
    unprotected: tuple[dict, ...] = ()
    detail: str = ""
    _never: bool = field(default=False, repr=False, compare=False)

    # --- the three questions a caller is allowed to ask -------------------

    @property
    def cleared(self) -> bool:
        """True iff every requested stop was cancelled and none is naked.

        This is the only state in which a SELL may proceed.
        """
        return not self.still_resting and not self.unprotected

    @property
    def coverage_shrank(self) -> bool:
        """True iff shares that were protected on entry are naked on exit."""
        return bool(self.unprotected)

    @property
    def unprotected_qty(self) -> float:
        return _total_qty(self.unprotected)

    @property
    def covered_qty(self) -> float:
        return _total_qty(self.still_resting)

    def summary(self) -> str:
        return (
            f"{self.symbol}: requested={len(self.requested)} "
            f"cancelled={len(self.cancelled)} "
            f"still_resting={len(self.still_resting)} "
            f"UNPROTECTED={len(self.unprotected)} "
            f"(qty {self.unprotected_qty:g})" + (f" — {self.detail}" if self.detail else "")
        )

    # --- the forcing function --------------------------------------------

    def __bool__(self) -> bool:
        raise TypeError(
            "StopCancelOutcome is not a boolean: 'the cancel did not go "
            "through' and 'the position lost stop coverage' are different "
            "answers and the old bool conflated them. Ask .cleared to decide "
            "whether the SELL may proceed, and .coverage_shrank / "
            ".unprotected to decide whether a recovery row must be kept. "
            f"({self.summary()})"
        )

    # --- constructors -----------------------------------------------------

    @classmethod
    def all_cleared(cls, symbol: str, specs=()) -> "StopCancelOutcome":
        s = tuple(specs or ())
        return cls(symbol=symbol, requested=s, cancelled=s)

    @classmethod
    def nothing_to_do(cls, symbol: str) -> "StopCancelOutcome":
        return cls(symbol=symbol)


class StopCoverageLost(RuntimeError):
    """Raised where a caller's return type has no room to say that shares
    lost their stop coverage. Carries the outcome so the handler can see
    exactly which specs are naked.

    Better a loud exception than a ``False`` that reads as "nothing moved":
    the one thing the caller must not do is proceed as if the position were
    still protected.
    """

    def __init__(self, outcome: StopCancelOutcome):
        self.outcome = outcome
        super().__init__("protective stop coverage shrank and could not be rolled back: " + outcome.summary())


def settle_cancel(symbol, specs, cancelled, untouched, cancel_failed, restore, log):
    """Turn the raw results of a cancel loop into a :class:`StopCancelOutcome`.

    The SDK call itself stays inside the gated broker; this is the
    bookkeeping that the old code did not do — roll back what was cancelled
    and record which specs the rollback could NOT put back, because those
    shares are naked and nothing else in the system was told.
    """
    if not cancel_failed and not untouched:
        log.info("Cancelled %d protective stop(s) for %s", len(cancelled), symbol)
        return StopCancelOutcome.all_cleared(symbol, tuple(specs))
    rollback_failed, restored = [], []
    if cancelled:
        try:
            _n, rollback_failed = restore(symbol, cancelled)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "cancel_snapshotted_stops: rollback RAISED for %s (%s) — treating every cancelled stop as unrestored.",
                symbol,
                exc,
            )
            rollback_failed = list(cancelled)
        lost = {id(s) for s in rollback_failed}
        restored = [s for s in cancelled if id(s) not in lost]
    outcome = StopCancelOutcome(
        symbol=symbol,
        requested=tuple(specs),
        cancelled=(),
        still_resting=tuple(cancel_failed) + tuple(untouched) + tuple(restored),
        unprotected=tuple(rollback_failed),
        detail=f"{len(cancel_failed)}/{len(specs)} cancel(s) failed"
        + (f", {len(untouched)} spec(s) had no order id" if untouched else ""),
    )
    if outcome.coverage_shrank:
        log.critical(
            "cancel_snapshotted_stops: %s LOST STOP COVERAGE — %s. The "
            "SELL will not proceed; the recovery row for the unprotected "
            "specs must be kept so the drain re-attaches them.",
            symbol,
            outcome.summary(),
        )
    else:
        log.warning(
            "cancel_snapshotted_stops: %s — rolled back cleanly, every share still covered; SELL won't proceed.",
            outcome.summary(),
        )
    return outcome


def keep_recovery_row(db, wal_row_id, cancel, log, label):
    """KEEP the write-ahead recovery row when coverage shrank.

    The mirror of discharging it: discharge when every share is still
    covered, keep when some are not. The row is left WHOLE on purpose — the
    drain restores it with ``check_idempotency=True``, which skips the specs
    that are still alive at the broker, so reality narrows the row at repair
    time instead of our bookkeeping narrowing it now (and being wrong if a
    stop was cancelled or filled in between). No new write, nothing stored.
    """
    log.critical(
        "%s PROTECTION LOST: %s — the action is abandoned and the "
        "recovery row is KEPT so the drain re-attaches the missing "
        "stop(s).",
        label,
        cancel.summary(),
    )
    if wal_row_id is None:
        log.critical(
            "%s PROTECTION LOST: no recovery row — %.6g share(s) of "
            "stop coverage are gone with nothing persisted to restore "
            "them; the coverage reconcile is the only repair left.",
            label,
            cancel.unprotected_qty,
        )


def handle_add_cancel(db, prep, cancel, logger, discharge):
    """Decide what a scale-in add does with its stop-cancel outcome.

    True  -> cleared, the add may proceed.
    False -> the add is abandoned; ``prep`` carries why. When coverage
             SHRANK the recovery row is kept (narrowed to the naked specs)
             instead of discharged, because it is the only durable intent
             that can re-attach them.
    """
    if cancel.cleared:
        return True
    if cancel.coverage_shrank:
        keep_recovery_row(db, prep.wal_row_id, cancel, logger, "scale-in")
        prep.skip_reason = "scale_in_coverage_shrank"
        prep.skip_detail = "stop coverage SHRANK on the pre-add cancel: " + cancel.summary()
        return False
    discharge(db, prep.wal_row_id)
    prep.wal_row_id = None
    prep.skip_reason = "scale_in_stop_clear_failed"
    prep.skip_detail = "the protective stop could not be cancelled (rolled back)"
    return False


def keep_lost_coverage_row(pipe, symbol, wal_row_id, cancel, logger):
    """Pipeline side of the same rule; True once the row has been kept."""
    keep_recovery_row(pipe.db, wal_row_id, cancel, logger, symbol)
    pipe._last_stop_clear_refusal = "coverage_shrank"
    return True
