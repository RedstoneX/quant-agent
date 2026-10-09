"""The role-based conviction bar (R7) — owner mandate 2026-09-25.

"Earn the right to ENTER and to STAY": the own-bar block and the
opposition reason, read off the seats' verdicts.

Bodies moved VERBATIM from `src/risk/rules.py` (AST-identical to the
originals; `tests/test_risk_rules_parts_boundary.py` is the witness that this
part builds and runs alone). `src/risk/rules.py` keeps the engine, every
ledger-pinned number and the re-export mirror, so every existing
`from src.risk.rules import X` keeps resolving.
"""

from src.models import AnalystVerdict


# --- Owner mandate 2026-09-25 — the ROLE-BASED conviction bar (R7) ---------
#
# "Earn the right to ENTER and to STAY." A name clears this bar only when a
# SUPPORTIVE seat carries a real, falsifiable reason AND no seat is opposed AND
# the chart is not fighting the trade. It is deliberately SEPARATE from
# `agreement_refuses_trade` (the §9.4 net-evidence floor) and from the
# continuous ranking score in `src/verdicts.py`: those grade and order, this
# one is a role-aware yes/no.
#
# WHY THIS INTRODUCES NO ARBITRARY NUMBER. The whole rule is counts, booleans
# and non-empty checks — "at least one" is existence, not a dial; "no seat
# opposed" is zero, not a chosen floor; the technical veto is a boolean. There
# is nothing here to ledger as `arbitrary` (contrast the discarded HIGH-count
# gate, which would have needed two owner-appetite numbers). The wider change
# carries no number at all: the STAY side culls a held name ONLY when a seat is
# ACTIVELY OPPOSED (owner ruling 2026-09-25), which is zero-versus-count, not a
# dial — there is no confirmation window and no streak.

#: Reasons these functions emit all start with this tag, the SAME string
#: `src.rotation.CONVICTION_BAR_REASON_PREFIX` matches on, so the STAY cull
#: (rotation's `ineligible_hold` tier) recognises exactly these reasons and
#: `holdings_below_entry_bar` counts them. A test pins the two equal.
OWN_BAR_REASON_PREFIX = "R7 conviction bar"


def _has_supported_directional_thesis(v: "AnalystVerdict", aligned: str) -> bool:
    """A NON-technical seat that took a SUPPORTED DIRECTIONAL side.

    MECHANICAL DEFINITION, honest about what the seats actually emit
    (2026-09-25, renamed 2026-09-25 to match what this actually enforces).
    Only Technical carries a machine-readable named invalidation LEVEL (a stop
    price); Earnings carries a genuine analyst-authored falsifier
    (`bear_case`/`bull_case`) or its verdict refuses to build; Macro carries a
    real trigger when the analyst stated one, else a generic fallback; News and
    Smart-money ALWAYS synthesise a templated/constructed invalidation. So there
    is no distinct "has a named falsifier" boolean to read downstream — that
    distinction is lost when `to_verdict()` runs, and News/Smart-money can
    NEVER fail this check on specificity grounds since their invalidation is
    always synthesised. This is NOT a test for a genuinely specific or
    falsifiable thesis — it cannot tell a templated invalidation from an
    analyst-authored one. Requiring a NON-generic invalidation string would
    couple this gate to those exact template sentences, which rot.

    What this actually checks, and all it checks: a supportive seat that took
    a DIRECTIONAL side (not a lukewarm neutral shrug), which
    `AnalystVerdict`'s own validator then FORCES to carry a non-empty
    invalidation condition AND at least one checkable evidence item. Technical
    is excluded — it adds no positive weight, it only gates (below).
    """
    return (
        v.seat != "technical" and v.direction == aligned and bool((v.invalidation or "").strip()) and bool(v.evidence)
    )


def _is_broadcast_macro_verdict(v: "AnalystVerdict") -> bool:
    """True for a MACRO verdict whose direction is the MARKET-WIDE
    `equity_outlook` broadcast, not a name/sector-SPECIFIC stance.

    `MacroAnalysis.to_verdict` (src/models.py) sets a symbol's macro direction
    to its SECTOR's own stance when this read stated one for that sector, and
    falls back to the broad `equity_outlook` otherwise. It marks the difference
    on the verdict itself: a sector-specific direction carries a
    `sector_stance:<sector>` evidence label (added there precisely so a reader
    can see WHY a symbol's macro direction differs from the broad one); the
    broadcast fallback carries no such label.

    A market-wide macro view is NOT a name-specific edge — owner ruling: macro
    alone cannot drive a name decision. So a broadcast-macro verdict must never
    count as per-name OPPOSITION. Without this, one bearish `equity_outlook`
    flip broadcasts "macro opposed" onto every held long that has no bullish
    sector row, culling the whole non-price-protected long book on a single
    review and blocking every new entry in any cautious-macro regime. A
    sector-SPECIFIC bearish macro stance is a genuine name-level opposition and
    still counts.
    """
    if v.seat != "macro":
        return False
    return not any(str(getattr(ev, "label", "") or "").startswith("sector_stance:") for ev in (v.evidence or []))


def own_bar_block_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """The role-based conviction bar (owner mandate 2026-09-25), as one yes/no.

    `None` when the name CLEARS the bar for `direction`, else the one-line
    reason it does not. ONE definition drives ENTRY (`candidate_eligibility`
    R7) and STAYING (the same `blocked` set, via rotation's `ineligible_hold`
    tier). Pure: a function of the seat `AnalystVerdict`s for one name only.

    The bar clears iff ALL of:

      1. TECHNICAL TIMING VETO not triggered. Technical is a timing gate, not a
         yes-vote: it must be PRESENT and CONFIRMING (aligned with the trade).
         A bearish technical read (chart hostile), a neutral one (chart not
         confirming), or NO technical read at all (cannot confirm timing) each
         block ENTRY even when the fundamental thesis is strong — "right
         name, wrong time". Absence is treated as "cannot confirm", the
         CONSERVATIVE choice: a name with no chart read this review does not get
         the benefit of the doubt on timing.
      2. NO seat opposed. A single seat pointing the other way fails the name
         outright — the mandate is "no seat opposed". ONE carve-out
         (`_is_broadcast_macro_verdict`): a MACRO seat whose direction is the
         market-wide `equity_outlook` broadcast (no sector-specific stance) is
         NOT counted as opposition, because a market-wide view is not a
         name-specific edge; a sector-SPECIFIC bearish macro stance still is.
      3. At least one NON-technical seat took a SUPPORTED DIRECTIONAL side
         (see `_has_supported_directional_thesis`) — a real directional call
         backed by evidence and an invalidation, not a bare neutral shrug.
         This is not a genuine specificity/falsifiability test (News and
         Smart-money always synthesise their invalidation); it is "a
         supported directional non-technical seat exists". Technical
         confirming is necessary but NOT sufficient and is never counted
         here — it carries no positive weight.
      4. MACRO IS NOT THE ONLY SUPPORTER (board item 109, owner ruling
         2026-09-25: "nothing can green light a name on its own"). Macro is
         the one seat that holds a view on every name whether or not it
         examined any of them, so it is the one seat that can satisfy (3)
         alone on a name no other seat looked at. Unconditional on weight.

    Supportive/opposed are read from `AnalystVerdict.direction` (a long is
    supported by a bullish verdict, a short by a bearish one), the one
    vocabulary every seat speaks, never the flat registry stance.
    """
    aligned = "bullish" if direction == "bullish" else "bearish"
    opposed = "bearish" if direction == "bullish" else "bullish"

    tech = [v for v in seat_verdicts if v.seat == "technical"]
    if not tech:
        return (
            f"{OWN_BAR_REASON_PREFIX} — no technical read this review; "
            "timing cannot be confirmed (right name, wrong time)"
        )
    if any(v.direction == opposed for v in tech):
        return f"{OWN_BAR_REASON_PREFIX} — technical opposed; chart hostile to the trade (right name, wrong time)"
    if not any(v.direction == aligned for v in tech):
        return (
            f"{OWN_BAR_REASON_PREFIX} — technical does not confirm timing; "
            "chart neutral/broken (right name, wrong time)"
        )

    other_opposed = sorted(
        {
            v.seat
            for v in seat_verdicts
            if v.direction == opposed and v.seat != "technical" and not _is_broadcast_macro_verdict(v)
        }
    )
    if other_opposed:
        return f"{OWN_BAR_REASON_PREFIX} — {', '.join(other_opposed)} opposed (mandate: no seat may be opposed)"

    supporting = sorted({v.seat for v in seat_verdicts if _has_supported_directional_thesis(v, aligned)})
    if not supporting:
        return f"{OWN_BAR_REASON_PREFIX} — no non-technical seat took a supported directional side"
    if supporting == ["macro"]:
        # Board item 109, owner ruling 2026-09-25: "Nothing can green light a
        # name on its own. This is a trading desk with multiple agents."
        # Macro is the one seat that has a view on every name whether or not
        # it looked at any of them, so it is the one seat that can reach this
        # branch alone on a name nobody else examined. Enforced HERE, at the
        # entry bar itself, and unconditionally on the weight: no strength a
        # macro reading can state — broadcast or sector-specific — makes one
        # market view sufficient to admit a name by itself. Technical is
        # already confirming by the time control reaches this line, and
        # technical is a timing veto that carries no positive weight, so
        # "macro only" really does mean nothing else backs the name.
        #
        # **THIS IS NARROWER THAN HIS WORDS, AND THE GAP IS REAL.** Check (3)
        # excludes only technical, so NEWS, EARNINGS or SMART_MONEY can each
        # still be the sole backer — and `_has_supported_directional_thesis`
        # says in its own docstring that News and Smart-money always
        # synthesise their invalidation, so a news-only name clears this bar
        # on a templated falsifier with nothing else having looked at it.
        # Under "nothing green-lights a name on its own" that is the same
        # defect wearing a different seat's badge.
        #
        # It is scoped to macro anyway, and the reason is NOT that the
        # role-based bar is old — it is not. Both rulings landed on
        # 2026-09-25, one day before this code: the role-based bar says ONE
        # supporting seat with a falsifiable thesis and no seat opposed is
        # enough, and the macro ruling says nothing acts alone. Those two
        # same-day rulings are in tension and only the owner can resolve it.
        # What distinguishes macro is not seniority of ruling but that macro
        # is the ONLY seat holding a stance on a name it never examined:
        # every other seat's sole-backer case is a seat that actually looked.
        # Closing the general case means raising the bar to two seats, which
        # would overturn the role-based bar rather than interpret it, so it
        # is left open and stated rather than decided here.
        return (
            f"{OWN_BAR_REASON_PREFIX} — macro is the only seat supporting "
            "this name; one macro view cannot admit a name on its own"
        )

    return None


def own_bar_opposition_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """The OPPOSITION-only subset of the conviction bar — the STAY cull test.

    `None` unless a seat is ACTIVELY OPPOSED to the held `direction`, else the
    one-line reason it is culled. Owner ruling 2026-09-25: a currently-HELD name
    earns its right to STAY, and it is culled ONLY when a seat turns actively
    opposed (technical opposed OR any non-technical seat opposed) — NOT when it
    merely fails the ENTRY bar on SOFT grounds (no technical read this review, a
    neutral/non-confirming technical read, or support that faded to neutral).
    Those soft cases drop a held name from the ranked survivors but never cull
    it; only opposition does. A broadcast-macro verdict (market-wide
    `equity_outlook`, no sector-specific stance) is NOT opposition here either —
    the same `_is_broadcast_macro_verdict` carve-out entry uses — so a single
    macro flip cannot cull the whole non-price-protected long book.

    ENTRY still uses the full-strict `own_bar_block_reason`; this narrower test
    exists solely for the held side. It reuses the SAME aligned/opposed
    vocabulary and the SAME reason strings as the two opposition branches of
    `own_bar_block_reason`, so a held name that IS culled reads identically to
    an entry candidate refused for the same opposition.

    Pure: a function of the seat `AnalystVerdict`s for one name only.
    """
    aligned = "bullish" if direction == "bullish" else "bearish"
    opposed = "bearish" if direction == "bullish" else "bullish"

    tech = [v for v in seat_verdicts if v.seat == "technical"]
    if any(v.direction == opposed for v in tech):
        return f"{OWN_BAR_REASON_PREFIX} — technical opposed; chart hostile to the trade (right name, wrong time)"

    other_opposed = sorted(
        {
            v.seat
            for v in seat_verdicts
            if v.direction == opposed and v.seat != "technical" and not _is_broadcast_macro_verdict(v)
        }
    )
    if other_opposed:
        return f"{OWN_BAR_REASON_PREFIX} — {', '.join(other_opposed)} opposed (mandate: no seat may be opposed)"

    return None
