"""The existing-open-stops scan of the residual re-protection, lifted verbatim out of
`ReprotectResidual._reprotect_residual_after_partial_sell` (2026-10-09, ceiling split).

The loop below is the method's `for o in existing or []` loop, character for character,
dedented one level. Its `continue`s are unchanged. Each `return X` inside it became
`return ScanOutcome(done=True, value=X)`, and the caller returns `value` unchanged when
`done` is set; falling out of the loop returns `done=False` and the caller carries on to
the submit exactly as before. `self` is the calling `ReprotectResidual` instance.
"""

import logging

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ScanOutcome:
    """Whether the scan ended the method (`done`) and, if so, the bool it returned (`value`)."""

    def __init__(self, *, done: bool, value: bool = False) -> None:
        self.done = done
        self.value = value


def scan_existing_stops(
    self,
    *,
    symbol,
    residual_qty,
    existing,
    cancelled_ids,
    identity_unprovable,
    best_stop,
    side,
) -> ScanOutcome:
    """Run the existing-open-stops loop for `self` (a ReprotectResidual); see the module docstring."""
    from src.execution.broker import (
        PROTECTIVE_ORDER_ACTIVE_STATUSES as _ACTIVE_STATUSES,
        PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES as _IN_FLIGHT_STATUSES,
        real_broker_order_id as _real_order_id,
    )

    for o in existing or []:
        try:
            existing_sp = float(getattr(o, "stop_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        # MEASURED against the broker 2026-09-30 on the separate
        # rehearsal account (id deliberately not written down: this
        # repository is public): immediately after a cancel the dead
        # stop is STILL LISTED with status "new", and Alpaca ACCEPTS a
        # second stop placed in that window. So a duplicate is not theoretical
        # and nothing here reconciles one.
        #
        # PRICE DOES NOT PARTICIPATE IN THIS DECISION AT ALL (adversary
        # round 2, defects 1 and 3). The old check compared the resting
        # trigger with the wanted one inside a half-penny window, which
        # made a prior attempt's stop that landed a cent away invisible
        # and put a second live stop on the same shares -- the
        # incident's own root filter. The window also required a
        # tolerance constant, and the only justification ever offered
        # for its size was an ASSERTED float<->Decimal round-trip that
        # nobody measured. Two honest attempts to derive it failed: the
        # production database at /home/qamc/quant-agent/data holds no
        # record of a broker-returned trigger beside the submitted one
        # (nothing stores the pair), and the rehearsal account is not
        # usable for it here because this task forbids placing orders.
        # Rather than guess the number, the need for it is REMOVED:
        # identity, status and quantity decide, and the price that is
        # recorded is the one actually resting at the broker.
        # DEFECT 1 (adversary round 3). `if existing_sp <= 0: continue`
        # used the TRIGGER PRICE as a presence test: a stop whose price
        # read as zero or negative was treated as though no order
        # existed at all, and the run submitted a second stop over it --
        # while a stop reading one cent was banked as full protection.
        # An unreadable price says nothing about whether an order is
        # resting on these shares. Presence, identity, status and
        # quantity decide; the price is consulted only at the point it
        # is actually needed, which is the write-back below.
        order_id = _real_order_id(getattr(o, "id", None))
        status_attr = getattr(o, "status", None)
        status = str(getattr(status_attr, "value", status_attr) or "").lower()
        if not order_id:
            logger.warning(
                "Reprotect for %s will SUBMIT: an open stop at $%.2f "
                "carries no readable order id, so it cannot be "
                "distinguished from the stop this run just cancelled.",
                symbol,
                existing_sp,
            )
            continue
        if order_id in cancelled_ids:
            logger.warning(
                "Reprotect for %s will SUBMIT: the open stop at $%.2f "
                "(order %s) is one THIS run just cancelled and is still "
                "being listed as open — not a prior successful attempt. "
                "Skipping here is what leaves the position naked.",
                symbol,
                existing_sp,
                order_id,
            )
            continue
        # QUANTITY, not just existence. The coverage sweep compares
        # covered_qty against held_qty and never reads stop_price
        # (grep: zero references), so a leftover 1-share sliver stop
        # from the fractional stop-repair path would otherwise satisfy
        # this check for a whole position and be reported as covered
        # forever. An order that does not cover the residual is not
        # this position's protection.
        try:
            existing_qty = abs(float(getattr(o, "qty", 0) or 0))
        except (TypeError, ValueError):
            existing_qty = 0.0
        if existing_qty + 1e-9 < float(residual_qty):
            logger.warning(
                "Reprotect for %s: an open stop (order %s) at $%.2f "
                "covers only %s of the %s residual shares, so it is "
                "not this position's protection and does not make "
                "this run idempotent.",
                symbol,
                order_id,
                existing_sp,
                self._format_qty(existing_qty),
                self._format_qty(residual_qty),
            )
            continue
        if status in _IN_FLIGHT_STATUSES:
            # DEFECT 2, adversary round 2. `pending_new` is a stop the
            # broker has received and not yet routed, and it can still
            # go to `rejected`. Counting it as protection drained the
            # recovery intent and wrote a stop price into the desk's
            # own record that the broker may refuse seconds later, with
            # only the coverage sweep to notice -- and that sweep
            # DEFERS repair while a trading session holds the lock,
            # which is precisely when this path runs. Neither banking
            # it nor placing a second stop over it is safe, so this
            # returns False WITHOUT a write-back: the caller keeps the
            # persisted recovery intent, and the next pass reads a
            # status that has resolved one way or the other. No timeout
            # and no retry count is invented to close the window; the
            # surviving intent is what closes it.
            logger.warning(
                "Reprotect for %s is NOT complete: a stop from a "
                "previous attempt (order %s) is still %s at $%.2f — "
                "received by the broker but not yet working, and a "
                "%s order can still be rejected. Not recording it as "
                "protection and not placing a second stop over it; "
                "the recovery intent stays alive so the next pass "
                "re-reads it.",
                symbol,
                order_id,
                status,
                existing_sp,
                status,
            )
            self._record_reprotect_identity_gap(
                symbol,
                f"a previous attempt's stop (order {order_id}) was "
                f"still {status} at ${existing_sp:.2f}; the recovery "
                f"intent was kept rather than banked as protection.",
            )
            # DEFECT 3 (adversary round 3), corrected in round 4.
            # Every other branch that leaves this method with the
            # residual unprotected pages the owner; this one did not,
            # and the only thing standing between the position and no
            # protection at all was a log line. It first reused the
            # NO-STOP-AT-ALL page, which was untrue here and which
            # consumed that page's per-symbol daily claim, so a later
            # REJECTION of this very order would have gone unreported.
            # It now has its own message, saying what is actually true,
            # under its own claim key.
            self._alert_owner_stop_pending_acceptance(
                symbol,
                self._format_qty(residual_qty),
                order_id,
                status or "unknown",
                existing_sp,
            )
            return ScanOutcome(done=True, value=False)
        if status not in _ACTIVE_STATUSES:
            logger.warning(
                "Reprotect for %s will SUBMIT: the open stop at $%.2f "
                "(order %s) is in status %r, not a live protective "
                "state — a dying order is not coverage.",
                symbol,
                existing_sp,
                order_id,
                status or "unknown",
            )
            continue
        # DEFECT 1, second half. The order is identity-proven, live and
        # covers the residual, so the position IS protected and a second
        # stop must NOT be submitted over it. But the trigger could not
        # be read, so there is no honest number to write into the desk's
        # own record -- writing $0.00 is the fabricated-stop defect the
        # owner-message audit already recorded. Bank nothing, submit
        # nothing, keep the recovery intent so the next pass re-reads.
        if existing_sp <= 0:
            logger.error(
                "Reprotect for %s is NOT complete: a live stop from a "
                "previous attempt (order %s, status %s) rests over the "
                "residual, but its trigger price read as %r. Not "
                "submitting a second stop over a live one, and not "
                "recording a trigger this desk cannot read. The "
                "recovery intent stays alive.",
                symbol,
                order_id,
                status or "unknown",
                existing_sp,
            )
            self._record_reprotect_identity_gap(
                symbol,
                f"a live stop (order {order_id}) rests over the "
                f"residual but its trigger price was unreadable; "
                f"nothing was banked and the recovery intent was kept.",
            )
            self._alert_owner_unreadable_stop(
                [
                    {
                        "symbol": symbol,
                        "held_qty": residual_qty,
                        "read_error": (
                            f"a live protective stop (order {order_id}, status "
                            f"{status or 'unknown'}) rests at the broker but its "
                            f"trigger price could not be read"
                        ),
                    }
                ]
            )
            return ScanOutcome(done=True, value=False)
        # FAULT 5 (adversary round 4). This bail-out used to run
        # BEFORE the unreadable-price branch below, so an open stop that
        # was BOTH unreadable and unprovable fell straight through to
        # the submit at the bottom: two stops resting on the same
        # shares, neither with a price this desk can read, and nothing
        # anywhere that reconciles a duplicate. Price is checked first,
        # because "a live order rests here and I cannot read it" is a
        # reason to place nothing whatever its identity turns out to be.
        if identity_unprovable:
            logger.warning(
                "Reprotect for %s will SUBMIT over an open, live stop "
                "(order %s, status %s) at $%.2f: this run could not "
                "prove the stop is not one it just cancelled, so it "
                "may not be banked as protection. The ambiguity is on "
                "the per-symbol refusal record.",
                symbol,
                order_id,
                status or "unknown",
                existing_sp,
            )
            continue
        # DEFECT 2 (adversary round 3) -- CONSCIOUSLY LEFT OPEN, not
        # missed. The objection is real: an identity-proven stop is
        # banked as this position's protection at ANY level, so a stop
        # resting looser than the one this run derived lets the position
        # lose more than the desk decided. A level test was written and
        # then REVERTED, because the repository's own specification
        # forbids it: `tests/test_reprotect_cancelled_stop_identity.py`
        # ::test_identity_decides_even_when_the_price_differs requires a
        # one-cent-looser identity-proven stop to be banked, with the
        # RESTING trigger recorded. That test exists because a
        # half-penny price window was the exact filter that produced the
        # 2026-09-30 naked incident, and any level comparison -- with a
        # tolerance or without -- puts price back into a decision the
        # incident proved it must stay out of. Refusing instead would
        # trade a too-wide stop for no stop, which is strictly worse.
        # Closing this properly means AMENDING the resting stop to the
        # wanted trigger in place (the measured-safe mechanism PR #806
        # landed; a refused amend leaves the original resting), not
        # refusing to bank it. That is a change to the specification and
        # to the test, so it belongs to its own item with the owner's
        # sight of it -- not to a defect sweep. Until then the warning
        # below is the only record, and it says in terms that the
        # coverage sweep will not correct the level.
        # Identity-proven, live and covering the residual: this is a
        # PREVIOUS attempt's stop. Record the
        # trigger the broker is actually holding, never the one this run
        # wanted -- they can differ, and the desk's record must say what
        # rests.
        if existing_sp != best_stop:
            logger.warning(
                "Reprotect NOT submitting for %s: a live stop from a "
                "PREVIOUS attempt (order %s, status %s) already rests "
                "at $%.2f, while this run wanted $%.2f. Submitting "
                "would leave TWO live stops on the same shares, which "
                "the broker accepts and nothing here reconciles. The "
                "resting stop is left in place and is what gets "
                "recorded. NOTE: the coverage sweep will NOT correct "
                "the price -- it compares quantity only and never "
                "reads stop_price -- so a wider-than-wanted stop "
                "persists until the trail moves it.",
                symbol,
                order_id,
                status or "unknown",
                existing_sp,
                best_stop,
            )
        else:
            logger.info(
                "Reprotect skipped for %s — a stop at $%.2f (order %s, "
                "status %s) placed by a PREVIOUS attempt is live at the "
                "broker and is not one this run cancelled (idempotent "
                "re-run)",
                symbol,
                existing_sp,
                order_id,
                status,
            )
        from src.execution.stop_records import write_back_stop_loss

        write_back_stop_loss(
            getattr(self, "db", None),
            symbol,
            existing_sp,
            is_short=(side == "buy"),
        )
        return ScanOutcome(done=True, value=True)
    return ScanOutcome(done=False)
