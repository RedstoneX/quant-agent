"""Long scale-in path B: cancel the resting protective sell, confirm, buy, rearm.

Owner ruling 2026-09-15: a BUY add on a name that already has a resting
protective sell (stop / stop_limit) is allowed ONLY through this sequence,
never by silently refusing the add.

    1. Snapshot the protective sell and persist a WAL recovery row.
    2. Cancel it.
    3. Confirm the cancel via the broker's own order status — do not assume.
       (Was `trade_updates` first; that socket is off since 2026-09-17, so
       this is the bounded REST wait. Same call, same window.)
    4. Submit the BUY add.
    5. On any fill (including partial): place ONE protective sell covering
       the broker's FULL position quantity at the existing protection level
       (most-protective of the cancelled stop and the add's own stop).
    6. If rearm fails: fail-closed owner alert. Do not leave the position
       naked quietly.

WHY THIS EXISTS (Alpaca wash-trade / opposite-side block)
---------------------------------------------------------
A resting SELL stop and a new BUY cannot both be working on the same
symbol. The old whole-book daily breaker cancelled every protective stop,
failed to sell on a gap, and restored — leaving an unprotected window on
the exact day it existed for (`docs/INCIDENT_HISTORY.md`, 2026-09-14).
This path is the same cancel-to-free-shares shape, but:

  * it is one symbol, one add, not the whole book;
  * cancel is confirmed before the BUY is sent;
  * a crash mid-sequence is recovered from the WAL row using the broker's
    CURRENT quantity, never the cancelled-stop size or the add's fill size;
  * trail / in-session coverage repair / the coverage watchdog skip a
    symbol that has a live scale-in WAL row so they cannot re-place the
    stop we just cancelled (which would re-create the wash-trade block)
    and cannot resize it while the add is in flight.

SHORT ADDS
----------
Not built. This sequence cancels a *sell*-stop, buys, and rearms a sell-stop.
A short add would have to cancel a *buy*-stop and rearm it, which is a
different path. A SHORT that opens a flat name is unchanged. Adding to an
existing short is recorded as `short_add_blocked` and never cancels a BUY
stop. Missing short stops are repaired separately (item 73, closed).

No new magic percentages. The stop trigger is the live protective level
already at the broker (or the add's reviewed stop if that is tighter).
The 3% stop-limit buffer is `AlpacaBroker.STOP_LIMIT_BUFFER_PCT`, the
same one every other protective-stop path uses. `execution.repeg_enabled`
is not consulted and is not changed.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Distinct from `_WAL_SELL_SENTINEL` (`__WAL_PENDING__`). Drain must NOT
#: run the SELL-finalize math on a scale-in row: a grown position would
#: restore the OLD stop size and leave the add naked — the partial-fill
#: size bug this path exists to close.
WAL_SCALE_IN_SENTINEL = "__WAL_SCALE_IN__"

#: Terminal statuses that mean the protective sell is gone and did not
#: sell shares. `filled` is deliberately NOT here: a stop that fires
#: during cancel is a real exit and the BUY add must not proceed.
_CANCEL_CONFIRMED = frozenset({
    "canceled", "cancelled", "expired", "rejected", "replaced",
})
_STOP_FILLED = frozenset({"filled"})

_SESSION_LOCK_DIR = Path.home() / ".cache" / "quant-agent" / "active-session.lock"


@dataclass
class LongAddPrep:
    """Outcome of preparing a long BUY that may be an add to a held name."""

    is_scale_in: bool = False
    cancelled: bool = False
    wal_row_id: int | None = None
    specs: list[dict] = field(default_factory=list)
    intended_stop: float = 0.0
    held_qty_before: float = 0.0
    skip_reason: str | None = None
    skip_detail: str = ""

    @classmethod
    def not_scale_in(cls) -> "LongAddPrep":
        return cls()


def held_signed_qty(positions: list | None, symbol: str) -> float:
    """Broker-signed quantity for `symbol` from a positions list. 0 if absent."""
    want = str(symbol or "").upper()
    for pos in positions or []:
        if str(getattr(pos, "symbol", "") or "").upper() != want:
            continue
        try:
            return float(getattr(pos, "qty", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def short_add_is_blocked(positions: list | None, symbol: str) -> bool:
    """True when SHORT would add to an existing short. Item 73 must land first."""
    return held_signed_qty(positions, symbol) < 0


def most_protective_long_stop(prices: list[float]) -> float:
    """Tightest long stop: highest trigger below price. 0 if none usable."""
    usable = [float(p) for p in prices if isinstance(p, (int, float)) and p > 0]
    return max(usable) if usable else 0.0


def cancelled_stop_specs(specs: list[dict] | None) -> list[dict]:
    """Specs that were actually at the broker (have an id)."""
    out: list[dict] = []
    for spec in specs or []:
        if spec.get("id"):
            out.append(spec)
    return out


def intended_specs(cancelled: list[dict], intended_stop: float) -> list[dict]:
    """Cancelled snapshots plus the add's own stop, for drain most-protective."""
    specs = [dict(s) for s in cancelled]
    if intended_stop > 0:
        specs.append({
            "id": None,
            "qty": 0.0,
            "stop_price": float(intended_stop),
            "limit_price": None,
            "role": "intended",
        })
    return specs


def confirm_protective_cancels(broker: Any, specs: list[dict]) -> tuple[bool, str]:
    """Wait until each cancelled stop is terminal, via `wait_for_order_terminal`.

    That is the websocket first when `execution.fill_stream_enabled` is on
    (it is, since 2026-09-18), and bounded REST polling otherwise. The wait
    itself is unchanged either way.

    Returns ``(True, "")`` only when every spec with an id reached a
    cancelled-like terminal state. A fill aborts the add: the stop did its
    job and buying more would be adding into an exit.
    """
    for spec in cancelled_stop_specs(specs):
        order_id = str(spec.get("id") or "")
        if not order_id:
            return False, "a cancelled stop had no id to confirm"
        try:
            status = broker.wait_for_order_terminal(order_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"cancel confirm raised for {order_id}: {exc}"
        status = str(status or "").lower()
        if status in _STOP_FILLED:
            return False, (
                f"protective stop {order_id} FILLED during cancel — "
                "the add is aborted rather than buying into an exit"
            )
        if status not in _CANCEL_CONFIRMED:
            return False, (
                f"protective stop {order_id} not confirmed cancelled "
                f"(status={status or 'unknown'})"
            )
    return True, ""


def broker_position_qty(broker: Any, symbol: str) -> float | None:
    """Signed broker qty for `symbol`. 0 if flat. None if the broker could not be asked."""
    try:
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scale-in: get_positions failed for %s: %s", symbol, exc)
        return None
    if not isinstance(positions, list):
        return None
    return held_signed_qty(positions, symbol)


def rearm_full_position_stop(
    broker: Any, *, symbol: str, qty: float, stop_price: float,
    db: Any = None,
) -> dict | None:
    """Place ONE protective SELL covering `qty` at `stop_price`.

    Routes through `_submit_protective_stop_retrying` so hybrid GTC+DAY
    fractional legs and the §11.1 retry burst are the same as every other
    protective-stop path. No take-profit, no bracket, no invented buffer.
    On accept, writes `stop_price` back onto the opening row when `db` is
    given — the rearmed level can be tighter than the add's own stop.
    """
    # docs/WORK.md item 88: a non-finite trigger passed the old `<= 0` test
    # (NaN/Inf compare False) and reached the broker, where quantization
    # turned it into None. Returning None here is the caller's failure
    # signal — the WAL row stays and the gap keeps escalating.
    from src.execution.stop_records import STOP_USABLE, classify_stop_price

    if qty <= 0 or classify_stop_price(stop_price)[0] != STOP_USABLE:
        logger.error(
            "scale-in rearm REFUSED for %s: qty=%r stop=%r is not a "
            "placeable protective stop — nothing placed, the gap stays open",
            symbol, qty, stop_price,
        )
        return None
    buffer = getattr(broker, "STOP_LIMIT_BUFFER_PCT", 0.03)
    try:
        buffer = float(buffer)
    except (TypeError, ValueError):
        buffer = 0.03
    result = broker._submit_protective_stop_retrying(
        symbol=symbol, qty=qty, stop_price=stop_price,
        limit_price=stop_price * (1 - buffer), side="sell",
    )
    from src.execution.stop_records import accepted_stop_order, write_back_stop_loss
    if accepted_stop_order(result) and db is not None:
        write_back_stop_loss(db, symbol, stop_price, is_short=False)
    return result


def restore_cancelled_stops(
    broker: Any, symbol: str, specs: list[dict],
) -> bool:
    """Put the snapshotted protective sells back. True when none remain failed."""
    to_restore = cancelled_stop_specs(specs)
    if not to_restore:
        return True
    try:
        _restored, failed = broker._restore_stop_orders(
            symbol, to_restore, check_idempotency=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "scale-in: restore of cancelled stops failed for %s: %s",
            symbol, exc,
        )
        return False
    return not failed


def discharge_scale_in_wal(db: Any, wal_row_id: int | None) -> None:
    if wal_row_id is None:
        return
    try:
        db.delete_pending_protection_restore(wal_row_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scale-in: failed to discharge WAL row %s: %s (drain will retry)",
            wal_row_id, exc,
        )


def prepare_long_add(
    *,
    broker: Any,
    db: Any,
    symbol: str,
    positions: list | None,
    intended_stop: float,
) -> LongAddPrep:
    """Cancel-confirm a resting protective sell so a long add can submit.

    No-op (``is_scale_in=False``) when the name is not already held long.
    Scale-in with no live stop: ``is_scale_in=True`` and nothing cancelled —
    the fill path still rearms to broker full qty.
    """
    held = held_signed_qty(positions, symbol)
    if held <= 0:
        return LongAddPrep.not_scale_in()

    intended = most_protective_long_stop([intended_stop])
    prep = LongAddPrep(
        is_scale_in=True,
        held_qty_before=held,
        intended_stop=intended,
    )
    try:
        ok, specs = broker.snapshot_protective_stops(symbol, side="sell")
    except Exception as exc:  # noqa: BLE001
        logger.error("scale-in: snapshot failed for %s: %s", symbol, exc)
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = f"could not read the resting protective sell ({exc})"
        return prep
    if not ok:
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "could not read the resting protective sell"
        return prep
    live = cancelled_stop_specs(specs or [])
    if not live:
        # Naked add: nothing to cancel. Fill path still covers full qty.
        return prep

    live_stops = [float(s.get("stop_price") or 0) for s in live]
    prep.intended_stop = most_protective_long_stop(live_stops + [intended])
    wal_specs = intended_specs(live, prep.intended_stop)
    prep.specs = wal_specs
    try:
        wal_row_id = db.insert_pending_protection_restore(
            symbol=symbol,
            sell_order_id=WAL_SCALE_IN_SENTINEL,
            position_qty_before_sell=held,
            specs_json=json.dumps(wal_specs),
            side="sell",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "scale-in: WAL write failed for %s: %s — refusing the add "
            "rather than cancelling without crash recovery", symbol, exc,
        )
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "could not persist the protection-restore intent before cancel"
        return prep
    prep.wal_row_id = wal_row_id if isinstance(wal_row_id, int) else wal_row_id

    if not broker.cancel_snapshotted_stops(symbol, live):
        discharge_scale_in_wal(db, prep.wal_row_id)
        prep.wal_row_id = None
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "protective sell could not be cancelled (rolled back)"
        return prep

    confirmed, detail = confirm_protective_cancels(broker, live)
    if not confirmed:
        restored = restore_cancelled_stops(broker, symbol, live)
        if restored:
            discharge_scale_in_wal(db, prep.wal_row_id)
            prep.wal_row_id = None
        prep.skip_reason = "scale_in_cancel_unconfirmed"
        prep.skip_detail = detail
        logger.error("scale-in: %s — add skipped for %s", detail, symbol)
        return prep

    prep.cancelled = True
    logger.info(
        "scale-in: cancelled and confirmed %d protective sell(s) for %s "
        "so a BUY add can submit; WAL row %s covers the unprotected window",
        len(live), symbol, prep.wal_row_id,
    )
    return prep


def restore_after_failed_add(
    broker: Any, db: Any, prep: LongAddPrep, symbol: str,
) -> None:
    """BUY never landed — put the original protective sell back if we cancelled it."""
    if not prep.cancelled:
        discharge_scale_in_wal(db, prep.wal_row_id)
        return
    if restore_cancelled_stops(broker, symbol, prep.specs):
        discharge_scale_in_wal(db, prep.wal_row_id)
        return
    logger.critical(
        "scale-in: BUY add for %s failed AND the original protective sell "
        "could not be restored — WAL row %s remains so drain/watchdog can "
        "rearm; OWNER must be alerted",
        symbol, prep.wal_row_id,
    )


def cover_qty_for_rearm(
    broker: Any, *, symbol: str, filled_qty: float, held_qty_before: float,
) -> float:
    """Quantity the post-fill protective sell must cover.

    Broker full position is the authority (partial fill of the add must
    not size the stop to the add alone). If the broker cannot be asked,
    fall back to filled + held-before — the two quantities we already
    measured — rather than inventing a third number.
    """
    current = broker_position_qty(broker, symbol)
    if current is not None:
        return max(0.0, abs(float(current)))
    try:
        filled = float(filled_qty or 0)
    except (TypeError, ValueError):
        filled = 0.0
    try:
        held = float(held_qty_before or 0)
    except (TypeError, ValueError):
        held = 0.0
    logger.warning(
        "scale-in: broker qty unreadable for %s — covering filled (%.4f) "
        "+ held-before (%.4f)", symbol, filled, held,
    )
    return max(0.0, filled + max(0.0, held))


def list_open_entry_ids(broker: Any, symbol: str) -> list[str]:
    """Working non-stop entry order ids for `symbol`. Empty on failure."""
    lister = getattr(broker, "list_open_entry_order_ids", None)
    if callable(lister):
        try:
            ids = lister(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scale-in: list_open_entry_order_ids failed for %s: %s",
                symbol, exc,
            )
            return []
        if isinstance(ids, list):
            return [str(i) for i in ids if i]
        return []
    return []


def trading_session_lock_held() -> bool:
    """True only when the wrapper's session lock directory is present.

    Used by the coverage watchdog so it does not ADD a stop during a live
    cancel-confirm-buy window. Absence is treated as no session — crash
    recovery may rearm. Unknown/OS error is also treated as no session:
    failing to repair after a crash is worse than a wash-trade reject of
    an add that then restores.
    """
    try:
        return _SESSION_LOCK_DIR.is_dir()
    except OSError:
        return False


def pending_scale_in_symbols(db: Any) -> set[str]:
    try:
        rows = db.get_pending_protection_restores()
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for row in rows or []:
        if row.get("sell_order_id") == WAL_SCALE_IN_SENTINEL and row.get("symbol"):
            out.add(str(row["symbol"]))
    return out


def pending_protection_symbols(db: Any) -> set[str]:
    """Every WAL-owned symbol — scale-in and sell — so trail/repair skip them."""
    try:
        rows = db.get_pending_protection_restores()
    except Exception:  # noqa: BLE001
        return set()
    return {str(r["symbol"]) for r in (rows or []) if r.get("symbol")}


def scale_in_symbols_to_skip(broker: Any, db: Any) -> set[str]:
    """Scale-in symbols the watchdog/repair must not touch right now."""
    symbols = pending_scale_in_symbols(db)
    if not symbols:
        return set()
    if trading_session_lock_held():
        return set(symbols)
    skip: set[str] = set()
    for symbol in symbols:
        if list_open_entry_ids(broker, symbol):
            skip.add(symbol)
    return skip


def drain_scale_in_row(broker: Any, db: Any, row: dict) -> bool:
    """Crash recovery for a scale-in WAL row. True when coverage is known-good.

    Cancels any leftover DAY entry (the add may still be working after a
    kill), confirms that, then places ONE protective sell covering the
    broker's current quantity at the most-protective stored stop. A working
    entry that will not confirm leaves the row in place.
    """
    symbol = row.get("symbol") or ""
    try:
        specs = json.loads(row.get("specs_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.error(
            "scale-in drain: row %s has unparseable specs (%s)",
            row.get("id"), exc,
        )
        return False
    stop_price = most_protective_long_stop(
        [float(s.get("stop_price") or 0) for s in specs],
    )
    entry_ids = list_open_entry_ids(broker, symbol)
    for order_id in entry_ids:
        try:
            broker.cancel_entry_order(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scale-in drain: cancel of leftover entry %s for %s failed: %s",
                order_id, symbol, exc,
            )
        try:
            status = str(broker.wait_for_order_terminal(order_id) or "").lower()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scale-in drain: confirm of leftover entry %s for %s failed: %s",
                order_id, symbol, exc,
            )
            return False
        if status and status not in (
            _CANCEL_CONFIRMED | _STOP_FILLED | frozenset({"done_for_day"})
        ):
            logger.warning(
                "scale-in drain: leftover entry %s for %s still %s — leaving WAL",
                order_id, symbol, status,
            )
            return False

    qty = broker_position_qty(broker, symbol)
    if qty is None:
        return False
    held = abs(float(qty))
    if held <= 0:
        return True
    # item 88: `<= 0` alone let a recorded ±Inf through to the broker.
    from src.execution.stop_records import STOP_USABLE, classify_stop_price

    if classify_stop_price(stop_price)[0] != STOP_USABLE:
        logger.error(
            "scale-in drain: %s held %.4f but the WAL row's stop price is "
            "%r, which cannot be a stop — leaving the row so the owner alert "
            "/ next repair can see it", symbol, held, stop_price,
        )
        return False

    try:
        existing_ok, existing = broker.snapshot_protective_stops(symbol, side="sell")
    except Exception:
        existing_ok, existing = False, []
    covered = 0.0
    if existing_ok:
        for spec in existing or []:
            try:
                covered += float(spec.get("qty") or 0)
            except (TypeError, ValueError):
                continue
    if covered + 1e-6 >= held:
        return True

    placed = rearm_full_position_stop(
        broker, symbol=symbol, qty=held, stop_price=stop_price, db=db,
    )
    if placed is None:
        logger.error(
            "scale-in drain: rearm FAILED for %s qty=%.4f stop=$%.2f",
            symbol, held, stop_price,
        )
        return False
    uncovered = 0.0
    if isinstance(placed, dict):
        try:
            uncovered = float(placed.get("uncovered_qty") or 0)
        except (TypeError, ValueError):
            uncovered = 0.0
    return uncovered <= 0


def alert_rearm_failed(*, symbol: str, qty: float, stop_price: float,
                       order_id: str | None, detail: str = "") -> None:
    """Fail-closed owner page when the post-add protective sell did not land."""
    body = (
        "STOP NOT REARMED AFTER A SCALE-IN\n"
        f"{symbol}: the desk cancelled the resting protective sell so it "
        f"could add, the add filled, and putting ONE protective sell back "
        f"over the full position ({qty:g} share(s) at {stop_price}) failed. "
        "The position is open at the broker with nothing standing watch.\n"
        f"Entry order: {order_id or 'unknown'}\n"
        f"{detail or 'Place a stop manually or flatten. The coverage sweep will also try to repair it.'}"
    )
    try:
        from src import notifier as _notifier
        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        logger.error("scale-in rearm-failure owner alert failed: %s", exc)


def pending_scale_in_symbols_from_path(db_path: str | os.PathLike | None) -> set[str]:
    """Read-only lookup for the coverage watchdog. Empty on any failure."""
    if not db_path:
        return set()
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT symbol FROM pending_protection_restores "
                "WHERE sell_order_id = ?",
                (WAL_SCALE_IN_SENTINEL,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return set()
    return {str(r["symbol"]) for r in rows if r["symbol"]}
