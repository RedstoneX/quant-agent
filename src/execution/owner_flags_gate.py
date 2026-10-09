"""The single door honours the owner's PAUSE (panel instalment 1).

Every write-capable `AlpacaBroker` method is wrapped here, so a pause holds for
every caller at once: exits, sizing and scale-in, rotation, de-lever and the
coverage sweep's repairs all reach the broker through these methods.

PAUSE blocks the trading verbs (PAUSE_BLOCKS) and leaves protective-stop upkeep
and cancels running: a paused desk must not leave positions naked.

If the flag cannot be read (retry, then the last known copy, then nothing) the
state is UNKNOWN: "cannot tell whether the owner paused". New exposure is
refused (UNKNOWN_BLOCKS); protection and closes, which reduce exposure, go on.

There is deliberately no per-position or per-symbol exclusion of any kind: the
desk manages every position it holds. Owner actions are instructions it carries
out, never an exemption from management.

`install` REFUSES TO LOAD if a write-capable method has no entry in `_REFUSALS`,
so a new broker verb cannot silently bypass the flag.
"""

from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import functools
import logging

from src import owner_flags

logger = logging.getLogger(__name__)

_db_path = None
WRITE_PREFIXES = (
    "submit_",
    "place_",
    "replace_",
    "cancel_",
    "close_",
    "shift_",
    "liquidate_",
)
PAUSE_BLOCKS = frozenset({"submit_order", "replace_entry_limit", "close_position"})
UNKNOWN_BLOCKS = frozenset({"submit_order", "replace_entry_limit"})
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
}


def configure(db_path) -> None:
    """Aim the door at this session's intent record; `release` ends that aim.

    The pointer is process-wide, so its lifetime must be the SESSION that set
    it, not the process. `main.main` pairs this with `release` in its finally
    block: without that pairing the pointer outlives the database it names
    (one session per process in production hides it; a test run does not),
    and every later read escalates to UNKNOWN against a database that is gone.
    """
    global _db_path
    _db_path = db_path


def release() -> None:
    """End the aim set by `configure`; an unaimed door reads no flags."""
    global _db_path
    _db_path = None


#: Called with a reason string whenever the desk acts on an UNKNOWN flag set.
#: Recording only (like the broker's protective_stop_block_recorder); None logs.
unknown_state_recorder = None
unknown_flag_state_calls = 0


def _verdict(name):
    """Return a refusal reason, or None to let the call through."""
    global unknown_flag_state_calls
    flags = owner_flags.read_flags(_db_path)
    if flags.unknown:
        unknown_flag_state_calls += 1
        why = "owner pause flag unreadable and no last known copy"
        logger.error("desk running on an UNKNOWN owner-flag state (%s)", name)
        if unknown_state_recorder is not None:
            try:
                unknown_state_recorder(f"{name}: {why}")
            except Exception as exc:  # noqa: BLE001 - recording never decides
                record_guarded_pass(NO_LEDGER, "owner_flags_gate.unknown_state_recorder", exc)
        return why if name in UNKNOWN_BLOCKS else None
    if flags.paused and name in PAUSE_BLOCKS:
        return "desk is paused by the owner"
    return None


def _wrap(name, orig):
    @functools.wraps(orig)
    def gated(self, *args, **kwargs):
        why = _verdict(name)
        if why is not None:
            logger.warning("owner flag refused %s: %s", name, why)
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
