"""Board item 70 — the structural-protection fallback band's settlement recording.

The fallback in `check_structural_protection` is anchored on the RUNNING
EXTREME since entry when its caller can read one (measured 2026-10-04: 54
blocks removed, NONE added), falling back to entry when it cannot, and every
payload says which anchor it used. Its multiple,
`FALLBACK_PROTECTION_ATR_MULTIPLE`, is `arbitrary` in
`config/number_ledger.yaml`; this recording, written on BOTH outcomes, is the
route to settling it on the desk's own evidence.

HISTORY: this module also wrote the midday reviewer's ENTRY-anchored gate
rows. That gate was removed on 2026-10-09 (owner ruling: no sale is refused
for the price the desk paid), and its payload builder with it.

It holds no constant, makes no decision, and is deliberately free of imports
from `exit_guard` so it can be used without a cycle.
"""

from __future__ import annotations

__all__ = ["fallback_outcome"]

_NO_BASIS = "no thesis_invalid_if and no verified structural level under the stop"


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

    Returns `(protected, basis, detail)`. The band is FLAT (it never scaled
    with hold length), the payload states so, and `anchor_kind` states
    whether the move was
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
        return (
            True,
            "noise_band_intact",
            (
                f"{payload}{_NO_BASIS}; adverse move ({adverse:.4g}) is within "
                f"the {band_multiple}x ATR noise band — protected"
            ),
        )
    return (
        False,
        "noise_band_broken",
        (
            f"{payload}{_NO_BASIS}; adverse move ({adverse:.4g}) exceeds the "
            f"{band_multiple}x ATR noise band — not protected"
        ),
    )
