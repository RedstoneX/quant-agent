"""The single door honours the owner's flags (panel instalment 1).

Every write-capable `AlpacaBroker` method is wrapped here, so PAUSE and
NEVER-TOUCH hold for every caller at once: exits, sizing and scale-in,
rotation, de-lever, protection upkeep and the coverage sweep's repairs all
reach the broker through these methods. Reads are untouched, so a listed
position is still reported everywhere.

PAUSE blocks the trading verbs (PAUSE_BLOCKS) and leaves protective-stop upkeep
and cancels running: a paused desk must not leave positions naked.
NEVER-TOUCH blocks every verb for that symbol.

If the flags cannot be read (retry, then the last known set, then nothing),
new exposure and closes are refused while protection keeps running.

There is deliberately no per-position "hands off": the desk manages every
position it holds; owner interventions are instructions it executes.

`install` REFUSES TO LOAD if a write-capable method has no entry in `_RULES`,
so a new broker verb cannot silently bypass the flags.
"""
import functools
import logging

from src import owner_flags

logger = logging.getLogger(__name__)

_db_path = None
WRITE_PREFIXES = (
    "submit_", "place_", "replace_", "cancel_", "close_", "shift_", "liquidate_",
)
PAUSE_BLOCKS = frozenset({"submit_order", "replace_entry_limit", "close_position"})
_HALT = lambda why: {"id": None, "status": "owner_flag_halted", "reason": why}  # noqa: E731

# method -> (how to find the symbol, refusal value)
#   "arg":      symbol is first positional / `symbol=`
#   "optarg":   same, but None means wholesale
#   "order":    first arg is an order id; symbol looked up from the broker
#   "wholesale": no symbol, touches every order
_RULES = {
    "submit_order": ("arg", _HALT),
    "close_position": ("arg", _HALT),
    "replace_entry_limit": ("order", _HALT),
    "cancel_entry_order": ("order", lambda why: False),
    "cancel_open_orders": ("wholesale", lambda why: 0),
    "cancel_open_entry_orders": ("optarg", lambda why: 0),
    "cancel_snapshotted_stops": ("arg", lambda why: False),
    "cancel_protective_stops": ("arg", lambda why: (False, [])),
    "cancel_stray_protective_stops": ("arg", lambda why: 0),
    "place_entry_protection": ("arg", lambda why: None),
    "shift_stops_down": ("arg", lambda why: None),
    "replace_stop_loss": ("arg", lambda why: None),
}


def configure(db_path) -> None:
    global _db_path
    _db_path = db_path


def _symbol(broker, name, kind, args, kwargs):
    if kind in ("arg", "optarg"):
        s = kwargs.get("symbol", args[0] if args else None)
    elif kind == "order":
        oid = kwargs.get("order_id", args[0] if args else None)
        try:
            s = broker.client.get_order_by_id(oid).symbol
        except Exception as exc:  # noqa: BLE001
            logger.warning("owner flags: cannot resolve order %s to a symbol: %s", oid, exc)
            s = None
    else:
        s = None
    return s.upper() if isinstance(s, str) else None


#: Called with a reason string whenever the desk acts on an UNKNOWN flag set.
#: Recording only (like the broker's protective_stop_block_recorder); None logs.
unknown_state_recorder = None
unknown_flag_state_calls = 0


def _verdict(broker, name, args, kwargs):
    """Return a refusal reason, or None to let the call through."""
    global unknown_flag_state_calls
    kind = _RULES[name][0]
    flags = owner_flags.read_flags(_db_path)
    if flags.unknown:
        # Cannot tell what the owner flagged. New exposure and closes are
        # refused; protection (stops, amends, cancels) keeps running.
        unknown_flag_state_calls += 1
        why = "owner flags unreadable and no last known set"
        logger.error("desk running on an UNKNOWN owner-flag state (%s)", name)
        if unknown_state_recorder is not None:
            try:
                unknown_state_recorder(f"{name}: {why}")
            except Exception:  # noqa: BLE001 - recording never decides
                pass
        if name in PAUSE_BLOCKS:
            return why
        return None
    if flags.paused and name in PAUSE_BLOCKS:
        return "desk is paused by the owner"
    sym = _symbol(broker, name, kind, args, kwargs)
    if sym is not None and sym in flags.never_touch:
        return f"{sym} is on the owner's never-touch list"
    if sym is None and kind == "order" and flags.never_touch:
        return "order's symbol cannot be resolved while owner-flagged symbols exist"
    return None


class CancelCount(int):
    """A cancel count that also says which flagged symbols were left alone."""
    skipped: tuple = ()


def _partial_cancel(self, name, flags):
    """Wholesale cancel minus the owner-flagged names, saying who was skipped.

    Each cancel goes back through the gated `cancel_entry_order`, so there is
    still exactly one door.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    entries_only = name == "cancel_open_entry_orders"
    count, skipped = 0, []
    orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
    for o in orders or []:
        oid, sym = getattr(o, "id", None), str(getattr(o, "symbol", "")).upper()
        otype = str(getattr(getattr(o, "order_type", None), "value", getattr(o, "order_type", ""))).lower()
        if not oid or (entries_only and "stop" in otype):
            continue
        if sym in flags.never_touch:
            skipped.append((sym, "never-touch"))
            continue
        if self.cancel_entry_order(oid):
            count += 1
    result = CancelCount(count)
    result.skipped = tuple(skipped)
    if skipped:
        logger.warning("owner flags: %s skipped %s", name, skipped)
    return result


def _wrap(name, orig):
    @functools.wraps(orig)
    def gated(self, *args, **kwargs):
        why = _verdict(self, name, args, kwargs)
        if why is None and _RULES[name][0] in ("wholesale", "optarg"):
            sym = _symbol(self, name, _RULES[name][0], args, kwargs)
            flags = owner_flags.read_flags(_db_path)
            if sym is None and flags.never_touch:
                return _partial_cancel(self, name, flags)
        if why is not None:
            logger.warning("owner flag refused %s: %s", name, why)
            return _RULES[name][1](why)
        return orig(self, *args, **kwargs)
    gated._owner_flag_gated = True
    return gated


def write_methods(cls) -> list:
    return sorted(n for n, v in vars(cls).items()
                  if callable(v) and not n.startswith("_") and n.startswith(WRITE_PREFIXES))


def install(cls) -> None:
    missing = [n for n in write_methods(cls) if n not in _RULES]
    if missing:
        raise RuntimeError(f"broker write methods with no owner-flag rule: {missing}")
    for n in write_methods(cls):
        m = getattr(cls, n)
        if not getattr(m, "_owner_flag_gated", False):
            setattr(cls, n, _wrap(n, m))
