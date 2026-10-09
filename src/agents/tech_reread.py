"""Board item 177: ask the technical seat only about symbols whose inputs moved.

`TechReread.analyze_batch` wraps the untouched
`TechAnalystAgent._analyze_batch_uncached`. See `src/research_throttle.py` for
the input set, the exact-equality comparison and the fail-open rules.

A standalone part, not a mixin: `TechAnalystAgent` inherits nothing from here.
It is handed two collaborators, keyword-only:

* `ask` -- a callable returning the seat's uncached batch call. Read per call,
  never snapshotted, so a spy bound onto the agent INSTANCE after construction
  (`agent._analyze_batch_uncached = spy`) is what this part calls.
* `state` -- the object that carries `last_carried`, `last_unanswered` and
  `last_unreadable` for the pipeline to read after the call. The owner agent
  itself; its identity never changes for the life of the part, so the
  reference is held directly.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.models import TechAnalysisResult

logger = logging.getLogger(__name__)

_PARAMS = (
    "symbols_data",
    "prior_ratings",
    "valuations",
    "prior_macro_regime",
    "prior_macro_outlook",
    "intraday_context",
)


class TechReread:
    def __init__(self, *, ask: Callable[[], Callable], state) -> None:
        self._ask = ask
        self._state = state

    def analyze_batch(
        self,
        symbols_data,
        prior_ratings=None,
        valuations=None,
        prior_macro_regime=None,
        prior_macro_outlook=None,
        intraday_context=None,
    ):
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
        from src.evidence_freshness import READ_CARRIED
        from src.research_throttle import (
            carry_unchanged_tech_reads,
            tech_input_fingerprint,
        )

        state = self._state
        state.last_carried: dict[str, TechAnalysisResult] = {}
        items = list(symbols_data or [])
        prior_ratings = prior_ratings or {}
        fingerprints: dict[str, str | None] = {}
        for item in items:
            sym = item.get("symbol") if isinstance(item, dict) else None
            if sym:
                fingerprints[sym] = tech_input_fingerprint(
                    sym,
                    symbol_data=item,
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
                result = TechAnalysisResult(**{k: v for k, v in dump.items() if k in allowed})
            except Exception as exc:
                # Unreadable prior read -> ask. Never reuse a verdict the desk
                # cannot reconstruct in full.
                logger.info(
                    "tech re-read cache: stored verdict for %s is unreadable (%s) — asking the seat",
                    sym,
                    exc,
                )
                continue
            result.input_fingerprint = fingerprints.get(sym)
            result.read_state = READ_CARRIED
            carried[sym] = result

        to_ask = [i for i in items if not (isinstance(i, dict) and i.get("symbol") in carried)]
        if carried:
            logger.info(
                "tech seat: %d symbol(s) carried forward unchanged (%s); %d asked",
                len(carried),
                ", ".join(sorted(carried)),
                len(to_ask),
            )
        if carried and not to_ask:
            # Nothing moved for anybody: no call is made at all.
            state.last_unreadable = {}
            state.last_unanswered = set()
            state.last_carried = dict(carried)
            return dict(carried), None

        out, agent_result = self._ask()(
            to_ask,
            prior_ratings=prior_ratings or None,
            valuations=valuations,
            prior_macro_regime=prior_macro_regime,
            prior_macro_outlook=prior_macro_outlook,
            intraday_context=intraday_context,
        )
        for sym, analysis in list(out.items()):
            if analysis is not None and analysis.input_fingerprint is None:
                analysis.input_fingerprint = fingerprints.get(sym)
        out.update(carried)
        state.last_carried = dict(carried)
        return out, agent_result


def hold_tech_reread(owner_cls: type) -> type:
    """Class decorator: give `owner_cls` a thin `analyze_batch` shim.

    The part is built per call so an agent made with `__new__` (no
    `__init__`) still answers, and `_analyze_batch_uncached` is read off the
    instance at call time, never captured.
    """

    def analyze_batch(self, *args, **kwargs):
        """Thin shim: body lives in src/agents/tech_reread.py (`TechReread`)."""
        part = TechReread(ask=lambda: self._analyze_batch_uncached, state=self)
        return part.analyze_batch(*args, **kwargs)

    owner_cls.analyze_batch = analyze_batch
    return owner_cls
