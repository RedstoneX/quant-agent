"""The constructor's refusal and data-fault side-channel, lifted out of `__init__`.

Bodies moved verbatim from `PortfolioConstructor`; `owner` is the constructor
instance, which keeps owning the state dicts (`last_data_faults`,
`last_refusals`, `last_parity_standdowns`) so their identity never changes.
"""

from __future__ import annotations

import logging

from src.risk.constants import reward_risk_parity_refuses

# The constructor's own logger, so log capture and filters keep matching.
logger = logging.getLogger("src.portfolio_constructor")


def drain_data_faults(owner) -> dict[str, dict[str, str]]:
    """Return every data fault recorded since the last drain, and clear.

    See `last_data_faults`. Returned as a fresh dict so the caller can
    hold it after this instance moves on to the next session.
    """
    faults = dict(owner.last_data_faults)
    owner.last_data_faults = {}
    return faults


def _note_data_fault(
    owner,
    symbol: str,
    direction: str,
    fault: str,
    detail: str,
) -> None:
    """Record and log one UNMEASURABLE symbol. Never raises.

    The log line deliberately says "skipped" and "UNMEASURABLE", not
    "rejected": the constructor did not judge this trade, it could not
    measure the symbol. `_DropReasonCapture` still picks the line up
    (so the symbol is never silently absent from `last_drop_reasons`),
    and the caller reads `last_data_faults` FIRST to file it under the
    right class.
    """
    try:
        key = str(symbol or "").strip().upper()
        owner.last_data_faults[key] = {
            "fault": str(fault),
            "detail": str(detail),
            "direction": str(direction or ""),
        }
    except Exception:  # noqa: BLE001 — a record side-channel must never raise
        pass
    logger.warning(
        "Constructor: %s %s skipped — UNMEASURABLE, a data fault and not a trade judgement [%s]: %s",
        "SHORT" if str(direction).lower() == "short" else "BUY",
        symbol,
        fault,
        detail,
    )


def drain_refusals(owner) -> dict[str, dict[str, str]]:
    """Return every structured refusal since the last drain, and clear.

    See `last_refusals`. A fresh dict, so the caller can hold it after
    this instance moves on to the next session.
    """
    refusals = dict(owner.last_refusals)
    owner.last_refusals = {}
    return refusals


def _parity_verdict(owner, entry_price, stop_loss, derivation, is_short):
    """The ONE parity test. Returns `(refuse, ratio, standdown)`.

    Called from `_resolve_entry_and_stop` (construction) and from
    `real_reward_risk_preview` (PM eligibility/ranking) so the two
    cannot disagree. Before 2026-10-01's second pass the preview did
    not run it at all, so a name could rank, be proposed, and then die
    silently at construction — the exact divergence
    `real_reward_risk_preview` was built to close.

    THREE STAND-DOWNS, all of them "the numerator is not a number this
    code believes", and all recorded rather than silent:

    * no structural level found at all — refusing on the ATR
      projection would be refusing on an invented number, which
      doctrine bars;
    * the level is PAST the horizon reach, in which case
      `derive_structural_target` has already ruled it unreachable and
      returned a measured-move target instead, while still reporting
      the level in `level_used`. Crediting reward from a price the
      same function just declared unreachable overstates the reward;
    * the reward sits INSIDE the one-session noise floor
      (`target_inside_noise`), i.e. the code has already labelled the
      reward noise. Refusing on a figure labelled noise is refusing on
      a number the desk does not believe.
    """
    level = getattr(derivation, "level_used", None)
    if level is None:
        return (False, None, owner.PARITY_STANDDOWN_NO_LEVEL)
    if getattr(derivation, "basis", "") != "structural_level":
        return (False, None, owner.PARITY_STANDDOWN_LEVEL_PAST_REACH)
    if getattr(derivation, "target_inside_noise", False):
        return (False, None, owner.PARITY_STANDDOWN_REWARD_INSIDE_NOISE)
    refuse, ratio = reward_risk_parity_refuses(
        entry_price,
        stop_loss,
        level,
        is_short=is_short,
        reward_is_measured_level=True,
    )
    return (refuse, ratio, None)


def _note_parity_standdown(owner, symbol, direction, reason, derivation):
    key = str(symbol or "").strip().upper()
    if not key:
        return
    owner.last_parity_standdowns[key] = {
        "reason": reason,
        "direction": direction,
        "basis": getattr(derivation, "basis", ""),
        "level_used": getattr(derivation, "level_used", None),
        "horizon_reach": getattr(derivation, "horizon_reach", None),
    }


def _record_parity_refusal(
    owner,
    symbol,
    direction,
    entry,
    stop,
    level,
    ratio,
    *,
    stage="construction",
):
    """Write ONE parity refusal to the durable table. Never raises.

    The owner ruled parity a trial. A trial whose only record is an
    English sentence in an in-memory dict, drained only for the symbols
    that happen to reach `constructor_dropped`, cannot be judged — so
    every number goes in its OWN column here.
    """
    recorder = owner.refusal_recorder
    if recorder is None:
        return
    recorder.record_parity_refusal(
        symbol,
        direction,
        entry,
        stop,
        level,
        ratio,
        stage=stage,
    )


def _record_subfloor_risk_target(
    owner,
    symbol: str,
    direction: str | None,
    requested_pct: float,
) -> None:
    """Record a positive sub-floor PM risk request. Never raises.

    Board item 223, ruled on the risk route 2026-10-01 on the
    adversary's measurement (NOT an owner ruling): NO deterministic
    refusal.
    Nothing here refuses, resizes or reroutes the target — the caller
    continues with the request untouched. The row exists because the
    floor is an INSTRUCTION to the portfolio manager (the prompt says
    so, and measured compliance is 115 of 115 targets carrying a risk
    allocation), and an instruction with no evidence trail cannot tell
    us whether it is ever broken.
    """
    recorder = owner.refusal_recorder
    if recorder is None:
        return
    recorder.record_subfloor_risk_target(
        symbol,
        direction,
        requested_pct,
        owner.cfg.min_risk_pct,
    )


def _note_refusal(
    owner,
    symbol: str,
    direction: str,
    refusal: str,
    detail: str,
    *,
    only_if_unrecorded: bool = False,
    action: str | None = None,
) -> None:
    """Record and log one NAMED trade refusal. Never raises.

    The log line says "refused" so `_DropReasonCapture` also picks it
    up (the symbol is never absent from `last_drop_reasons`), but the
    durable record is the structured entry — the caller reads
    `last_refusals` FIRST and files the code as data.

    `only_if_unrecorded` is for the BACKSTOP callers (board item 10,
    2026-09-14): a terminal check that fires after a more specific rule
    has already refused the same symbol — `_resolve_entry_and_stop`'s
    side check running on a `None` that `_widen_stop_past_noise` just
    refused by name, say. The specific reason must win, so the backstop
    writes nothing (and logs nothing) when this symbol already carries a
    refusal or a data fault from this call. Without it the generic code
    would overwrite the precise one and the fix would make the record
    worse, not better.
    """
    key = str(symbol or "").strip().upper()
    if only_if_unrecorded and (key in owner.last_refusals or key in owner.last_data_faults):
        return
    try:
        owner.last_refusals[key] = {
            "refusal": str(refusal),
            "detail": str(detail),
            "direction": str(direction or ""),
        }
    except Exception:  # noqa: BLE001 — a record side-channel must never raise
        pass
    logger.warning(
        "Constructor: %s %s refused [%s] — %s",
        action or ("SHORT" if str(direction).lower() == "short" else "BUY"),
        symbol,
        refusal,
        detail,
    )
