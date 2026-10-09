"""How many ATRs of room a stop gets — ONE definition, no constructor needed.

Lifted out of `PortfolioConstructor._stop_atr_multiple` unchanged on
2026-10-02 so the coverage repair can ask the same question without
building a constructor. No new number: with no setup and no regime it
returns the declared base `src.config.RiskConfig.min_stop_atr_multiple`
(recorded in `config/number_ledger.yaml`).
"""

from __future__ import annotations


def stop_atr_multiple(cfg, analysis, regime) -> float:
    """The body of `_stop_atr_multiple`, callable with a config alone.

    Extracted 2026-10-02 (no behaviour change) so the coverage repair can
    ask the SAME question without building a `PortfolioConstructor`. With
    no setup and no regime it returns the declared base,
    `src.config.RiskConfig.min_stop_atr_multiple` (recorded in
    `config/number_ledger.yaml`) — neither scaler applies when the label
    matches no key, which is already this function's documented behaviour.
    """
    multiple = cfg.min_stop_atr_multiple
    setup = (getattr(analysis, "setup_type", None) or "").strip().lower()
    for key, scale in cfg.stop_atr_setup_scale:
        if setup == key:
            multiple *= scale
            break
    tape = (regime or "").strip().lower()
    for key, scale in cfg.stop_atr_regime_scale:
        if tape == key:
            multiple *= scale
            break
    return multiple
