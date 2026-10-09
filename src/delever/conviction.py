"""src.delever.conviction -- the weakest-conviction cut order for a ceiling trim.

Bodies moved verbatim from src/pipeline_delever.py (`DeleverMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body (e.g. `_live_delever_price`) are the HOST's shim, handed in,
never a body this part owns, so no recursion guard is needed.
"""

import logging

from src.pipeline_context import RunContext

from src.risk.rules import (
    SECTOR_SIDE_LONG,
    count_aligned_sources,
    count_opposing_sources,
    position_side,
)

from src.verdicts import rank_verdicts

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class DeleverConviction:
    """The weakest-conviction cut order for a ceiling trim; standalone, built from explicit collaborators."""

    def __init__(self) -> None:
        pass

    def _conviction_cut_order(self, ctx: RunContext) -> dict[str, tuple[int, int]]:
        """Weakest-conviction-FIRST sort key per held symbol (item 112).

        WHAT THIS RANKS, AND WHAT IT DELIBERATELY DOES NOT. It ranks SEAT
        CONVICTION ABOUT THE HOLDING. It must not rank ENTRY ELIGIBILITY,
        and an earlier version of this build did: it read
        `PortfolioManagerAgent.last_candidate_ranking`, which is the list of
        survivors AFTER `candidate_eligibility` (R2 neutral rating, R3 not
        BUY-eligible today, R6 constructor refusal) and AFTER the conviction
        bar. Every one of those is a reason not to BUY a name today. None is
        a reason to SELL it first. Worse, the conviction bar's STAY side is
        opposition-only by owner ruling (2026-09-25): a held name that fails
        the ENTRY bar on soft grounds — no technical read, a neutral read,
        support faded — is dropped from the survivors "with no cull reason;
        it earns its right to STAY". Ordering a cut by that list sold exactly
        the names the ruling protects. So this reads the RAW seat verdicts
        (`ctx.seat_verdicts`, everything the seats actually said, before any
        admission gate) and this session's evidence registry.

        Key 1 — the bucket. THREE states, not two, so "four seats argued
        against this" and "nobody looked at this" can never collapse into one
        decision to liquidate (the reason the graded score was retired at all,
        board item 66):

          0 OPPOSED      at least one seat argues AGAINST the side held. The
                         desk has a live objection, so this is the one state
                         that is itself a reason to shed — and it is the exact
                         test the owner's 2026-09-25 STAY ruling uses to cull
                         a holding ("opposition-only").
          1 NO COVERAGE  no seat read this name for this side at all. Cut
                         ahead of a live thesis, because there is none to
                         protect — but NEVER first, because a name the desk
                         could not read is not a name the desk decided
                         against, and refusing to BUY is not a decision to
                         SELL. A bar-fetch outage must not author a
                         liquidation order.
          2 SUPPORTED    at least one seat argues FOR the side held and none
                         against. Cut last.

        Counted with `count_opposing_sources` / `count_aligned_sources`
        against the side the position actually carries, with
        `ctx.evidence_stale_sources` excluded as `ignored_sources` exactly as
        every other caller does — which today means an over-age EARNINGS
        stance and nothing else, that being §9.4's only freshness rule.

        Key 2 — inside a bucket, the desk's own `rank_verdicts` ordering over
        those same raw verdicts, best first, counted only when the ranked
        direction MATCHES the side held (a top-ranked BEARISH read is not
        conviction in a long). Unranked scores 0.

        Sorted ASCENDING, so the cut starts with the names the desk has an
        argument against and reaches its live theses last.

        MISSING TECHNICAL READS ARE MADE VISIBLE, NOT FILLED IN. The
        technical seat's symbol set (`_run_tech`) is today's admitted names
        plus the configured universe; unlike News it is NOT passed the held
        book, and a symbol whose bars fail to fetch is skipped. A held name
        can therefore reach here with no technical verdict through no fault
        of its own. This build does not inject held symbols into the
        technical seat — that widens the paid research scope and is a
        separate, costed decision — it instead gives absence its own bucket
        and LOGS every uncovered holding by name, so the gap is reportable
        instead of silently scoring zero and sorting first.
        """
        registry = getattr(ctx, "evidence_registry", None) or {}
        stale = getattr(ctx, "evidence_stale_sources", None) or {}
        non_corroborating = getattr(ctx, "evidence_non_corroborating_sources", None) or {}
        verdicts = list(getattr(ctx, "seat_verdicts", None) or [])
        strength_of: dict[tuple[str, str], int] = {}
        if verdicts:
            try:
                ranked = rank_verdicts(verdicts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("conviction cut order: ranking failed: %s", exc)
                ranked = []
            total = len(ranked)
            for index, candidate in enumerate(ranked):
                symbol = str(getattr(candidate, "symbol", "") or "").strip().upper()
                direction = str(getattr(candidate, "direction", "") or "").strip().lower()
                if symbol and direction:
                    # Best-first -> biggest number is the strongest conviction.
                    strength_of[(symbol, direction)] = total - index
        order: dict[str, tuple[int, int]] = {}
        uncovered: list[str] = []
        for position in ctx.positions or []:
            symbol = str(getattr(position, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            side = position_side(position)
            wanted = "bullish" if side == SECTOR_SIDE_LONG else "bearish"
            sources = registry.get(symbol) or {}
            ignored = stale.get(symbol)
            opposed = count_opposing_sources(
                symbol,
                sources,
                side,
                ignored_sources=ignored,
            )
            # ONE-SIDED (item 109, owner ruling 2026-09-25 "macro weighted,
            # never solo"): a macro stance BROADCAST onto a name whose sector
            # the macro read never mentioned may not be what saves that name
            # from the cut — that is macro protecting a holding on its own.
            # Its dissent above is untouched, so the removal can only ever
            # move a name EARLIER in the cut, never later.
            aligned = count_aligned_sources(
                symbol,
                sources,
                side,
                ignored_sources=(ignored or frozenset()) | (non_corroborating.get(symbol) or frozenset()),
            )
            if opposed > 0:
                bucket = 0  # OPPOSED — cut first
            elif aligned > 0:
                bucket = 2  # SUPPORTED — cut last
            else:
                bucket = 1  # NO COVERAGE — between the two, never first
                uncovered.append(symbol)
            order[symbol] = (bucket, strength_of.get((symbol, wanted), 0))
        if uncovered:
            logger.warning(
                "Conviction cut order: no seat read %s for the side held — "
                "ranked as UNREAD, never as opposed. A holding the desk could "
                "not read is not a holding it decided against.",
                ", ".join(sorted(uncovered)),
            )
        return order
