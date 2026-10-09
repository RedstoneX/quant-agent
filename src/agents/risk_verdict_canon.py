"""Shape canonicalisation for the risk seat's verdict, lifted out of
``risk_manager.py`` (which sits at its size ratchet) so its catch-alls can be
made LOUD. Pure functions over the pydantic models: nothing here reads the
desk, so a malformed shape fails closed exactly as before -- it is only now
counted (``NO_LEDGER``: nothing ledger-bearing is ever in scope here).
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from src.models import RiskModification, SymbolRejection
from src.sentinel.guarded import NO_LEDGER, record_guarded_pass

logger = logging.getLogger("src.agents.risk_manager")


def canonical_rejections(rejections) -> list[tuple] | None:
    """Order-insensitive (symbol, reason) rows for the per-symbol
    refusals, built by re-validating through `SymbolRejection` so the
    shorthand coercions (bare string, absent reason) are the schema's own
    and not a second ad-hoc path.

    Returns None — never `==` to anything — when the shape doesn't
    validate, so a malformed side fails closed instead of comparing
    (incorrectly) equal. Mirrors `_canonical_modifications`.
    """
    if rejections is None:
        rejections = []
    if isinstance(rejections, (str, dict)):
        # Same container shorthands `RiskVerdict` itself accepts; route
        # them through the model so both sides canonicalize identically.
        from src.models import _normalize_rejected_symbols_field

        rejections = _normalize_rejected_symbols_field(
            {"rejected_symbols": rejections},
        )["rejected_symbols"]
    if not isinstance(rejections, list):
        return None
    models: list[SymbolRejection] = []
    for r in rejections:
        if not isinstance(r, (dict, str)):
            return None
        try:
            models.append(SymbolRejection.model_validate(r))
        except Exception as exc:  # noqa: BLE001 — any shape failure fails closed
            record_guarded_pass(NO_LEDGER, "risk_manager.canonical_rejections", exc, log=logger)
            return None
    record_guarded_pass(NO_LEDGER, "risk_manager.canonical_rejections", log=logger)
    record_guarded_pass(NO_LEDGER, "risk_manager.canonical_modifications", log=logger)
    return sorted(
        ((r.symbol, r.reason) for r in models),
        key=lambda row: row[0],
    )


def canonical_modifications(mods) -> list[tuple] | None:
    """Full RiskModification decision payload (symbol, field,
    original_value, new_value, reason), order-insensitive. Built by
    re-validating each entry through the `RiskModification` model
    itself, so numeric coercion is the schema's own — not a second
    ad-hoc `float()` path — and `reason` (part of what THIS
    modification decided, unlike the top-level narrative
    `reasoning_chain`/`reasoning`) is preserved rather than dropped.
    Returns None — never `==` to anything — when the shape doesn't
    validate, so a malformed side fails closed instead of comparing
    (incorrectly) equal.
    """
    if mods is None:
        mods = []
    if not isinstance(mods, list):
        return None
    models: list[RiskModification] = []
    for m in mods:
        if not isinstance(m, dict):
            return None
        try:
            models.append(RiskModification(**m))
        except Exception as exc:  # noqa: BLE001 — any shape failure fails closed
            record_guarded_pass(NO_LEDGER, "risk_manager.canonical_modifications", exc, log=logger)
            return None
    return sorted(
        ((m.symbol, m.field, m.original_value, m.new_value, m.reason) for m in models),
        key=lambda row: (row[0], row[1]),
    )


def drop_invalid_modifications(parsed: dict) -> dict:
    """Pre-validate each RiskModification; drop malformed entries with a
    warning naming the symbol (or list index when missing).

    Mutates parsed in place for `modifications`. Non-list shapes
    normalize to []. Mirrors EveningAnalyst._drop_invalid_missed_opportunities
    (PR #73) and the news/position_reviewer/meta_reflector pattern (PR #74).
    """
    raw = parsed.get("modifications")
    if raw is None:
        return parsed
    if not isinstance(raw, list):
        logger.warning(
            "Risk manager: modifications is %s, not list — replacing with []",
            type(raw).__name__,
        )
        parsed["modifications"] = []
        return parsed
    valid: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning(
                "Risk manager: dropping non-dict modifications entry at index %d: %r",
                i,
                item,
            )
            continue
        try:
            RiskModification(**item)
        except ValidationError as e:
            sym = item.get("symbol") or f"<idx {i}>"
            logger.warning(
                "Risk manager: dropping malformed modification for %s: %s",
                sym,
                e,
            )
            continue
        valid.append(item)
    parsed["modifications"] = valid
    return parsed
