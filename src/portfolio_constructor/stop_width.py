"""How many ATRs of room a stop gets -- ONE definition, no constructor needed.

Owner ruling 2026-10-04: an unbacked stop is 2.5 ATR. A stock's stop width
comes from that stock's own behaviour (its ATR) only; the setup and
macro-regime scalers were removed 2026-10-09 (mandate: market mood is one
weighted input elsewhere, never a stop-width scaler). The multiple is the
declared base `min_stop_atr_multiple` (recorded in `config/number_ledger.yaml`).
"""

from __future__ import annotations


def stop_atr_multiple(cfg) -> float:
    """The unbacked-stop width in ATRs: the configured base, unscaled."""
    return float(cfg.min_stop_atr_multiple)
