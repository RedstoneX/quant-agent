"""The single door honours the owner's flags (panel instalment 1).

Every write-capable `AlpacaBroker` method is wrapped here, so PAUSE, NEVER-TOUCH
and HANDS-OFF hold for every caller at once: exits, sizing and scale-in,
rotation, de-lever, protection upkeep and the coverage sweep's repairs all
reach the broker through these methods. Reads are untouched, so a hands-off
position is still reported everywhere.

PAUSE blocks the trading verbs (PAUSE_BLOCKS) and leaves protective-stop upkeep
and cancels running: a paused desk must not leave positions naked.
NEVER-TOUCH / HANDS-OFF block every verb for that symbol, stops included: the
owner has taken the position, and a stop he set is honoured, not moved.

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


def _verdict(broker, name, args, kwargs):
    """Return a refusal reason, or None to let the call through."""
    kind = _RULES[name][0]
    flags = owner_flags.read_flags(_db_path)
    if flags.paused and name in PAUSE_BLOCKS:
        return "desk is paused by the owner"
    sym = _symbol(broker, name, kind, args, kwargs)
    if sym is not None and sym in flags.untouchable:
        why = "never-touch" if sym in flags.never_touch else "hands-off"
        return f"{sym} is {why} by the owner"
    if sym is None and kind in ("wholesale", "optarg") and flags.untouchable:
        return "wholesale cancel would reach owner-flagged symbols"
    return None


def _wrap(name, orig):
    @functools.wraps(orig)
    def gated(self, *args, **kwargs):
        why = _verdict(self, name, args, kwargs)
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
