"""The single door honours the owner's FREEZE (panel instalment 1).

Every write-capable `AlpacaBroker` method is wrapped here, so a freeze holds
for every caller at once: entries, scale-in, re-entry, rotation, de-lever and
the coverage sweep's repairs all reach the broker through these methods.

FREEZE (owner ruling 2026-10-09 ~23:20 UTC, superseding the same-day "pause
blocks sells too"): the desk keeps running and protecting what it holds --
stops, trails and exits all work -- but opens NOTHING new. Freeze is also the
future Sentinel's inbound seam: "a flag, never a call" (docs/FUTURE.md).

The rule is position-aware. An order is an ENTRY when it opens or increases
exposure, judged against the broker's own position read (`get_positions`,
ungated) at the moment of the order:
  - buy with no position or a long (open / add / re-entry), or buying more
    than a short holds (flip);
  - sell_short, always; a plain sell with no position or a short, or selling
    more than a long holds (flip);
  - every entry-limit re-peg (`replace_entry_limit`).
Entries are refused while frozen. Exits (selling at most the long held,
buying at most the short held, `close_position`), stop placement / amend /
repair and cancels go through. An order whose side, size or position cannot
be read is treated as an ENTRY (refused): when the door cannot tell, it opens
nothing.

OVERSELL (always, frozen or not, whenever the door is aimed): a plain `sell`
whose quantity exceeds the long freshly read from the broker is refused with a
per-symbol `oversell_refused` reason. A short is only ever opened with
`sell_short`, so a plain sell bigger than the long is never legitimate -- it is
an exit sized from a stale read (e.g. the resting stop filled in between) and
would open a short. Both door rules (freeze and oversell) compare quantities as
Decimal quantized to 9 dp (the broker's quantity precision, one shared helper),
so float noise such as 0.1+0.2 against 0.3 held is neither an oversell nor a flip.
Each refusal is recorded per symbol as a guarded pass
(`owner_flags_gate.oversell_refused` / `.positions_unreadable`); a failed
position read refuses the sell as `positions_unreadable`, the freeze's rule.

If the flag cannot be read the state is UNKNOWN: "cannot tell whether the owner
froze". UNKNOWN behaves exactly like frozen -- entries blocked, exits allowed --
and the owner is alerted once when the flag becomes unreadable and once when it
is readable again -- never once per order.

An UNAIMED door (no desk session: tools, tests, after `release`) reads no flag
and lets calls through; a desk session aimed at no database is UNKNOWN.
`main.main` aims the door (at nothing) before anything else runs, so a desk
session is never unaimed; an empty or missing path blocks like a freeze.

There is deliberately no per-position or per-symbol exclusion of any kind: the
desk manages every position it holds. Owner actions are instructions it carries
out, never an exemption from management.

`install` REFUSES TO LOAD if a write-capable method has no entry in `_REFUSALS`,
so a new broker verb cannot silently bypass the flag. Stored keys keep the old
names (PAUSE / RESUME, `paused`) so existing intent rows replay unchanged.
"""

from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import functools
import logging
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from src import owner_flags

logger = logging.getLogger(__name__)

_UNAIMED = object()
_db_path = _UNAIMED
WRITE_PREFIXES = (
    "submit_",
    "place_",
    "replace_",
    "cancel_",
    "close_",
    "shift_",
    "liquidate_",
)
#: Always an entry: a re-peg only ever moves a working ENTRY limit.
FREEZE_BLOCKS = frozenset({"replace_entry_limit"})
#: Judged per order against the held position: entry refused, exit allowed.
FREEZE_JUDGES = frozenset({"submit_order"})
_HALT = lambda why: {"id": None, "status": "owner_flag_halted", "reason": why}  # noqa: E731

# method -> the value returned in place of the call when it is refused
_REFUSALS = {
    "submit_order": _HALT,
    "close_position": _HALT,
    "replace_entry_limit": _HALT,
    "cancel_entry_order": lambda why: False,
    "cancel_open_orders": lambda why: 0,
    "cancel_open_entry_orders": lambda why: 0,
    "cancel_snapshotted_stops": lambda why: False,
    "cancel_protective_stops": lambda why: (False, []),
    "cancel_stray_protective_stops": lambda why: 0,
    "place_entry_protection": lambda why: None,
    "shift_stops_down": lambda why: None,
    "replace_stop_loss": lambda why: None,
    "replace_order_qty": lambda why: (False, why),
}


def configure(db_path) -> None:
    """Aim the door at this session's intent record; `release` ends that aim.

    The pointer is process-wide, so its lifetime must be the SESSION that set
    it, not the process. `main.main` pairs this with `release` in its finally
    block: without that pairing the pointer outlives the database it names
    (one session per process in production hides it; a test run does not),
    and every later read escalates to UNKNOWN against a database that is gone.
    """
    global _db_path, _was_unknown
    _db_path = db_path
    _was_unknown = False


def release() -> None:
    """End the aim set by `configure`; an unaimed door reads no flags."""
    global _db_path, _was_unknown
    _db_path = _UNAIMED
    _was_unknown = False


def _send_owner_alert(text: str) -> None:
    from src.notifier.category import CATEGORY_OPERATIONAL
    from src.notifier.owner_alert import send_owner_alert

    send_owner_alert(text, category=CATEGORY_OPERATIONAL)


#: Called with an alert text when the flag state CHANGES between readable and
#: UNKNOWN (once per change, never per order). Defaults to the owner alert.
unknown_state_recorder = _send_owner_alert
unknown_flag_state_calls = 0
_was_unknown = False


def _note_state(unknown: bool, name: str) -> None:
    global _was_unknown
    if unknown == _was_unknown:
        return
    _was_unknown = unknown
    if unknown:
        text = (
            "Owner Freeze flag UNREADABLE: desk treats itself as FROZEN -- no new "
            "positions or adds until it reads again; exits and stops keep working "
            f"(first checked: {name})."
        )
    else:
        text = "Owner Freeze flag readable again: desk orders follow the flag as normal."
    try:
        unknown_state_recorder(text)
    except Exception as exc:  # noqa: BLE001 - alerting never decides
        record_guarded_pass(NO_LEDGER, "owner_flags_gate.unknown_state_recorder", exc)


#: The broker's quantity precision (Alpaca: 9 decimal places).
QTY_QUANTUM = Decimal("1e-9")


def _at_broker_precision(q: Decimal) -> Decimal:
    """The ONE precision both door rules (freeze and oversell) compare at."""
    return q.quantize(QTY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _order_terms(args, kwargs):
    """(symbol, qty, side) of a `submit_order` call, or None if unreadable."""
    try:
        symbol = kwargs["symbol"] if "symbol" in kwargs else args[0]
        qty = _at_broker_precision(Decimal(str(kwargs["qty"] if "qty" in kwargs else args[1])))
        side = str(kwargs["side"] if "side" in kwargs else args[2]).lower()
    except (IndexError, KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not isinstance(symbol, str) or not qty.is_finite() or not qty > 0:
        return None
    return symbol, qty, side


class PositionsUnreadable(RuntimeError):
    """The broker position read failed, so entry vs exit cannot be told."""


def _held_qty(broker, symbol) -> Decimal:
    """Signed quantity held in `symbol` (0 if none), as Decimal at the broker's 9 dp precision.

    Raises PositionsUnreadable when the broker read fails; `_verdict` turns
    that into a refusal ("positions_unreadable").
    """
    want = symbol.strip().upper().replace("/", "")
    try:
        positions = list(broker.get_positions())
    except Exception as exc:  # noqa: BLE001 - re-raised as a named failure, never swallowed
        raise PositionsUnreadable(f"{type(exc).__name__}: {exc}") from exc
    total = Decimal(0)
    for p in positions:
        if str(p.symbol).strip().upper().replace("/", "") == want:
            total += Decimal(str(p.qty))
    return _at_broker_precision(total)


def is_entry(broker, name, args, kwargs) -> bool:
    """True when this write opens or increases exposure (see module docstring)."""
    if name in FREEZE_BLOCKS:
        return True
    if name not in FREEZE_JUDGES:
        return False
    terms = _order_terms(args, kwargs or {})
    if terms is None:
        return True
    symbol, qty, side = terms
    if side not in ("buy", "sell"):
        return True  # sell_short (and anything unrecognised) opens or adds
    held = _held_qty(broker, symbol)
    if side == "sell":
        return not (held > 0 and qty <= held)
    return not (held < 0 and qty <= -held)


class DoorRefusal(RuntimeError):
    """A broker-door refusal, recorded durably per symbol (never raised)."""


def _record_refusal(broker, args, kwargs, why: str) -> None:
    """Durable, machine-readable, per-symbol record of an oversell or unreadable-position refusal."""
    kind = why.split(":", 1)[0]
    if kind not in ("oversell_refused", "positions_unreadable"):
        return
    terms = _order_terms(args, kwargs or {})
    context = {"symbol": terms[0] if terms else None, "qty": str(terms[1]) if terms else None, "reason": why}
    record_guarded_pass(broker, f"owner_flags_gate.{kind}", DoorRefusal(why), context=context)


def oversell_reason(broker, name, args, kwargs):
    """Named refusal when a plain `sell` exceeds the long held, else None.

    Raises PositionsUnreadable when the fresh position read fails.
    """
    if name not in FREEZE_JUDGES:
        return None
    terms = _order_terms(args, kwargs or {})
    if terms is None or terms[2] != "sell":
        return None
    symbol, qty, _side = terms
    held = _held_qty(broker, symbol)
    if qty <= held:
        return None
    return (
        f"oversell_refused: {symbol} sell of {qty} exceeds the {max(held, Decimal(0))} long held; it would open a short"
    )


def _verdict(name, broker=None, args=(), kwargs=None):
    """Return a refusal reason, or None to let the call through."""
    global unknown_flag_state_calls
    if _db_path is _UNAIMED:
        return None
    flags = owner_flags.read_flags(_db_path)
    _note_state(flags.unknown, name)
    try:
        over = oversell_reason(broker, name, args, kwargs)
    except PositionsUnreadable as exc:
        logger.error("oversell gate refused %s: positions_unreadable (%s)", name, exc)
        return "positions_unreadable: a plain sell cannot be checked against the long held"
    if over is not None:
        logger.error("broker door refused %s: %s", name, over)
        return over
    if flags.unknown:
        unknown_flag_state_calls += 1
        logger.error("desk running on an UNKNOWN owner-flag state (%s)", name)
        why = "owner Freeze flag unreadable: treated as frozen, no new exposure"
    elif flags.frozen:
        why = "desk is frozen by the owner: no new exposure"
    else:
        return None
    try:
        return why if is_entry(broker, name, args, kwargs) else None
    except PositionsUnreadable as exc:
        logger.error("freeze gate refused %s: positions_unreadable (%s)", name, exc)
        return f"positions_unreadable: {why}"


def _wrap(name, orig):
    @functools.wraps(orig)
    def gated(self, *args, **kwargs):
        why = _verdict(name, self, args, kwargs)
        if why is not None:
            logger.warning("owner flag refused %s: %s", name, why)
            _record_refusal(self, args, kwargs, why)
            return _REFUSALS[name](why)
        return orig(self, *args, **kwargs)

    gated._owner_flag_gated = True
    return gated


def write_methods(cls) -> list:
    return sorted(
        n for n, v in vars(cls).items() if callable(v) and not n.startswith("_") and n.startswith(WRITE_PREFIXES)
    )


def install(cls) -> None:
    missing = [n for n in write_methods(cls) if n not in _REFUSALS]
    if missing:
        raise RuntimeError(f"broker write methods with no owner-flag rule: {missing}")
    for n in write_methods(cls):
        m = getattr(cls, n)
        if not getattr(m, "_owner_flag_gated", False):
            setattr(cls, n, _wrap(n, m))
