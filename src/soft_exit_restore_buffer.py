"""Run-scoped parking for mechanical soft-exit heal observations.

ITEM 78 RECORDING, RECORDING ONLY. Lifted out of `src/seat_heal.py`
unchanged in purpose and re-exported from there.

WHY IT IS A BUFFER: the mechanical restore runs inside a Pydantic
`model_validator`, which has no run id, no session and no database
handle. Passing one in would make a validator depend on storage, so the
observation is parked here and written where that context exists.

TWO DEFECTS THIS MODULE FIXES, both measured on production:

1. DUPLICATE GENERATION, not a cap that was too small. One live session
   generated ~20,122 observations of which 15,122 were discarded; 5,136
   of the 5,174 stored rows were the identical `(blank_found=1,
   healed=0)` tuple, because the validator re-runs per name per pass and
   appended an indistinguishable row every time. Observations are now
   keyed on the identity that actually distinguishes them and a repeat
   increments `occurrences` instead of appending. The cap is unchanged:
   it bounds DISTINCT identities, and raising it would have swapped one
   invented number for another. `dropped` still counts identities the
   cap refused, so the evidence that drops happen is not removed.

2. CROSS-RUN LEAK. The buffer used to be a module global drained by one
   late pipeline stage, so a run that exited before that stage left it
   loaded and the next run filed those observations under its own run
   id. The buffer is now opened by `RunContext.start`, the single
   factory every session goes through, and a drain only returns
   observations opened under the run id it asks for. No exit path can
   leak, because nothing has to be REACHED to clean up: the next run's
   birth abandons whatever the last one left, counted, never refiled.

THREE DISTINCTIONS THAT MUST SURVIVE: never-exercised is not zero (no
row means the path never ran); ran-clean, ran-and-failed and
never-reached stay separate; and `occurrences=1` is a recorded single
observation, never the same thing as an unrecorded one.
"""

from __future__ import annotations

#: Bounds DISTINCT observation identities held in memory, not total
#: observations: repeats of an identity already parked cost nothing and
#: are never refused. The number is the memory bound and is deliberately
#: unchanged by the dedupe fix.
_RESTORE_OBSERVATION_CAP = 5000

_restore_run_id: str | None = None
_restore_observations: dict[tuple, dict] = {}
_restore_observations_dropped = 0
_restore_observations_abandoned = 0


def _identity(obs: dict) -> tuple:
    """What actually distinguishes one parked observation from another.

    Everything the row stores apart from the write-time timestamp and the
    occurrence count. Two observations agreeing on all of it carry no
    distinguishing payload, so the second is a repeat and not a new fact.
    """
    sym = obs.get("symbol")
    sym = sym.strip().upper() if isinstance(sym, str) and sym.strip() else None
    blank = obs.get("blank_found")
    healed = obs.get("healed")
    src = obs.get("source")
    return (
        sym,
        None if blank is None else bool(blank),
        None if healed is None else bool(healed),
        src if isinstance(src, str) and src.strip() else None,
    )


def open_restore_run(run_id: str | None) -> int:
    """Start a fresh buffer for `run_id`. Returns identities abandoned.

    Called from `RunContext.start`. Anything the previous run left parked
    belongs to that run and is counted as abandoned rather than filed
    under this one.
    """
    global _restore_run_id, _restore_observations_dropped
    global _restore_observations_abandoned
    abandoned = len(_restore_observations)
    _restore_observations.clear()
    _restore_observations_dropped = 0
    _restore_observations_abandoned = abandoned
    _restore_run_id = run_id
    return abandoned


def restore_run_id() -> str | None:
    """The run the parked observations belong to, or None if unopened."""
    return _restore_run_id


def abandoned_restore_observations() -> int:
    """Identities the last `open_restore_run` found left over, if any."""
    return _restore_observations_abandoned


def _note_restore_observation(obs: dict) -> None:
    """Park one mechanical-restore observation. Never raises."""
    global _restore_observations_dropped
    try:
        key = _identity(obs)
        held = _restore_observations.get(key)
        if held is not None:
            held["occurrences"] = int(held.get("occurrences") or 1) + 1
            return
        if len(_restore_observations) >= _RESTORE_OBSERVATION_CAP:
            _restore_observations_dropped += 1
            return
        parked = dict(obs)
        parked["occurrences"] = 1
        _restore_observations[key] = parked
    except Exception:  # noqa: BLE001 — a recording never blocks a trade
        pass


def drain_restore_observations(run_id: str | None = None) -> tuple[list[dict], int]:
    """Take and clear this run's parked observations plus the dropped count.

    Returns `(observations, dropped_since_last_drain)`. Each observation
    carries an `occurrences` count of at least 1, so a deduplicated row
    loses nothing. The dropped count is reported rather than silently
    lost, so a reader of the rows knows its own denominator is short.

    When `run_id` is given and does not match the run the buffer was
    opened for, nothing is returned: those observations belong to another
    run and filing them here is the leak this guards against.
    """
    global _restore_observations_dropped
    if run_id is not None and _restore_run_id is not None and run_id != _restore_run_id:
        return [], 0
    out = list(_restore_observations.values())
    dropped = _restore_observations_dropped
    _restore_observations.clear()
    _restore_observations_dropped = 0
    return out, dropped
