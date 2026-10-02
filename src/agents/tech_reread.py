"""Board item 177: ask the technical seat only about symbols whose inputs moved.

`TechRereadMixin.analyze_batch` wraps the untouched
`TechAnalystAgent._analyze_batch_uncached`. See `src/research_throttle.py` for
the input set, the exact-equality comparison and the fail-open rules.
"""

import logging

from src.models import TechAnalysisResult

logger = logging.getLogger(__name__)

_PARAMS = (
    "symbols_data", "prior_ratings", "valuations", "prior_macro_regime",
    "prior_macro_outlook", "intraday_context",
)


class TechRereadMixin:
    def analyze_batch(self, symbols_data, prior_ratings=None, valuations=None,
                      prior_macro_regime=None, prior_macro_outlook=None,
                      intraday_context=None):
        """Ask the seat, except about a symbol whose every input is unchanged.

        The technical seat re-reads each held name once per half-hourly check;
        a symbol is skipped only when a fingerprint over EVERY input the seat
        consumes is byte-identical to the one stored beside the verdict the
        desk already holds. Not a timer, cooldown or sampling rate. Fails open:
        a missing, unreadable or ambiguous prior read, or any input that
        cannot be pinned down exactly, asks the seat. A reused verdict is
        marked `read_state="carried_forward"`. Everything else is exactly
        `_analyze_batch_uncached`.
        """
        from src.evidence_gate import READ_CARRIED
        from src.research_throttle import (
            carry_unchanged_tech_reads,
            tech_input_fingerprint,
        )

        self.last_carried: dict[str, TechAnalysisResult] = {}
        items = list(symbols_data or [])
        prior_ratings = prior_ratings or {}
        fingerprints: dict[str, str | None] = {}
        for item in items:
            sym = item.get("symbol") if isinstance(item, dict) else None
            if sym:
                fingerprints[sym] = tech_input_fingerprint(
                    sym, symbol_data=item,
                    prior_rating=prior_ratings.get(sym),
                    valuation=(valuations or {}).get(sym),
                    intraday=(intraday_context or {}).get(sym),
                    prior_macro_regime=prior_macro_regime,
                    prior_macro_outlook=prior_macro_outlook,
                )
        carried: dict[str, TechAnalysisResult] = {}
        allowed = set(TechAnalysisResult.model_fields)
        for sym, dump in carry_unchanged_tech_reads(fingerprints, prior_ratings).items():
            try:
                result = TechAnalysisResult(
                    **{k: v for k, v in dump.items() if k in allowed}
                )
            except Exception as exc:
                # Unreadable prior read -> ask. Never reuse a verdict the desk
                # cannot reconstruct in full.
                logger.info(
                    "tech re-read cache: stored verdict for %s is unreadable "
                    "(%s) — asking the seat", sym, exc,
                )
                continue
            result.input_fingerprint = fingerprints.get(sym)
            result.read_state = READ_CARRIED
            carried[sym] = result

        to_ask = [
            i for i in items
            if not (isinstance(i, dict) and i.get("symbol") in carried)
        ]
        if carried:
            logger.info(
                "tech seat: %d symbol(s) carried forward unchanged (%s); "
                "%d asked", len(carried), ", ".join(sorted(carried)), len(to_ask),
            )
        if carried and not to_ask:
            # Nothing moved for anybody: no call is made at all.
            self.last_unreadable = {}
            self.last_unanswered = set()
            self.last_carried = dict(carried)
            return dict(carried), None

        out, agent_result = self._analyze_batch_uncached(
            to_ask, prior_ratings=prior_ratings or None, valuations=valuations,
            prior_macro_regime=prior_macro_regime,
            prior_macro_outlook=prior_macro_outlook,
            intraday_context=intraday_context,
        )
        for sym, analysis in list(out.items()):
            if analysis is not None and analysis.input_fingerprint is None:
                analysis.input_fingerprint = fingerprints.get(sym)
        out.update(carried)
        self.last_carried = dict(carried)
        return out, agent_result
