"""Coverage-gap banner and the fractional-overnight line.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from src.notifier.markup import (
    _fmt_qty,
)


def _actionable_coverage_gaps(gaps) -> list[dict]:
    """The subset of coverage gaps that a human has to do something about.

    Spec §11.1 hybrid fractional stops. Filters out the two states the hybrid
    design produces on purpose (see `_gap_is_expected_fractional`) so callers
    that decide whether to speak at all — chiefly `intra_check`'s silence
    policy — key off real faults rather than off the daily heartbeat of a
    sub-share DAY stop lapsing and being re-placed.
    """
    if not isinstance(gaps, list):
        return []
    return [g for g in gaps if isinstance(g, dict) and not _gap_is_expected_fractional(g)]


def _gap_is_uncovered(gap: dict) -> bool:
    """Spec §11.1 guard 3 — is this coverage gap "NO STOP AT ALL"?

    `_reconcile_stop_coverage` stamps `coverage` ('none' | 'partial') and
    that is the authority when present. Derived from `covered_qty` when it
    is absent, so a gap dict from an older run snapshot (or any caller that
    predates the field) is still classified rather than silently demoted to
    the milder banner.
    """
    coverage = gap.get("coverage")
    if coverage:
        return str(coverage).strip().lower() == "none"
    try:
        return float(gap.get("covered_qty") or 0) <= 0
    except (TypeError, ValueError):
        return False


def _append_coverage_gap_banner(lines: list[str], result: dict) -> None:
    """Render the broker-truth stop-coverage gap banner (🛑) when the
    reconciler found held positions with less open protective-stop coverage
    than held qty — a (partially) naked position the WAL queue didn't know
    about. This is operator-actionable: a stop needs manual re-protection.

    Spec §11.1 guard 3: NO STOP AT ALL and STOP PRESENT BUT MIS-SIZED are
    rendered as two separate banners, never merged into one count. They are
    different conditions with different urgency — a position stopped at the
    wrong size still has a broker order standing watch over most of it; a
    position with no stop has nothing. A single "N under-protected" line
    made the worse of the two invisible inside the milder one.
    """
    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list) or not gaps:
        return

    def _describe(rows: list[dict]) -> str:
        # Board item 89 clarity: "NVDA(4/10)" was a fraction with no words
        # around it. Same two numbers, said as what they are.
        return "; ".join(
            f"{g.get('symbol', '?')} holding "
            f"{_fmt_qty(g.get('held_qty', 0) or 0)}, stop covers "
            f"{_fmt_qty(g.get('covered_qty', 0) or 0)}"
            for g in rows[:6]
        )

    rows = [g for g in gaps if isinstance(g, dict)]
    # Spec §11.1 hybrid fractional stops. These two classes are NOT faults
    # and must never be counted into either red banner: 'fractional_overnight'
    # is a sub-share DAY stop that lapsed at the close exactly as the design
    # intends, and 'fractional_replaced' is one the session's own sweep has
    # already put back. Both happen to every fractional position every day.
    # Rendering them as the top-severity banner would put a 🛑🛑🛑 on this
    # alert on every single run, which is how the owner learns to stop
    # reading the banner that matters.
    expected = [g for g in rows if _gap_is_expected_fractional(g)]
    # Board item 172. A position whose stops could not be READ is neither a
    # measured gap nor a covered position, and it must not be swept into the
    # mis-sized banner, which asserts that a stop IS standing watch over
    # most of the position — a claim nobody established here.
    unreadable = [g for g in rows if _gap_is_unreadable(g)]
    faults = [g for g in rows if not _gap_is_expected_fractional(g) and not _gap_is_unreadable(g)]
    uncovered = [g for g in faults if _gap_is_uncovered(g)]
    partial = [g for g in faults if not _gap_is_uncovered(g)]
    if unreadable:
        # Below NO STOP AT ALL, above MIS-SIZED. It cannot be the top tier:
        # the triple mark means unbounded loss confirmed, and this is a
        # question, not a confirmation. It cannot be the warning tier
        # either: with per-position stops the desk's only loss protection,
        # an unanswerable question about one may BE the top tier and
        # nothing here can rule that out.
        lines.append(
            f"🛑🛑 STOP UNREADABLE: {len(unreadable)} position(s) whose "
            "protective stops the broker could not be asked about — "
            "coverage UNKNOWN, not confirmed either way — "
            + "; ".join(f"{g.get('symbol', '?')} holding {_fmt_qty(g.get('held_qty', 0) or 0)}" for g in unreadable[:6])
        )
    if uncovered:
        # Top severity tier (item 21b): a held position with ZERO stop
        # coverage is unbounded loss, not just a degraded state — the one
        # class of alert on this desk that gets the triple mark.
        lines.append(
            f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing protecting them — {_describe(uncovered)}"
        )
    if partial:
        # Still under-protected but a stop IS standing watch over most of
        # the position — warning tier, not the top one.
        lines.append(f"⚠️ STOP MIS-SIZED: {len(partial)} position(s) only partly protected — {_describe(partial)}")
    _append_fractional_overnight_line(lines, expected)


def _gap_is_unreadable(gap: dict) -> bool:
    """Board item 172 — is this row an unanswered question rather than a
    measured shortfall?

    Keyed on the `coverage` stamp alone and never derived from
    `covered_qty`, unlike `_gap_is_uncovered`: an unreadable row carries
    `covered_qty=None` precisely because no quantity was established, and
    deriving a classification from the absence of a number is how it would
    end up in the wrong banner.
    """
    return str(gap.get("coverage", "")).strip().lower() == "unreadable"


def _gap_is_expected_fractional(gap: dict) -> bool:
    """Is this gap the hybrid design working rather than failing?

    True for the two states spec §11.1's hybrid fractional stops produce on
    purpose: a sub-share DAY stop that lapsed overnight, and one the sweep
    re-placed this session. Neither is operator-actionable.
    """
    return str(gap.get("coverage", "")).strip().lower() in (
        "fractional_overnight",
        "fractional_replaced",
    )


def _append_fractional_overnight_line(lines: list[str], expected: list[dict]) -> None:
    """Spec §11.1 hybrid fractional stops — make the accepted exposure VISIBLE.

    The owner accepted a bounded overnight exposure on the sub-share
    remainder of a fractional position, because being locked out of expensive
    names on a ~$10k account is itself a cost. He accepted it on the explicit
    condition that it be observable: "a number he can look at beats a
    guarantee he has to trust."

    So this line reports the DOLLARS actually unprotected right now, not a
    reassurance that the design bounds them. It is deliberately not a 🛑 —
    nothing here needs doing — and it is deliberately not silent either.
    Uses 🌙 rather than a colour: the state is 'overnight', and hue carries
    no meaning on this channel.

    Silent when the remainder has already been re-placed for the session
    (nothing is exposed) — only a live, currently-unprotected remainder
    prints.
    """
    live = [g for g in expected if str(g.get("coverage", "")).strip().lower() == "fractional_overnight"]
    if not live:
        return
    total = 0.0
    for gap in live:
        try:
            total += float(gap.get("unprotected_value") or 0)
        except (TypeError, ValueError):
            continue
    detail = ", ".join(f"{g.get('symbol', '?')} {_fmt_qty(g.get('uncovered_qty', 0) or 0)}sh" for g in live[:6])
    lines.append(
        f"🌙 overnight fractional remainder unprotected (by design): "
        f"${total:,.2f} across {len(live)} position(s) — {detail}. "
        f"Whole-share part still covered by its GTC stop; the DAY stop is "
        f"re-placed at the next open."
    )
