"""Board item 70 — the ATR noise band's settlement recording.

ANCHOR, 2026-10-04: the two homes anchor DIFFERENTLY and each payload now says
which anchor it used. The midday reviewer stays anchored on ENTRY (measured:
re-anchoring it ADDS 191 blocks against 51 removed, 129 of them never
releasing within 60 sessions). The structural-protection fallback is anchored
on the RUNNING EXTREME since entry when its caller can read one (measured: 54
blocks removed, NONE added), falling back to entry when it cannot.

`NOISE_BAND_ATR_MULTIPLE` is 1.0 and `arbitrary`: no published work measures
the quantity it bounds (the adverse move at which a move stops
being ordinary daily wobble — the width is unsourced under either
anchor). Both derivation attempts are
spent and recorded in `config/number_ledger.yaml`, so the only remaining
honest route is to accrue the desk's own observations in the constant's own
unit and settle it on evidence.

That recording existed from 2026-09-30 and had produced nothing, and the
reason was structural, not just the desk being off: the payload was written
ONLY where the band BLOCKED an exit, and the band's second home emitted no
payload at all. A sample truncated at exactly the threshold under examination
is the one sample that can never locate that threshold — it could only hold
moves below 1.0 ATR and would have been consistent with any multiple at all.

This module owns the payload text for BOTH homes so the two are written in
one shape and can be read as one distribution. It holds no constant, makes no
decision, and is deliberately free of imports from `exit_guard` so either
side can use it without a cycle.
"""

from __future__ import annotations

__all__ = ["fallback_outcome", "midday_payload"]

_NO_BASIS = (
    "no thesis_invalid_if and no verified structural level under the stop"
)


def midday_payload(
    *,
    close_side: str,
    blocked: bool,
    adverse: float,
    entry: float,
    price: float,
    atr: float,
    band_multiple: float,
    sessions_held: float,
    sessions_measured: bool,
    tail: str,
) -> str:
    """The midday reviewer's row, written on BOTH outcomes.

    This home is ENTRY-anchored and stays that way: re-anchoring it on the
    running extreme was MEASURED on 2026-10-04 over 424 trail-stop exits and
    is a regression (191 blocks added against 51 removed, 129 of the added
    never releasing within 60 sessions). `anchor_kind=entry` is therefore a
    constant on this home's rows, written so the two homes' rows can be told
    apart when the distribution is read back.

    `band_multiple` here already carries the sqrt(sessions-held) widening,
    and `sessions_measured` says whether the hold length was real or was
    silently floored to one session — a band width quoted without that is an
    assertion the record cannot support.
    """
    adverse_atr = adverse / atr if atr > 0 else float("nan")
    return (
        f"rule=atr_noise_band home=midday_reviewer side={close_side} "
        f"blocked={str(bool(blocked)).lower()} "
        f"adverse={adverse:.4f} adverse_atr_multiple={adverse_atr:.4f} "
        f"entry={entry:.4f} "
        f"anchor={entry:.4f} anchor_kind=entry "
        f"price={price:.4f} atr14={atr:.4f} "
        f"band_multiple={band_multiple:.4f} "
        f"band_width={band_multiple * atr:.4f} "
        f"sessions_held={sessions_held:g} "
        f"sessions_measured={str(bool(sessions_measured)).lower()} "
        f"| {tail}"
    )


def fallback_outcome(
    *,
    ent: float,
    cur: float,
    atr_f: float,
    is_short: bool,
    is_noise: bool,
    band_multiple: float,
    anchor: float | None = None,
    anchor_kind: str = "entry",
) -> tuple[bool, str, str]:
    """The `check_structural_protection` fallback's row, on both outcomes.

    Returns `(protected, basis, detail)`. This home differs from the midday
    one in a way nothing recorded until now: it judges the move against a
    FLAT band, because it passes no hold length, while the midday home widens
    with sqrt(sessions held). One named constant therefore produced two
    different widths for the same holding on the same day, and the payload
    now states which was applied, and `anchor_kind` states whether the move was
    measured from the running extreme since entry or fell back to entry.
    """
    anc = ent if anchor is None else anchor
    adverse = (cur - anc) if is_short else (anc - cur)
    payload = (
        f"rule=atr_noise_band home=structural_protection_fallback "
        f"band_multiple={band_multiple:g} "
        f"band_scales_with_hold_length=false "
        f"atr14={atr_f:.4g} band_width={band_multiple * atr_f:.4g} "
        f"adverse={adverse:.4g} adverse_atr_multiple={adverse / atr_f:.4g} "
        f"entry={ent:.4g} anchor={anc:.4g} anchor_kind={anchor_kind} "
        f"price={cur:.4g} "
        f"side={'short' if is_short else 'long'} "
        f"inside_band={str(bool(is_noise)).lower()} | "
    )
    if is_noise:
        return True, "noise_band_intact", (
            f"{payload}{_NO_BASIS}; adverse move ({adverse:.4g}) is within "
            f"the {band_multiple}x ATR noise band — protected"
        )
    return False, "noise_band_broken", (
        f"{payload}{_NO_BASIS}; adverse move ({adverse:.4g}) exceeds the "
        f"{band_multiple}x ATR noise band — not protected"
    )
