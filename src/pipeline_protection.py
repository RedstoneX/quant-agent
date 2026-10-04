"""Protective stops and broker reconciliation: everything that places, cancels,
restores or reconciles a protective stop or a sell against the broker.

Step 2 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved verbatim out of
`src/pipeline.py` as a mixin, so `TradingPipeline` keeps every one of these as
its own attribute and every test that patches or calls them is untouched.

This is the live-money code: the stop-coverage reconciler, the repair path, the
protected sell, the write-ahead cancel/restore legs, the repeg and restore
drains, the residual re-protection after a partial sell, the fill/stop-out
reconcilers, and `_handle_ex_dividends` -- which the plan REASSIGNED here out of
step 1's prompt-facts cluster because it shifts live stops down by the dividend
(plan S1 correction, 2026-10-01). A stop-moving method does not belong in a
module advertised as read-only.

The module-level helpers travel with it: `_WAL_SELL_SENTINEL`,
`_market_is_open_now`, `_price_is_through_stop`, `_position_notional`,
`_classify_coverage_gap`, `_reconciled_exit_action` and the broker-fill float
coercion `_finite_float_or_none`. All are re-exported from `src.pipeline` so
`from src.pipeline import ...` keeps working -- but a test that PATCHES one of
them on `src.pipeline` no longer reaches this module's code and must patch it
here instead (plan S5, silent-behaviour risk 1).

Nothing here may import `src.pipeline`: this module is one of its bases.

Five pieces now live as standalone classes under `src/protection/` (owner alerts,
sell finalisation, fill reconciliation, the repeg drain, coverage election),
each built from explicit keyword-only collaborators; the mixin keeps a thin
same-named shim per method, so callers and patch targets are unchanged. The
helpers `_price_is_through_stop`, `_position_notional`, `_finite_float_or_none`,
`_reconciled_exit_action` and `_WAL_SELL_SENTINEL` moved with them and are
imported back here, so `src.pipeline_protection.<name>` still resolves. What
stayed: everything that imports the broker seam (`src.execution`, a frozen
importer list), everything reading `_market_is_open_now`/`et_today` that tests
patch on this module, and the protected sell with its write-ahead cancel, which
hands `_last_stop_clear_refusal` between two methods through the pipeline.

2026-10-04: those followed too. `CoverageRepair`, `ProtectedSell`, `ExitRelief`,
`RestoreDrain`, `ReprotectRecords` and `ExDividends` live under `src/protection/`;
the two bodies over the 400-line ceiling for a new file (`StopCoverageReconciler`,
`ReprotectResidual`) stay here as standalone classes built the same way. Every
builder reads the host LIVE per call; host attributes a body assigns or reads with
a default go through `_HostState`, never a copy. The mixin is shims only.
"""

import json as _json
import logging
import math

from src.protection.coverage_repair import CoverageRepair
from src.protection.protected_sell import ProtectedSell
from src.protection.exit_relief import ExitRelief
from src.protection.restore_drain import RestoreDrain
from src.protection.reprotect_records import ReprotectRecords
from src.protection.ex_dividends import ExDividends
from src.protection.coverage_book_read import read_positions_with_retry
from src.protection.coverage_book_read import unverified_book_sweep
from src.sentinel.reconciliation import record_reconciliation
from src.execution.broker import AlpacaBroker, _split_protective_qty
from src.models import TradeDecision
from src.pipeline_context import RunContext
from src.storage.db import Database
from src.trading_calendar import et_now, et_today
from src.protection.coverage_election import _position_notional, _price_is_through_stop  # noqa: F401
from src.protection.fill_reconciler import _finite_float_or_none, _reconciled_exit_action  # noqa: F401
from src.protection.sell_finalization import _WAL_SELL_SENTINEL  # noqa: F401

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _market_is_open_now(broker) -> bool:
    """Is the regular cash session open RIGHT NOW?

    Spec §11.1 hybrid fractional stops. This is the discriminator the
    whole alerting distinction rests on: a fractional DAY stop that is
    absent while the market is SHUT is the design working — it lapsed at
    16:00 ET exactly as intended and the next session re-places it. The
    same stop absent while the market is OPEN is a placement failure and
    must wake somebody.

    Delegates to `src.market_session.market_open_verdict` — the single answer
    shared with the coverage watchdog. FAILS TOWARD "OPEN" ON PURPOSE. Every way this can be wrong has an
    asymmetric cost: believing the market is shut when it is open would
    SUPPRESS a real naked-position alert, which is the one failure this
    desk cannot absorb. Believing it is open when it is shut costs a
    redundant banner. So anything unknown, unreadable or unexpected
    answers True, and only a confident, positively-established "outside
    the session" answers False.

    The session-window table (`trading_calendar.SESSION_WINDOWS`) is the
    weekday 09:30-16:00 ET baseline; `broker.get_session_close()`
    tightens it on early-close days (Thanksgiving Friday 13:00, July 3),
    and is best-effort — a calendar failure leaves the baseline answer
    rather than inventing a closed market.

    Note the callers all sit behind `_is_trading_day()`, so a holiday
    never reaches here; the weekday check is belt-and-braces for a
    direct call.
    """
    from src.market_session import market_open_now

    return market_open_now(broker, et_now)




def _classify_coverage_gap(*, held: float, covered: float) -> tuple[str, float]:
    """Name the shortfall between held shares and stop-covered shares.

    Returns ``(coverage, frac_uncovered)`` where `coverage` is one of:

    ``'none'``       zero protective coverage on a position that should
                     have some. Guard 3's worst condition; escalates.
    ``'partial'``    some coverage, but the WHOLE-SHARE part of the
                     position is under-covered. Guard 3's milder
                     condition; banner, not escalation.
    ``'fractional'`` the ONLY thing missing is the sub-share remainder —
                     the durable GTC leg over floor(held) is intact.

    Spec §11.1 hybrid fractional stops. The third value is the whole
    point: under the hybrid design a sub-share remainder loses its DAY
    stop at every close, so classifying that as 'none' (which is what a
    bare `covered <= 0` test does for a position under one share) would
    fire the NO-STOP-AT-ALL owner alert every single night on a state
    that is expected, bounded and deliberate. An alert that cries wolf
    nightly is worse than no alert, because it trains the owner to swipe
    away the one message that must never be ignored.

    Market hours are deliberately NOT an input here. This answers only
    "what is missing"; the caller decides what that means at this hour.
    Keeping the two apart is what makes the overnight suppression
    auditable — it can only ever soften a gap already known to be
    'fractional', and it is one branch at one call site rather than a
    condition smeared through the classifier.
    """
    whole_held, frac_held = _split_protective_qty(held)
    shortfall = max(0.0, held - covered)
    # The durable leg is intact iff the covered qty reaches the whole-share
    # floor of the position. Anything less means a GTC stop is missing,
    # which is never the expected overnight state.
    durable_leg_intact = covered + 1e-6 >= whole_held
    only_sub_share_missing = shortfall <= frac_held + 1e-6
    if frac_held > 0 and durable_leg_intact and only_sub_share_missing:
        return "fractional", shortfall
    return ("none" if covered <= 1e-6 else "partial"), 0.0



def _collab_of(obj, name: str):
    """A collaborator for a lifted protection object: `obj._collab(name)` when the host's
    CLASS defines the deferred-error stand-in (TradingPipeline does), else the plain
    attribute -- so a MagicMock host hands over its own `db`/`broker`, not a child mock."""
    collab = getattr(type(obj), "_collab", None)
    if collab is not None:
        return collab(obj, name)
    return getattr(obj, name)


def _build_owner_alerts(host):
    """Builds the standalone OwnerAlerts from the host pipeline's collaborators (bodies moved to src/protection/owner_alerts.py)."""
    from src.protection.owner_alerts import OwnerAlerts
    return OwnerAlerts(
        broker=_collab_of(host, "broker"),
        still_uncovered=_collab_of(host, "_still_uncovered"),
        alert_owner_no_stop=_collab_of(host, "_alert_owner_no_stop"),
        format_qty=_collab_of(host, "_format_qty"),
    )


def _build_sell_finalization(host):
    """Builds the standalone SellFinalization from the host pipeline's collaborators (bodies moved to src/protection/sell_finalization.py)."""
    from src.protection.sell_finalization import SellFinalization
    return SellFinalization(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        terminal_order_statuses=host._TERMINAL_ORDER_STATUSES,
        finalize_protection_after_sell=_collab_of(host, "_finalize_protection_after_sell"),
        register_exit_settlement=_collab_of(host, "_register_exit_settlement"),
        finalize_protection_after_sell_core=_collab_of(host, "_finalize_protection_after_sell_core"),
        cancel_stray_stops_on_flat=_collab_of(host, "_cancel_stray_stops_on_flat"),
        current_position_qty_for_finalize=_collab_of(host, "_current_position_qty_for_finalize"),
        persist_orphaned_protection_restore=_collab_of(host, "_persist_orphaned_protection_restore"),
        reprotect_residual_after_partial_sell=_collab_of(host, "_reprotect_residual_after_partial_sell"),
        derive_close_side_for_drain=_collab_of(host, "_derive_close_side_for_drain"),
    )


def _build_fill_reconciler(host):
    """Builds the standalone FillReconciler from the host pipeline's collaborators (bodies moved to src/protection/fill_reconciler.py)."""
    from src.protection.fill_reconciler import FillReconciler
    return FillReconciler(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        config=getattr(host, "config", None),
        flag_stop_out_anomaly=_collab_of(host, "_flag_stop_out_anomaly"),
        format_qty=_collab_of(host, "_format_qty"),
        parse_broker_fill_timestamp=_collab_of(host, "_parse_broker_fill_timestamp"),
    )


def _build_repeg_drain(host):
    """Builds the standalone RepegDrain from the host pipeline's collaborators (bodies moved to src/protection/repeg_drain.py)."""
    from src.protection.repeg_drain import RepegDrain
    return RepegDrain(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        delete_repeg_row=_collab_of(host, "_delete_repeg_row"),
    )


def _build_coverage_election(host):
    """Builds the standalone CoverageElection from the host pipeline's collaborators (bodies moved to src/protection/coverage_election.py)."""
    from src.protection.coverage_election import CoverageElection
    return CoverageElection(
    )


class _HostState:
    """Live get/set view of the host attributes a part reads with a default or assigns (never a copy)."""

    def __init__(self, host) -> None:
        self._host = host

    def get(self, name: str):
        return getattr(self._host, name)

    def set(self, name: str, value) -> None:
        setattr(self._host, name, value)


class StopCoverageReconciler:
    """The broker-truth stop-coverage audit; standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        broker=None,
        db=None,
        sweeper=None,
        retired_cash_park_symbol=None,
        alert_owner_elected_unfilled=None,
        alert_owner_no_stop=None,
        alert_owner_repair_resolved=None,
        alert_owner_session_repair_failed=None,
        alert_owner_unreadable_stop=None,
        elected_unfilled_stop_row=None,
        repair_stop_coverage=None,
    ) -> None:
        self.broker = broker
        self.db = db
        self._sweeper = sweeper
        self._retired_cash_park_symbol = retired_cash_park_symbol
        self._alert_owner_elected_unfilled = alert_owner_elected_unfilled
        self._alert_owner_no_stop = alert_owner_no_stop
        self._alert_owner_repair_resolved = alert_owner_repair_resolved
        self._alert_owner_session_repair_failed = alert_owner_session_repair_failed
        self._alert_owner_unreadable_stop = alert_owner_unreadable_stop
        self._elected_unfilled_stop_row = elected_unfilled_stop_row
        self._repair_stop_coverage = repair_stop_coverage

    def _reconcile_stop_coverage(self) -> list[dict]:
        """Broker-truth stop-coverage audit, independent of the WAL queue.

        At session entry, enumerate every held position — long or short —
        and compare its held qty against the qty actually covered by its
        open protective stops at the broker (SELL-stops for a long,
        BUY-stops for a short). Flag (log + return) any position whose
        covered qty is materially below its held qty.

        Why this exists (design review's strongest finding): the whole
        naked-protection guarantee otherwise rests on some code path having
        successfully persisted a WAL recovery row. A position that goes naked
        WITHOUT a row — a best-effort persist that silently failed, a manual
        broker action, a future SELL path that skips a step — is never
        re-detected, because the WAL is a log of INTENDED operations, not an
        audit of ACTUAL broker coverage. This reconciler closes that gap by
        reading broker truth directly.

        Auto-repairing for longs AND shorts (see `_repair_stop_coverage` —
        it reconstructs the stop from the recorded opening row: BUY for a
        long, SHORT for a short). Inventing a level is still refused when
        that row has none; the SHORT row stores `stop_loss` the same way
        BUY does, so the old "no recorded entry for a short" objection is
        false.

        Symbols already queued for WAL recovery are skipped — the drain owns
        them. Returns the list of under-covered ``{symbol, held_qty,
        covered_qty, coverage, repaired}`` for the caller to surface to the
        operator.

        SPEC §11.1 HYBRID FRACTIONAL STOPS — this sweep is also the
        re-placement mechanism, and the alerting distinction lives here.

        A fractional position is covered by two orders: a durable GTC stop
        over floor(qty) and a DAY stop over the sub-share remainder, which
        the broker expires at 16:00 ET by design. That means "held qty
        exceeds covered qty" is now THREE different situations, not one, and
        reporting them identically would be the worst possible outcome — a
        nightly red banner on an expected state teaches the owner to ignore
        the banner that must never be ignored:

          (a) the durable GTC leg is intact, only the sub-share remainder is
              uncovered, and the market is SHUT. Expected. Stamped
              ``coverage='fractional_overnight'`` with ``uncovered_qty`` and
              ``unprotected_value`` so the exposure is a NUMBER the owner can
              read. No repair (a DAY order into a shut market is a rejection
              at best), no banner, no escalation.
          (b) the same shortfall while the market is OPEN. A placement
              failure. Repaired in place; if the repair lands it is stamped
              ``'fractional_replaced'`` — this is the ordinary start-of-
              session heartbeat and stays quiet — and if it does NOT land it
              falls back onto guard 3's existing ladder and alerts exactly as
              before.
          (c) the whole-share GTC leg is missing or short. Never suppressed,
              never reclassified, market hours irrelevant: 'none' escalates
              to the owner, 'partial' banners. Unchanged from guard 3.

        The three §11.1 guards are extended by this, not replaced: the retry
        burst (guard 1) now runs over each hybrid leg, the owner alert (guard
        2) still fires on a genuine partial cover, and this sweep still
        separates NO STOP AT ALL from STOP MIS-SIZED (guard 3).
        """
        try:
            pending_syms = {
                r.get("symbol") for r in self.db.get_pending_protection_restores()
            }
        except Exception:  # noqa: BLE001
            pending_syms = set()
        # A failed positions read used to `return []`, which every caller
        # reads as all-clear. See src/protection/coverage_book_read.py.
        positions, read_error = read_positions_with_retry(self.broker)
        if positions is None:
            return unverified_book_sweep(self, read_error or "", pending_syms)

        # Spec §11.1 hybrid fractional stops. Read ONCE per pass, not per
        # position: every gap in this sweep must be judged against the same
        # clock, or a sweep straddling 16:00 ET could call one symbol's
        # lapse expected and the next symbol's identical lapse a failure.
        market_open = _market_is_open_now(self.broker)
        if market_open: from src.execution.stop_repair import drain_owed_stop_levels; drain_owed_stop_levels(self.broker, self.db)  # owed out-of-hours stop levels land FIRST
        gaps: list[dict] = []
        # Positions whose protective stop has been elected and has not
        # filled. Kept OUT of `gaps`: every consumer of that list buckets a
        # row as "no stop at all" or "stop mis-sized", and this is neither —
        # the stop is present and correctly sized, it simply did not fill.
        elected_unfilled: list[dict] = []
        # Positions whose protective stops could NOT BE READ at the broker
        # (board item 172). Kept in their own list and appended to `gaps`
        # only at the very end, after every classifier, repair attempt and
        # escalation filter has run over the measured rows — so an unknown
        # can never be repaired against a guessed level, counted into the
        # overnight dollar total, or reclassified as a fractional lapse.
        unreadable: list[dict] = []
        # Symbols whose coverage gap this pass actually CLOSED. A red alarm
        # that later resolves has to say so: before this, "COULD NOT PUT THE
        # PROTECTIVE STOP BACK" was the owner's last word on a position the
        # desk itself re-covered fifteen minutes later, and he was left with
        # an instruction to place a stop by hand that already existed
        # (2026-09-23, RSG). Collected here and retracted once at the end
        # rather than one Telegram per symbol per sweep.
        repaired_symbols: list[str] = []
        longs_checked = 0
        shorts_checked = 0
        sweeper = self._sweeper()
        # A DISABLED sweep's vehicle is still exempt while it is held: it is
        # awaiting `_release_retired_cash_park`, not naked.
        sweep_symbol = (
            sweeper.symbol if sweeper is not None
            else self._retired_cash_park_symbol()
        )
        for p in positions:
            symbol = getattr(p, "symbol", None)
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            # A short carries a negative qty (Alpaca convention) and is a
            # real, currently-unreachable-but-possible position (shorts-safe,
            # Stage 2). `qty <= 0` used to exempt every short from this audit
            # outright — the "a SELL-stop can't protect a short" reasoning
            # was true, but the fix is to check the OTHER side's stops, not
            # to skip the check. Inverse-ETF hedges have their own handling.
            # Skip symbols the drain already owns.
            if not symbol or qty == 0 or symbol in pending_syms:
                continue
            # The cash-sweep vehicle is deliberately stopless (cash-equivalent;
            # see src/execution/cash_sweep.py) — flagging it every session
            # would train the operator to ignore the 🔴 banner.
            if sweep_symbol is not None and symbol == sweep_symbol:
                continue
            is_short = qty < 0
            if is_short:
                shorts_checked += 1
            else:
                longs_checked += 1
            held = abs(qty)
            # ---- CANNOT BE ASKED, board item 172 --------------------------
            # This used to `continue` on a warning, which dropped the symbol
            # out of `gaps` entirely — so a position whose stops the broker
            # refused to describe was reported to nobody and read downstream
            # exactly like a position confirmed covered. Removing the
            # account-level halt made per-position stops the desk's only
            # loss protection, which makes "I could not check this one" the
            # single most important thing this sweep can find and the one
            # thing it was silent about.
            #
            # Recorded as its own condition and the sweep carries on to the
            # next symbol: a read failure on one name says nothing about any
            # other, and aborting would hide the rest of the book behind it.
            try:
                ok, specs = self.broker.snapshot_protective_stops(
                    symbol, side=("buy" if is_short else "sell"),
                )
            except Exception as exc:  # noqa: BLE001
                unreadable.append({
                    "symbol": symbol, "held_qty": qty,
                    "covered_qty": None, "coverage": "unreadable",
                    "repaired": False, "is_short": is_short,
                    "read_error": f"snapshot_protective_stops raised: {exc}",
                })
                continue
            # `ok=False` is the COMMON read failure and the reason this
            # whole item exists: the broker's order listing swallows its own
            # exception, so a snapshot that raises is the rare case and a
            # snapshot that comes back False-with-nothing is the usual one.
            # Before it was honoured here, an outage read as 'none' — a
            # confirmed naked position — and was repaired against.
            if not ok:
                unreadable.append({
                    "symbol": symbol, "held_qty": qty,
                    "covered_qty": None, "coverage": "unreadable",
                    "repaired": False, "is_short": is_short,
                    "read_error": (
                        "the broker's open-order listing failed, so whether "
                        "a protective stop exists could not be established"
                    ),
                })
                continue
            if specs is not None and not isinstance(specs, list):
                # Not iterable in the loop below, and `for s in (specs or [])`
                # would raise straight out of this method and take the whole
                # sweep — every other position included — with it.
                unreadable.append({
                    "symbol": symbol, "held_qty": qty,
                    "covered_qty": None, "coverage": "unreadable",
                    "repaired": False, "is_short": is_short,
                    "read_error": (
                        f"protective-stop snapshot in an unusable shape: "
                        f"{type(specs).__name__}"
                    ),
                })
                continue
            # A stop order whose quantity cannot be parsed is a stop nobody
            # can size, and neither possible guess is safe: counting it as
            # zero invents a gap, skipping it invents coverage. Left
            # unparsed, the old `sum(...)` raised straight out of this whole
            # method and took the entire sweep — every other position
            # included — with it.
            covered = 0.0
            unparsable = ""
            for s in (specs or []):
                try:
                    covered += float(s.get("qty", 0) or 0)
                except (TypeError, ValueError, AttributeError) as exc:
                    unparsable = f"protective stop in an unreadable shape: {exc}"
                    break
            if unparsable:
                unreadable.append({
                    "symbol": symbol, "held_qty": qty,
                    "covered_qty": None, "coverage": "unreadable",
                    "repaired": False, "is_short": is_short,
                    "read_error": unparsable,
                })
                continue
            # ---- ELECTED BUT UNFILLED -------------------------------------
            # Runs for EVERY held position, including the ones this sweep is
            # about to call perfectly covered — which is the entire point.
            # The desk's protective stops now rest as stop-MARKET orders
            # (owner ratified 2026-09-25), which FILL when elected — so this
            # state should no longer arise for them. It CAN still arise for
            # the stop-LIMIT fallback (taken only when the broker refuses a
            # stop-market for an unsupported type/tif combo — see
            # `_submit_stop_limit_order` / `STOP_LIMIT_BUFFER_PCT`): on a gap
            # past that limit the stop is ELECTED and does not fill, the
            # order stays `status=OPEN`, so `specs` above still counts its
            # shares as covered and this sweep — correctly by its own logic —
            # does nothing about it, indefinitely. The shares are not
            # protected: an unfilled order is not an exit. Kept as the
            # backstop for exactly that residual case.
            #
            # Detected only while the market is OPEN, reusing the one
            # `market_open` read this pass already took: with the tape shut
            # there is no live "price through the stop", only yesterday's
            # close, and nobody could act on it anyway.
            if market_open:
                row = self._elected_unfilled_stop_row(
                    p, specs, is_short=is_short,
                )
                if row is not None:
                    elected_unfilled.append(row)
            if covered + 1e-6 < held:
                # Spec §11.1 guard 3. NO STOP AT ALL and STOP PRESENT BUT
                # MIS-SIZED were previously one condition with one message.
                # They are not the same thing and must never read as if they
                # were: a position stopped at the wrong size still has a
                # broker order standing watch over most of it, while a
                # position with zero coverage has nothing between it and the
                # tape. The second is the state that ends a desk, and it was
                # being reported in the same sentence as the first.
                coverage, frac_uncovered = _classify_coverage_gap(
                    held=held, covered=covered,
                )
                gap = {
                    "symbol": symbol, "held_qty": qty, "covered_qty": covered,
                    "coverage": coverage,
                }
                # ---- Spec §11.1 hybrid fractional stops: case (a) ----
                # The durable whole-share GTC leg is intact and the only
                # thing missing is the sub-share remainder, whose DAY stop
                # the broker expires at 16:00 ET BY DESIGN. Outside session
                # hours that is not a fault, it is the mechanism working, and
                # it happens to EVERY fractional position EVERY night. It is
                # reported as measured overnight exposure — a number the
                # owner can look at — and it does not touch either red
                # banner or the owner escalation. Nor is a repair attempted:
                # a DAY order submitted into a shut market is a rejection at
                # best and a surprise queued order at worst.
                if coverage == "fractional" and not market_open:
                    # WHERE THE QUIET STATE ENDS. The ratified overnight
                    # lapse is a sub-share DAY stop that WAS placed and
                    # expired at 16:00 by design. A remainder that spent the
                    # whole session waiting for its name's first print and
                    # never got a stop at all is not that, and filing it as
                    # that would turn the bell-adjacent silence into a
                    # suppression: nothing would ever have told the owner.
                    # This is the pass that can honestly say the session's
                    # repair path is exhausted, so this is the pass that
                    # pages.
                    try:
                        from src.coverage_watchdog import (
                            session_awaiting_print_symbols,
                        )

                        never_covered = (
                            symbol.strip().upper()
                            in session_awaiting_print_symbols()
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "coverage sweep: could not read today's "
                            "awaiting-print names (%s) — treating %s as the "
                            "ordinary overnight lapse.", exc, symbol,
                        )
                        never_covered = False
                    if never_covered:
                        gap["coverage"] = "partial" if covered > 1e-6 else "none"
                        gap["uncovered_qty"] = frac_uncovered
                        gap["unprotected_value"] = _position_notional(
                            p, frac_uncovered,
                        )
                        gap["repaired"] = False
                        gap["is_short"] = is_short
                        gap["session_repair_failed"] = True
                        gap["never_printed_today"] = True
                        logger.error(
                            "FRACTIONAL STOP NEVER RE-PLACED THIS SESSION: "
                            "%s held=%.4f, %.4f covered — the name produced "
                            "no confirmed trade print all session, so the "
                            "repair could never price a stop, and the "
                            "sub-share remainder has now been uncovered "
                            "since the previous close. This is NOT the "
                            "expected overnight lapse and it alerts.",
                            symbol, qty, covered,
                        )
                        gaps.append(gap)
                        continue
                    gap["coverage"] = "fractional_overnight"
                    gap["uncovered_qty"] = frac_uncovered
                    gap["unprotected_value"] = _position_notional(
                        p, frac_uncovered,
                    )
                    gap["repaired"] = False
                    logger.info(
                        "FRACTIONAL DAY STOP LAPSED (expected): %s held=%.4f, "
                        "%.4f whole share(s) still covered by the durable GTC "
                        "stop, %s sub-share remainder unprotected until the "
                        "next session re-places its DAY stop.",
                        symbol, qty, covered, frac_uncovered,
                    )
                    gaps.append(gap)
                    continue
                if coverage == "fractional":
                    # ---- case (b), first half: session hours ----
                    # The remainder should be covered RIGHT NOW. Repair it,
                    # and only if the repair fails does it carry a real
                    # condition name into the alerting below.
                    logger.warning(
                        "FRACTIONAL STOP MISSING DURING SESSION HOURS: %s "
                        "held=%.4f, %.4f covered — the sub-share DAY stop is "
                        "absent while the market is OPEN, which is a placement "
                        "failure, not the expected overnight lapse. Repairing.",
                        symbol, qty, covered,
                    )
                    repaired = self._repair_stop_coverage(
                        symbol, held - covered, is_short=is_short,
                        outcome=gap, resting_stops=list(specs or []),
                    )
                    gap["repaired"] = repaired
                    if repaired:
                        # Re-placed inside the same pass. This is the ordinary
                        # start-of-session path for every fractional position
                        # the desk holds, so it must NOT read as a red banner
                        # — it is the design's daily heartbeat.
                        gap["coverage"] = "fractional_replaced"
                        gap["uncovered_qty"] = 0.0
                        repaired_symbols.append(symbol)
                        logger.info(
                            "FRACTIONAL DAY STOP RE-PLACED: %s — the sub-share "
                            "remainder is covered again for this session.",
                            symbol,
                        )
                    else:
                        # Could not re-place during session hours. Falls back
                        # onto guard 3's existing ladder unchanged: zero
                        # coverage escalates, some coverage banners.
                        gap["coverage"] = "none" if covered <= 1e-6 else "partial"
                        gap["uncovered_qty"] = held - covered
                        gap["unprotected_value"] = _position_notional(
                            p, held - covered,
                        )
                        # The marker that makes the log line below TRUE.
                        # It used to be a claim only: a partial fallback
                        # ('partial' whenever any whole share is still
                        # covered, which is every fractional position) never
                        # reached `_alert_owner_no_stop`, and the only code
                        # that could page lived in the standalone coverage
                        # watchdog — a separate process that need not be
                        # running, and was not on 2026-09-18, when NET and
                        # RSG sat uncovered during the session and the owner
                        # was never told. The session that OBSERVED it now
                        # sends it.
                        gap["is_short"] = is_short
                        # WHICH SWEEP IS ENTITLED TO PAGE. Not this one, if
                        # the refusal is the tape's rather than the desk's.
                        # Measured across the whole retained production log,
                        # every `no_trade_print_today` refusal fired inside
                        # 45 seconds of the opening bell and every one was
                        # resolved in the same session, five of them by the
                        # very next sweep — so the first attempt paged before
                        # the mechanism that fixes it had had its turn, and
                        # the owner was sent to place a stop by hand that the
                        # desk placed itself fifteen minutes later. The
                        # classifier and its derivation live in one place
                        # (`src.coverage_watchdog.page_now_for_refusal`),
                        # shared with the standalone sweep, and a position
                        # with NO coverage left is never deferred by it.
                        try:
                            from src.coverage_watchdog import (
                                awaiting_first_print, note_awaiting_first_print,
                            )

                            waiting = awaiting_first_print(
                                refusal_code=str(
                                    gap.get("repair_refusal_code") or ""
                                ),
                                still_covered=covered > 1e-6,
                                market_open=True,
                            )
                            if waiting:
                                note_awaiting_first_print(symbol)
                        except Exception as exc:  # noqa: BLE001
                            # An unreadable marker file errs towards telling
                            # the owner, the same way the claim does.
                            logger.warning(
                                "coverage sweep: could not classify the "
                                "stop-repair refusal for %s (%s) — paging.",
                                symbol, exc,
                            )
                            waiting = False
                        page_now = not waiting
                        gap["session_repair_failed"] = page_now
                        if page_now:
                            logger.error(
                                "FRACTIONAL STOP RE-PLACEMENT FAILED for %s "
                                "during session hours (held=%.4f, "
                                "covered=%.4f) — this is case (b) and it "
                                "alerts.", symbol, qty, covered,
                            )
                        else:
                            # NOT a new `coverage` word. The gap stays
                            # 'partial' and keeps its ⚠️ STOP MIS-SIZED line
                            # in the session feed and the evening banner:
                            # the position really is under-protected and the
                            # owner should still SEE it. The only thing this
                            # state changes is whether it INTERRUPTS him,
                            # which is the defect. Reclassifying it would
                            # have meant registering a fifth coverage word
                            # in `src/notifier.py` and `src/trader_feed.py`
                            # and would have hidden a real shortfall to fix
                            # an alerting bug.
                            gap["awaiting_first_print"] = True
                            logger.warning(
                                "FRACTIONAL STOP AWAITING FIRST PRINT: %s "
                                "(held=%.4f, covered=%.4f): %s. The repair "
                                "was attempted and will be attempted again "
                                "on every pass; the whole-share leg is still "
                                "standing watch. Not an owner page while the "
                                "session can still resolve it — the pass "
                                "that finds the market shut with this still "
                                "true is the one that pages.",
                                symbol, qty, covered,
                                gap.get("repair_refusal") or "no reason given",
                            )
                    gaps.append(gap)
                    continue
                # ---- case (c) and every pre-existing condition ----
                # The whole-share GTC leg is missing or short. Never
                # suppressed, never reclassified, market hours irrelevant:
                # that leg is the durable protection and its absence is the
                # state that ends a desk.
                if coverage == "none":
                    logger.critical(
                        "NO STOP AT ALL: %s held=%.4f with ZERO open "
                        "protective %s-stops — the position is COMPLETELY "
                        "unprotected and has no WAL recovery row.",
                        symbol, qty, "buy" if is_short else "sell",
                    )
                else:
                    logger.warning(
                        "STOP MIS-SIZED: %s held=%.4f but only %.4f covered by "
                        "open protective %s-stops — partially unprotected with "
                        "no WAL recovery row.", symbol, qty, covered,
                        "buy" if is_short else "sell",
                    )
                gap["repaired"] = self._repair_stop_coverage(
                    symbol, held - covered, is_short=is_short, outcome=gap,
                    resting_stops=list(specs or []),
                )
                if gap["repaired"]:
                    repaired_symbols.append(symbol)
                gaps.append(gap)
        if (longs_checked or shorts_checked) and not gaps and not unreadable:
            logger.info(
                "Stop-coverage reconcile: all %d long / %d short position(s) "
                "adequately stop-covered", longs_checked, shorts_checked,
            )
        elif unreadable and not gaps:
            # Board item 172. The clean line above says every position is
            # covered. A pass that could not read one is not entitled to
            # say that about the book, only about the part it could read.
            logger.error(
                "Stop-coverage reconcile: %d of %d position(s) UNREADABLE "
                "(%s) — every position that COULD be read is adequately "
                "stop-covered; the rest is unknown.",
                len(unreadable), longs_checked + shorts_checked,
                ", ".join(str(g.get("symbol")) for g in unreadable),
            )
        # Spec §11.1 hybrid fractional stops, observability half. Total the
        # deliberate overnight exposure into ONE line the owner can read at a
        # glance. The individual gap dicts carry it too (the notifier renders
        # them), but a running total is what turns "a bounded remainder" from
        # a promise into a measurement.
        overnight = [
            g for g in gaps if g.get("coverage") == "fractional_overnight"
        ]
        if overnight:
            total_value = sum(
                float(g.get("unprotected_value") or 0) for g in overnight
            )
            logger.warning(
                "OVERNIGHT FRACTIONAL EXPOSURE: %d position(s) carrying a "
                "sub-share remainder with no live stop until the next session "
                "— $%.2f total at risk. Expected and bounded by design; the "
                "whole-share part of each is still covered by its GTC stop.",
                len(overnight), total_value,
            )
        # Spec §11.1 guard 3, escalation half. A gap the auto-repair CLOSED
        # needs no interruption — the belt did its job. A position still
        # carrying NO stop at all after the repair attempt is a live naked
        # position, and the sweep runs on a 30-minute cadence whose
        # `intra_check` message is silent unless it liquidates: without this,
        # the worst state this reconciler can find would be reported only in
        # a log file. Mis-sized gaps stay in the session banner rather than
        # interrupting the owner — they are real but bounded, and alerting on
        # both is how a channel gets tuned out.
        #
        # Spec §11.1 hybrid fractional stops: the `coverage == "none"` test is
        # exactly the right filter and needs no exception added to it. An
        # expected overnight lapse is stamped 'fractional_overnight' and a
        # re-placed one 'fractional_replaced', so neither can reach this list
        # — while a sub-share position that could NOT be re-covered during
        # SESSION hours falls back to 'none' above and escalates here, which
        # is precisely case (b). The suppression lives in one classifier, not
        # in a growing list of special cases at the escalation site.
        naked = [
            g for g in gaps
            if g.get("coverage") == "none" and not g.get("repaired")
        ]
        if elected_unfilled:
            self._alert_owner_elected_unfilled(elected_unfilled)
        if naked:
            self._alert_owner_no_stop(naked)
        # A session-hours re-placement that did not land is its own
        # escalation, separate from the naked list above: the whole-share
        # GTC leg is usually still standing watch, so the gap classifies as
        # 'partial' and would otherwise be a banner line the owner reads
        # hours later, if at all. Suppression is shared with the standalone
        # watchdog so whichever process sees it first is the one that tells
        # him, and neither repeats the other.
        session_failures = [
            g for g in gaps
            if g.get("session_repair_failed") and not g.get("repaired")
            # A sub-share failure with zero coverage left already went out
            # as NO STOP AT ALL above; one condition, one message.
            and g not in naked
        ]
        if session_failures:
            self._alert_owner_session_repair_failed(session_failures)
        # Sent AFTER the escalations above, and last for a reason: a symbol
        # this pass both repaired and then found short again must end on the
        # alarm, not on the retraction.
        if repaired_symbols:
            try:
                from src.coverage_watchdog import clear_awaiting_first_print
                # The gap is closed, so the name is no longer waiting on a
                # print and must not be reported after the close as though
                # it had waited all session.
                clear_awaiting_first_print(repaired_symbols)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "coverage sweep: could not clear the awaiting-print "
                    "marker for %s: %s", ", ".join(repaired_symbols), exc,
                )
            self._alert_owner_repair_resolved(repaired_symbols)
        # Board item 172. Appended AFTER every filter above has been built
        # from `gaps`, so an unreadable row cannot reach the naked list, the
        # session-failure list, the overnight dollar total or a repair — all
        # of which require a measured shortfall this row does not have.
        if unreadable:
            self._alert_owner_unreadable_stop(unreadable)
            gaps.extend(unreadable)
        try:
            from src.execution.stop_records import (
                reconcile_recorded_stop_levels, report_stop_level_mismatches,
                write_back_live_protective_stops,
            )
            mismatches = reconcile_recorded_stop_levels(
                broker=self.broker,
                last_buy=lambda sym, action="BUY": self.db.get_symbol_last_buy(
                    sym, include_in_flight=True, action=action,
                ),
                positions=positions,
                sweep_symbol=sweep_symbol,
                skip_symbols=pending_syms, db=self.db,
            )
            mismatches = write_back_live_protective_stops(self.db, mismatches)
            report_stop_level_mismatches(record_reconciliation(db=self.db, kind="recorded_stop_levels", result=mismatches))
        except Exception as exc:  # noqa: BLE001
            logger.error("stop-level reconcile failed: %s", exc)
        return record_reconciliation(db=self.db, kind="stop_coverage", result=gaps)


class ReprotectResidual:
    """The residual re-protection after a partial sell; standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        broker=None,
        format_qty=None,
        alert_owner_reprotect_left_naked=None,
        alert_owner_stop_pending_acceptance=None,
        alert_owner_unreadable_stop=None,
        record_reprotect_identity_gap=None,
        state=None,
    ) -> None:
        self.broker = broker
        self._format_qty = format_qty
        self._alert_owner_reprotect_left_naked = alert_owner_reprotect_left_naked
        self._alert_owner_stop_pending_acceptance = alert_owner_stop_pending_acceptance
        self._alert_owner_unreadable_stop = alert_owner_unreadable_stop
        self._record_reprotect_identity_gap = record_reprotect_identity_gap
        self._state = state  # live get/set view of the host's attributes this part reads and assigns

    @property
    def db(self):
        return self._state.get('db')

    @db.setter
    def db(self, value) -> None:
        self._state.set('db', value)

    def _reprotect_residual_after_partial_sell(
        self, symbol: str, residual_qty: float, cancelled_specs: list[dict],
        *, side: str = "sell",
    ) -> bool:
        """After a partial exit (REDUCE / PARTIAL_SELL), place a
        fresh stop on the residual qty using the most-protective price among
        the stops we cancelled to clear held_for_orders for the SELL.

        Without this, the cancel-then-sell flow introduced in P1 #3 leaves
        the residual position naked until the next morning's BUY rebuilds an
        OTO leg — which never happens for a held-through position. The stop
        we re-place isn't a perfect copy of the original (we collapse
        multiple stops onto the highest stop_price), but it preserves at
        least the most-protective coverage that was in place pre-SELL.

        ``side`` — 'sell' (default) re-places a SELL stop below price for a
        long; 'buy' re-places a BUY stop above price for a short. This also
        flips which extreme counts as "most protective": for a long's SELL
        stop, tighter/sooner-to-trigger is the HIGHEST stop_price (closest
        to price from below); for a short's BUY stop it's the OPPOSITE —
        the LOWEST stop_price (closest to price from above). Picking the
        long-side extreme for a short would silently place the loosest,
        least-protective stop of the set instead of the tightest one.

        Returns True iff a fresh stop was successfully submitted (or there
        was nothing to do). Returns False if the submit raised — drain
        callers use this to keep the persisted recovery intent alive.
        Best-effort logging: a False return doesn't propagate the
        exception (the SELL itself already succeeded), but the caller
        knows coverage wasn't actually rebuilt.
        """
        if residual_qty <= 0 or not cancelled_specs:
            # NOTHING WAS REQUESTED: no residual to protect, or no stops were
            # cancelled to restore. Legitimately "nothing to do".
            return True
        # docs/WORK.md item 88. `[s.get("stop_price", 0) for s in specs]` then
        # `best_stop <= 0: return True` was the fail-open: a spec set whose
        # prices are all zero/absent/garbage was read as "no stop to restore"
        # and reported as SUCCESS — which makes the drain caller DELETE the
        # persisted recovery intent and leaves the residual position naked
        # with nothing left to retry it. A missing price also made `min`/`max`
        # raise on a None. Judge the values first, then decide; specs existed,
        # so "no usable price among them" is a REFUSAL, not an absence.
        from src.execution.stop_records import usable_stop_prices

        usable = usable_stop_prices(
            s.get("stop_price") for s in cancelled_specs
        )
        if not usable:
            logger.error(
                "Reprotect REFUSED for %s: %d cancelled stop spec(s) carried "
                "no usable trigger price (%r) — the residual %s share(s) are "
                "UNPROTECTED and the recovery intent is kept so the next "
                "drain retries. A garbage stop is not 'no stop needed'.",
                symbol, len(cancelled_specs),
                [s.get("stop_price") for s in cancelled_specs],
                self._format_qty(residual_qty),
            )
            self._alert_owner_reprotect_left_naked(
                symbol, residual_qty, 0.0,
                "cancelled stop specs carried no usable trigger price",
            )
            return False
        best_stop = min(usable) if side == "buy" else max(usable)

        # Idempotency: drain may replay finalize on a row whose previous
        # attempt already submitted the residual stop but couldn't
        # delete the pending_protection_restores row (DB error / process
        # kill between broker submit and row delete). Without this
        # check, the next drain pass would add a SECOND stop at the same
        # price on the same residual qty — doubling exit on trigger.
        # Audit 2026-05-27: matches the discipline _restore_stop_orders
        # already enforces via its `check_idempotency` flag for the
        # restore-originals branch.
        #
        # INCIDENT 2026-09-30 (AAPL): the check as written could not tell
        # the replay case above from THIS run's own cancel. Alpaca's cancel
        # is asynchronous and its OPEN order filter includes the
        # transitional `pending_cancel` state, so a stop cancelled 486ms
        # earlier in this same run was still listed as open, matched
        # `best_stop` exactly (it IS the spec best_stop came from), and the
        # skip returned True — which made the drain caller delete the
        # recovery intent and left ~$2,500 naked with no record that a stop
        # was still owed. The distinction is made by IDENTITY, not by
        # timing and not by a tolerance: `cancelled_specs` already carry the
        # broker order `id` (stamped by `_snapshot_stop_order`), which is
        # the same discipline `replace_stop_loss` has enforced since PR #75.
        # Ambiguity fails toward SUBMITTING.
        #
        # WHAT A DUPLICATE STOP ACTUALLY COSTS -- corrected 2026-09-30.
        # This comment used to assert "a duplicate stop is recoverable, a
        # naked position is not". Nothing in this repository makes the
        # first half of that true: `src/coverage_watchdog.py` states at its
        # top that it never cancels or modifies anything, and the only
        # duplicate handling anywhere is an owner message asking for the
        # extra order to be cancelled BY HAND. Two sell stops resting over
        # one long, both elected on the same gap, sell the shares twice:
        # the second fill opens a SHORT of the position's size, which no
        # stop covers and which this desk never decided to hold. That is
        # not "recoverable"; it is a new unbounded position.
        #
        # DEFECT 5 (adversary round 3). This comment used to close with
        # "a naked position is worse than a duplicate". That ranking is
        # NOT measured anywhere in this repository -- the refusal record
        # written below says so in as many words -- so asserting it here
        # and denying it forty lines later cannot both be honest. The
        # ranking is withdrawn. What is left is the only claim the code
        # actually relies on: BOTH outcomes are unacceptable, so the check
        # below separates them by IDENTITY rather than by preferring one,
        # and it falls toward submitting only where identity genuinely
        # cannot be established -- an ordering of last resort, recorded as
        # an ambiguity each time it is used, not a measured preference.
        from src.execution.broker import (
            PROTECTIVE_ORDER_ACTIVE_STATUSES as _ACTIVE_STATUSES,
            PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES as _IN_FLIGHT_STATUSES,
            real_broker_order_id as _real_order_id,
        )
        # `_snapshot_stop_order` stamps `str(order.id)`, so an absent id
        # arrives here as the TRUTHY string "None". Filtering on
        # truthiness let such a spec count toward `ids_complete` and then
        # match no open order at all, which sent every replay straight into
        # the submit branch -- entering the duplicate case through the
        # wrong door. Judge the value.
        real_ids = [_real_order_id(spec.get("id")) for spec in cancelled_specs]
        cancelled_ids = {oid for oid in real_ids if oid}
        # If ANY cancelled spec arrived without a real id we cannot prove
        # that a matching open stop isn't one of ours, so no open stop may
        # satisfy the check at all. Counted over the LIST, not the set: two
        # specs sharing one id is also a state we cannot reason from.
        missing_ids = sum(1 for oid in real_ids if not oid)
        ids_complete = missing_ids == 0 and len(cancelled_ids) == len(cancelled_specs)
        # Alpaca's OPEN filter includes transitional statuses, and the
        # two named sets in `src/execution/broker.py` are read SEPARATELY
        # here (adversary round 2, defect 2). A `pending_new` stop has been
        # received but not routed, and `pending_new` can still become
        # `rejected`: it is therefore neither protection this run may bank
        # nor an order this run may safely place a second stop over. That
        # third state gets its own outcome below -- no write-back, no
        # drain, return False -- so the recovery intent SURVIVES and the
        # next pass re-reads a status that has by then resolved. Nothing
        # here waits, retries or times out: no such number is derivable
        # and none is invented. `pending_cancel` is in neither set.
        # No literal is copied.
        try:
            if side == "buy":
                existing = self.broker._list_open_protective_stop_orders(symbol, side="buy")
            else:
                existing = self.broker._list_open_sell_stop_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Reprotect idempotency check failed for %s: %s — "
                "proceeding with submit (may duplicate if a stop already "
                "exists)", symbol, exc,
            )
            existing = []
        # HOISTED out of the per-order loop (adversary round 2, defect 4).
        # `ids_complete` does not depend on `o`. Evaluated inside the loop
        # it was only ever consulted for an order that had already passed
        # the price and quantity filters, so the case it exists for -- this
        # run cannot prove which stops are its own -- submitted in exactly
        # the silence that preceded the fix whenever nothing matched. It is
        # now decided BEFORE any order is examined, and its reason is
        # written to the append-only per-symbol refusal record rather than
        # living only in a log line.
        # DEFECT 4 (adversary round 3). This branch used to set
        # `existing = []`, which threw away EVERY broker record -- including
        # the in-flight (`pending_new`) and under-covering cases below, whose
        # whole purpose is to stop this run acting on a state it cannot act
        # on safely. Unprovable identity is a reason not to BANK an open stop
        # as protection; it is not a reason to go blind to what the broker
        # just said. The records are kept and the inability to prove
        # ownership is carried as a flag, consulted at the one point where it
        # matters: immediately before banking.
        identity_unprovable = False
        if existing and not ids_complete:
            logger.warning(
                "Reprotect for %s will SUBMIT despite %d open stop(s) at "
                "the broker: %d of %d cancelled spec(s) carried no broker "
                "order id, so this run cannot prove an open stop is not "
                "the one it just cancelled. Submitting risks a duplicate "
                "stop, which nothing in this desk reconciles; skipping "
                "risks a naked position. The ordering of those two harms "
                "is NOT measured anywhere in this repo, so this branch "
                "does not rank them -- it records the ambiguity and fails "
                "toward the position having a stop.",
                symbol, len(existing), missing_ids, len(cancelled_specs),
            )
            self._record_reprotect_identity_gap(
                symbol,
                f"{missing_ids} of {len(cancelled_specs)} cancelled stop "
                f"spec(s) carried no broker order id; submitted a fresh "
                f"stop at ${best_stop:.2f} over {len(existing)} open "
                f"broker stop(s) that could not be identified. A DUPLICATE "
                f"protective stop may now rest on {symbol} and nothing in "
                f"this desk reconciles one.",
            )
            identity_unprovable = True
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
            status = str(
                getattr(status_attr, "value", status_attr) or ""
            ).lower()
            if not order_id:
                logger.warning(
                    "Reprotect for %s will SUBMIT: an open stop at $%.2f "
                    "carries no readable order id, so it cannot be "
                    "distinguished from the stop this run just cancelled.",
                    symbol, existing_sp,
                )
                continue
            if order_id in cancelled_ids:
                logger.warning(
                    "Reprotect for %s will SUBMIT: the open stop at $%.2f "
                    "(order %s) is one THIS run just cancelled and is still "
                    "being listed as open — not a prior successful attempt. "
                    "Skipping here is what leaves the position naked.",
                    symbol, existing_sp, order_id,
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
                    symbol, order_id, existing_sp,
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
                    symbol, order_id, status, existing_sp, status,
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
                    symbol, self._format_qty(residual_qty), order_id,
                    status or "unknown", existing_sp,
                )
                return False
            if status not in _ACTIVE_STATUSES:
                logger.warning(
                    "Reprotect for %s will SUBMIT: the open stop at $%.2f "
                    "(order %s) is in status %r, not a live protective "
                    "state — a dying order is not coverage.",
                    symbol, existing_sp, order_id, status or "unknown",
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
                    symbol, order_id, status or "unknown", existing_sp,
                )
                self._record_reprotect_identity_gap(
                    symbol,
                    f"a live stop (order {order_id}) rests over the "
                    f"residual but its trigger price was unreadable; "
                    f"nothing was banked and the recovery intent was kept.",
                )
                self._alert_owner_unreadable_stop([{
                    "symbol": symbol, "held_qty": residual_qty,
                    "read_error": (
                        f"a live protective stop (order {order_id}, status "
                        f"{status or 'unknown'}) rests at the broker but its "
                        f"trigger price could not be read"
                    ),
                }])
                return False
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
                    symbol, order_id, status or "unknown", existing_sp,
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
                    symbol, order_id, status or "unknown",
                    existing_sp, best_stop,
                )
            else:
                logger.info(
                    "Reprotect skipped for %s — a stop at $%.2f (order %s, "
                    "status %s) placed by a PREVIOUS attempt is live at the "
                    "broker and is not one this run cancelled (idempotent "
                    "re-run)",
                    symbol, existing_sp, order_id, status,
                )
            from src.execution.stop_records import write_back_stop_loss
            write_back_stop_loss(
                getattr(self, "db", None), symbol, existing_sp,
                is_short=(side == "buy"),
            )
            return True

        # SUBMIT THROUGH THE RETRYING PATH, not the raw one (2026-09-30).
        #
        # This loop used to call `_submit_stop_limit_order` directly and
        # place its OWN legs. Three things were wrong with that, and this
        # PR routes far more traffic through them:
        #
        #   * no retry burst and no `held_for_orders` reconciliation, so a
        #     transient refusal -- or a refusal caused by a stop another
        #     path had already placed over these very shares -- ended as a
        #     naked residual when the broker was in fact already covered;
        #   * it placed the whole-share GTC leg BEFORE the fractional
        #     sliver, which is MEASURED-bad: 2026-09-16, the GTC hold
        #     reserved the position and Alpaca refused the 0.4393 BRK-B DAY
        #     sliver with held_for_orders (see `_submit_stop_legs`). The
        #     shared path places the DAY remainder FIRST for that reason;
        #   * a half-placed pair was silently reported as full success.
        #
        # `_submit_protective_stop_retrying` is the desk's one protective
        # submit. It is used here in preference to `_submit_stop_legs`
        # (the all-or-nothing variant `replace_stop_loss` uses) on purpose:
        # rolling a landed whole-share GTC leg back to ZERO coverage
        # because the sub-share sliver was refused would make the residual
        # fully naked, which is the worse of the two states. Instead the
        # partial is REPORTED with the quantity actually covered, and the
        # sliver is left to the coverage sweep that already owns DAY-leg
        # re-placement. It never raises; None means nothing was placed.
        from src.execution.stop_records import accepted_stop_order, write_back_stop_loss

        # `_submit_protective_stop_retrying` documents that it never
        # raises, but this function's own contract is that the SELL has
        # already succeeded and nothing here may propagate — so an
        # unexpected raise is caught and reported as no coverage rather
        # than escaping into the drain caller.
        try:
            placed = self.broker._submit_protective_stop_retrying(
                symbol=symbol, qty=residual_qty, stop_price=best_stop,
                limit_price=None, side=side,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Re-protect failed for %s residual=%s @ $%.2f: %s — position "
                "is unprotected until the next session re-attaches a stop",
                symbol, self._format_qty(residual_qty), best_stop, exc,
            )
            self._alert_owner_reprotect_left_naked(
                symbol, residual_qty, 0.0,
                f"the protective stop submit at ${best_stop:.2f} raised: {exc}",
            )
            return False
        if placed is None or (
            isinstance(placed, dict) and not accepted_stop_order(placed)
        ):
            logger.warning(
                "Re-protect failed for %s residual=%s @ $%.2f — the "
                "protective submit placed nothing the broker acknowledged; "
                "the position is unprotected until it is re-attached",
                symbol, self._format_qty(residual_qty), best_stop,
            )
            self._alert_owner_reprotect_left_naked(
                symbol, residual_qty, 0.0,
                f"the protective stop submit at ${best_stop:.2f} placed "
                "nothing the broker acknowledged",
            )
            return False
        # Whole-share submits return the broker's own response untouched
        # (no `covered_qty` key) -- that shape means one GTC leg over the
        # whole quantity, so the covered quantity IS the residual.
        covered_qty, uncovered_qty = float(residual_qty), 0.0
        if isinstance(placed, dict):
            covered_qty = float(placed.get("covered_qty", residual_qty) or 0.0)
            uncovered_qty = float(placed.get("uncovered_qty", 0.0) or 0.0)
        # A stop IS live at this trigger, so record it either way -- the
        # write-back is what the next sweep compares the book against.
        write_back_stop_loss(
            getattr(self, "db", None), symbol, best_stop,
            is_short=(side == "buy"),
        )
        if uncovered_qty > 0:
            logger.error(
                "Re-protect for %s is PARTIAL @ stop $%.2f: %s of %s "
                "share(s) are covered, %s are NOT — reporting the real "
                "coverage rather than a naked-or-covered guess.",
                symbol, best_stop, self._format_qty(covered_qty),
                self._format_qty(residual_qty),
                self._format_qty(uncovered_qty),
            )
            self._alert_owner_reprotect_left_naked(
                symbol, residual_qty, covered_qty,
                f"the stop at ${best_stop:.2f} covers only part of the "
                "residual after a partial exit",
            )
            return False
        logger.info(
            "Re-protected %s residual qty=%s @ stop $%.2f after partial exit",
            symbol, self._format_qty(residual_qty), best_stop,
        )
        return True


def _build_coverage_repair(host):
    """Builds the standalone CoverageRepair from the host pipeline's collaborators, read LIVE at each call."""
    return CoverageRepair(
        broker=_collab_of(host, 'broker'),
        db=_collab_of(host, 'db'),
        alert_owner_kill_switch_blocked=_collab_of(host, '_alert_owner_kill_switch_blocked'),
    )


def _build_protected_sell(host):
    """Builds the standalone ProtectedSell from the host pipeline's collaborators, read LIVE at each call."""
    return ProtectedSell(
        broker=_collab_of(host, 'broker'),
        db=_collab_of(host, 'db'),
        alert_owner_exit_declined=_collab_of(host, '_alert_owner_exit_declined'),
        order_accepted=_collab_of(host, '_order_accepted'),
        write_ahead_protection_restore=_collab_of(host, '_write_ahead_protection_restore'),
        cancel_stops_with_write_ahead=_collab_of(host, '_cancel_stops_with_write_ahead'),
        state=_HostState(host),
    )


def _build_exit_relief(host):
    """Builds the standalone ExitRelief from the host pipeline's collaborators, read LIVE at each call."""
    return ExitRelief(
        broker=_collab_of(host, 'broker'),
        state=_HostState(host),
    )


def _build_restore_drain(host):
    """Builds the standalone RestoreDrain from the host pipeline's collaborators, read LIVE at each call."""
    return RestoreDrain(
        broker=_collab_of(host, 'broker'),
        db=_collab_of(host, 'db'),
        terminal_order_statuses=_collab_of(host, '_TERMINAL_ORDER_STATUSES'),
        finalize_protection_after_sell=_collab_of(host, '_finalize_protection_after_sell'),
        resolve_wal_row_side=_collab_of(host, '_resolve_wal_row_side'),
        restore_after_unconfirmed_sell=_collab_of(host, '_restore_after_unconfirmed_sell'),
    )


def _build_reprotect_records(host):
    """Builds the standalone ReprotectRecords from the host pipeline's collaborators, read LIVE at each call."""
    return ReprotectRecords(
        record_exit_refusal=_collab_of(host, '_record_exit_refusal'),
    )


def _build_ex_dividends(host):
    """Builds the standalone ExDividends from the host pipeline's collaborators, read LIVE at each call."""
    return ExDividends(
        broker=_collab_of(host, 'broker'),
        db=_collab_of(host, 'db'),
        market=_collab_of(host, 'market'),
        repair_stop_coverage=_collab_of(host, '_repair_stop_coverage'),
    )


def _build_stop_coverage_reconciler(host):
    """Builds the standalone StopCoverageReconciler from the host pipeline's collaborators, read LIVE at each call."""
    return StopCoverageReconciler(
        broker=_collab_of(host, 'broker'),
        db=_collab_of(host, 'db'),
        sweeper=_collab_of(host, '_sweeper'),
        retired_cash_park_symbol=_collab_of(host, '_retired_cash_park_symbol'),
        alert_owner_elected_unfilled=_collab_of(host, '_alert_owner_elected_unfilled'),
        alert_owner_no_stop=_collab_of(host, '_alert_owner_no_stop'),
        alert_owner_repair_resolved=_collab_of(host, '_alert_owner_repair_resolved'),
        alert_owner_session_repair_failed=_collab_of(host, '_alert_owner_session_repair_failed'),
        alert_owner_unreadable_stop=_collab_of(host, '_alert_owner_unreadable_stop'),
        elected_unfilled_stop_row=_collab_of(host, '_elected_unfilled_stop_row'),
        repair_stop_coverage=_collab_of(host, '_repair_stop_coverage'),
    )


def _build_reprotect_residual(host):
    """Builds the standalone ReprotectResidual from the host pipeline's collaborators, read LIVE at each call."""
    return ReprotectResidual(
        broker=_collab_of(host, 'broker'),
        format_qty=_collab_of(host, '_format_qty'),
        alert_owner_reprotect_left_naked=_collab_of(host, '_alert_owner_reprotect_left_naked'),
        alert_owner_stop_pending_acceptance=_collab_of(host, '_alert_owner_stop_pending_acceptance'),
        alert_owner_unreadable_stop=_collab_of(host, '_alert_owner_unreadable_stop'),
        record_reprotect_identity_gap=_collab_of(host, '_record_reprotect_identity_gap'),
        state=_HostState(host),
    )



class ProtectionMixin:
    """See the module docstring. Methods are the moved text, byte-for-byte."""

    # Statuses Alpaca uses for terminal/non-terminal orders. Kept as a
    # class attribute so tests can introspect the exact set the
    # finalizer treats as "done".
    _TERMINAL_ORDER_STATUSES = {
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced",
    }

    def _current_position_qty_for_finalize(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._current_position_qty_for_finalize(_build_sell_finalization(self), *args, **kwargs)

    def _reconcile_stop_coverage(self, *args, **kwargs):
        """Thin shim -> StopCoverageReconciler (this module; body over the 400-line ceiling for a new file); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return StopCoverageReconciler._reconcile_stop_coverage(_build_stop_coverage_reconciler(self), *args, **kwargs)

    def _elected_unfilled_stop_row(self, *args, **kwargs):
        """Thin shim -> CoverageElection (src/protection/coverage_election.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.coverage_election import CoverageElection
        return CoverageElection._elected_unfilled_stop_row(_build_coverage_election(self), *args, **kwargs)

    @staticmethod
    def _alert_owner_elected_unfilled(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_elected_unfilled (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_elected_unfilled(*args, **kwargs)

    def _still_uncovered(self, *args, **kwargs):
        """Thin shim -> OwnerAlerts (src/protection/owner_alerts.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._still_uncovered(_build_owner_alerts(self), *args, **kwargs)

    def _alert_owner_session_repair_failed(self, *args, **kwargs):
        """Thin shim -> OwnerAlerts (src/protection/owner_alerts.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_session_repair_failed(_build_owner_alerts(self), *args, **kwargs)

    @staticmethod
    def _alert_owner_repair_resolved(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_repair_resolved (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_repair_resolved(*args, **kwargs)

    @staticmethod
    def _alert_owner_no_stop(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_no_stop (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_no_stop(*args, **kwargs)

    @staticmethod
    def _alert_owner_stop_pending_acceptance(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_stop_pending_acceptance (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_stop_pending_acceptance(*args, **kwargs)

    @staticmethod
    def _alert_owner_unreadable_stop(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_unreadable_stop (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_unreadable_stop(*args, **kwargs)

    @staticmethod
    def _alert_owner_exit_declined(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_exit_declined (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_exit_declined(*args, **kwargs)

    def _wire_protective_stop_block_recorder(self, *args, **kwargs):
        """Thin shim -> CoverageRepair (src/protection/coverage_repair.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return CoverageRepair._wire_protective_stop_block_recorder(_build_coverage_repair(self), *args, **kwargs)

    @staticmethod
    @staticmethod
    def _alert_owner_kill_switch_blocked(*args, **kwargs):
        """Thin shim -> CoverageRepair._alert_owner_kill_switch_blocked (static; src/protection/coverage_repair.py)."""
        return CoverageRepair._alert_owner_kill_switch_blocked(*args, **kwargs)

    def _repair_stop_coverage(self, *args, **kwargs):
        """Thin shim -> CoverageRepair (src/protection/coverage_repair.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return CoverageRepair._repair_stop_coverage(_build_coverage_repair(self), *args, **kwargs)

    def _submit_protected_sell(self, *args, **kwargs):
        """Thin shim -> ProtectedSell (src/protection/protected_sell.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ProtectedSell._submit_protected_sell(_build_protected_sell(self), *args, **kwargs)

    #: Exit orders this process submitted and waited on that did NOT reach a
    #: terminal broker state: {order_id: {symbol, submitted_qty}}.
    #: `wait_for_order_terminal` has a 15s ceiling and returns the LAST KNOWN
    #: status, so a marketable limit that does not fill in time leaves the
    #: order working while the code moves on and re-reads the book from the
    #: broker. That refreshed book still carries exposure already on its way
    #: out, and anything that then re-measures gross would shed it twice.
    #: Registered centrally in `_finalize_pending_protections` so EVERY exit
    #: path is covered by construction — the cash-deficit safety net, the
    #: gross-ceiling de-lever, the position reviewer's sells, the execution
    #: stage and the cash sweep — rather than by remembering to flag each one.
    _unsettled_exit_orders: dict[str, dict]

    def _register_exit_settlement(self, *args, **kwargs):
        """Thin shim -> ExitRelief (src/protection/exit_relief.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ExitRelief._register_exit_settlement(_build_exit_relief(self), *args, **kwargs)

    def _open_exit_relief(self, *args, **kwargs):
        """Thin shim -> ExitRelief (src/protection/exit_relief.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ExitRelief._open_exit_relief(_build_exit_relief(self), *args, **kwargs)

    def _finalize_pending_protections(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._finalize_pending_protections(_build_sell_finalization(self), *args, **kwargs)

    def _finalize_protection_after_sell(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._finalize_protection_after_sell(_build_sell_finalization(self), *args, **kwargs)

    def _finalize_protection_after_sell_core(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._finalize_protection_after_sell_core(_build_sell_finalization(self), *args, **kwargs)

    def _cancel_stray_stops_on_flat(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._cancel_stray_stops_on_flat(_build_sell_finalization(self), *args, **kwargs)

    def _write_ahead_protection_restore(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._write_ahead_protection_restore(_build_sell_finalization(self), *args, **kwargs)

    def _cancel_stops_with_write_ahead(self, *args, **kwargs):
        """Thin shim -> ProtectedSell (src/protection/protected_sell.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ProtectedSell._cancel_stops_with_write_ahead(_build_protected_sell(self), *args, **kwargs)

    def _restore_after_unconfirmed_sell(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._restore_after_unconfirmed_sell(_build_sell_finalization(self), *args, **kwargs)

    def _persist_orphaned_protection_restore(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._persist_orphaned_protection_restore(_build_sell_finalization(self), *args, **kwargs)

    def _derive_close_side_for_drain(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._derive_close_side_for_drain(_build_sell_finalization(self), *args, **kwargs)

    def _resolve_wal_row_side(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization
        return SellFinalization._resolve_wal_row_side(_build_sell_finalization(self), *args, **kwargs)

    def _drain_pending_repegs(self, *args, **kwargs):
        """Thin shim -> RepegDrain (src/protection/repeg_drain.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.repeg_drain import RepegDrain
        return RepegDrain._drain_pending_repegs(_build_repeg_drain(self), *args, **kwargs)

    def _delete_repeg_row(self, *args, **kwargs):
        """Thin shim -> RepegDrain (src/protection/repeg_drain.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.repeg_drain import RepegDrain
        return RepegDrain._delete_repeg_row(_build_repeg_drain(self), *args, **kwargs)

    def _drain_pending_protection_restores(self, *args, **kwargs):
        """Thin shim -> RestoreDrain (src/protection/restore_drain.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return RestoreDrain._drain_pending_protection_restores(_build_restore_drain(self), *args, **kwargs)

    def _reprotect_residual_after_partial_sell(self, *args, **kwargs):
        """Thin shim -> ReprotectResidual (this module; body over the 400-line ceiling for a new file); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ReprotectResidual._reprotect_residual_after_partial_sell(_build_reprotect_residual(self), *args, **kwargs)

    def _record_reprotect_identity_gap(self, *args, **kwargs):
        """Thin shim -> ReprotectRecords (src/protection/reprotect_records.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ReprotectRecords._record_reprotect_identity_gap(_build_reprotect_records(self), *args, **kwargs)

    def _alert_owner_reprotect_left_naked(self, *args, **kwargs):
        """Thin shim -> OwnerAlerts (src/protection/owner_alerts.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.owner_alerts import OwnerAlerts
        return OwnerAlerts._alert_owner_reprotect_left_naked(_build_owner_alerts(self), *args, **kwargs)

    @staticmethod
    def _order_accepted(*args, **kwargs):
        """Thin shim -> FillReconciler._order_accepted (static; src/protection/fill_reconciler.py)."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._order_accepted(*args, **kwargs)

    def _reconcile_fills(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._reconcile_fills(_build_fill_reconciler(self), *args, **kwargs)
            # Any other non-terminal status (new, accepted, pending_new, ...)
            # has nothing filled yet: stay 'submitted' for the next pass.

    def _reconcile_orphan_pending_submits(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._reconcile_orphan_pending_submits(_build_fill_reconciler(self), *args, **kwargs)

    @staticmethod
    def _parse_broker_fill_timestamp(*args, **kwargs):
        """Thin shim -> FillReconciler._parse_broker_fill_timestamp (static; src/protection/fill_reconciler.py)."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._parse_broker_fill_timestamp(*args, **kwargs)

    def _flag_stop_out_anomaly(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._flag_stop_out_anomaly(_build_fill_reconciler(self), *args, **kwargs)

    def _reconcile_stop_out_fills(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._reconcile_stop_out_fills(_build_fill_reconciler(self), *args, **kwargs)

    def _surface_reconcile_outcomes(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler
        return FillReconciler._surface_reconcile_outcomes(_build_fill_reconciler(self), *args, **kwargs)

    def _handle_ex_dividends(self, *args, **kwargs):
        """Thin shim -> ExDividends (src/protection/ex_dividends.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ExDividends._handle_ex_dividends(_build_ex_dividends(self), *args, **kwargs)
