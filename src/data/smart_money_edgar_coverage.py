"""EDGAR coverage arithmetic for the Form 4 scan, standalone: pure functions of
the stats a scan hands in, with no provider, network or cache behind them.
"""
from __future__ import annotations

import re


ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


# ---------------------------------------------------------------------------
# EDGAR's own denominator — board item 126
# ---------------------------------------------------------------------------

#: The `edgar_coverage_reasons` that mean the scan CANNOT SAY how much of
#: EDGAR's own count it read. Every one of them is a body this code could
#: not read, or a page that stopped short of a count EDGAR itself gave.
#:
#: Deliberately NOT in here: `scan_cap_reached`, `refresh_deadline_exceeded`
#: and `days_not_queried`. Those are the desk's OWN bounded choices — the
#: `max_filings_per_refresh` budget and the refresh deadline — and their
#: residue is already reported through `pending_filings` /
#: `watched_pending_filings` / the per-issuer read-through map. A scan that
#: spent its budget exactly as designed is not a scan that failed, and
#: treating it as one would make the seat read degraded every single day,
#: which is the harm board item 126 names in its own text.
UNVERIFIED_EDGAR_REASONS = frozenset({
    "edgar_total_unreadable",
    "edgar_hits_unreadable",
    "edgar_returned_no_hits_for_nonzero_total",
    "edgar_page_short_of_total",
    "edgar_hits_malformed",
    "edgar_total_changed",
    "edgar_coverage_stale",
    "edgar_rows_unreadable",
    "edgar_enumerated_above_total",
})


def edgar_total(hits_block: object) -> int | None:
    """EDGAR's own filing count from an EFTS ``hits`` block, or None.

    None means "this body did not tell us", which is a different fact from
    zero and the entire reason this function exists: a provider answering
    200 with an empty or garbage body used to be indistinguishable from a
    day on which nobody filed. A negative or non-integer count is also
    None — EDGAR cannot have filed a negative number of forms, so a body
    saying so is a body this code does not understand.

    ``relation`` is read, not ignored. EFTS caps the count it reports and
    then says so: ``{"value": 10000, "relation": "gte"}`` means "at least
    this many", not "this many". Taking it as exact would let the scan page
    to the cap, decide it had read the day through, and report complete
    coverage of a day it had only read the head of. "At least N" is EDGAR
    declining to give a denominator, so it is treated as no denominator.
    """
    if not isinstance(hits_block, dict):
        return None
    raw = hits_block.get("total")
    if isinstance(raw, dict):
        relation = str(raw.get("relation") or "eq").strip().lower()
        if relation not in {"eq", ""}:
            return None
        raw = raw.get("value")
    if isinstance(raw, bool) or raw is None:
        return None
    if not isinstance(raw, (int, float, str)):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def well_formed_hit(hit: object) -> bool:
    """Is this EFTS row shaped like the answer to a ``forms=4`` query?

    Shape only — whether the desk cares about the issuer is a different
    question, answered in `_discover`. This one asks whether the row could
    have come from EDGAR at all.
    """
    if not isinstance(hit, dict):
        return False
    source = hit.get("_source")
    if not isinstance(source, dict):
        return False
    if not ACCESSION_RE.fullmatch(str(source.get("adsh") or "").strip()):
        return False
    # Whitespace-tolerant on purpose: this feeds an EXACT shortfall check,
    # so a stray space in a real EDGAR row must not read as an unusable one.
    return str(source.get("form") or "").strip() in {"4", "4/A"}


def blank_edgar_coverage() -> dict:
    """The record of a scan that answered no question about its coverage.

    Never-recorded is not evidence of a clean fetch, so this reads as
    unverified everywhere it is used — an older cache, a sub-provider that
    has no Form 4 half, a test double, a stats dict from a stubbed
    `_discover`.
    """
    return {
        "known": False, "verified": False, "reasons": ["never_recorded"],
        "edgar_total": 0, "enumerated": 0, "rows_received": 0, "ratio": None,
        "days_queried": 0, "days_in_window": 0, "days_with_total": 0,
        "window_fraction": None,
    }


def edgar_coverage(stats: object) -> dict:
    """How much of EDGAR's own Form 4 count the last scan actually walked.

    Reports RATIOS and NAMED REASONS. It sets no threshold and defines no
    new seat status: the one judgement it makes is ``verified``, which is
    True only when every day slice this scan queried handed back a readable
    count of its own and no page stopped short of one. That is an exact
    condition, not a cut point.

    ``verified`` False does not mean "not enough data"; it means the desk
    cannot tell how much data there was, which is the state that used to be
    reported as a clean, quiet day.

    WHAT IT DOES NOT CLOSE, stated because a record believed to cover more
    than it does is worse than none. ``verified`` is True when the scan can
    account for what it was HANDED. Numerator and denominator both come out
    of the same response, so a source that confidently and consistently
    reports zero filings every day is indistinguishable from a quiet market
    and always will be from inside this process.

    ``window_fraction`` is the other half a reader needs and the reason it
    is reported beside the ratio: the market-wide scan is bounded by
    `max_filings_per_refresh` and by its own deadline, and in production it
    reaches only the first few day slices of a 366-day window. ``ratio``
    speaks ONLY for ``days_queried``. It is reported, never judged — the
    watched names the desk actually trades are covered by the separate
    per-issuer drain, not by this scan.
    """
    blank = blank_edgar_coverage()
    if not isinstance(stats, dict) or "edgar_days_queried" not in stats:
        return blank
    try:
        total = int(stats.get("edgar_total") or 0)
        enumerated = int(stats.get("edgar_enumerated") or 0)
        rows_received = int(stats.get("edgar_rows_received") or 0)
        days_queried = int(stats.get("edgar_days_queried") or 0)
        days_in_window = int(stats.get("edgar_days_in_window") or 0)
        days_with_total = int(stats.get("edgar_days_with_total") or 0)
    except (TypeError, ValueError):
        return blank
    reasons = sorted({
        str(r) for r in (stats.get("edgar_coverage_reasons") or [])
        if str(r).strip()
    })
    if days_queried <= 0:
        # Nothing was asked of EDGAR at all. Recorded as a reason rather
        # than as an empty success.
        reasons = sorted(set(reasons) | {"edgar_never_queried"})
    elif days_with_total < days_queried:
        reasons = sorted(set(reasons) | {"edgar_total_unreadable"})
    if total > 0 and enumerated > total:
        # More distinct readable filings than EDGAR said existed. Contrived
        # rather than observed, but the alternative is a ratio above 1.0
        # rendered with nothing attached to explain it, and a coverage
        # figure that reads better than complete is not a coverage figure.
        reasons = sorted(set(reasons) | {"edgar_enumerated_above_total"})
    verified = (
        days_queried > 0
        and days_with_total == days_queried
        and not (set(reasons) & UNVERIFIED_EDGAR_REASONS)
    )
    return {
        "known": days_queried > 0,
        "verified": verified,
        "reasons": reasons,
        "edgar_total": total,
        # DISTINCT rows the scan could read, not rows it received. The two
        # differ when a page repeats, or carries rows that could not have
        # answered the query — both are reported rather than netted off.
        "enumerated": enumerated,
        "rows_received": rows_received,
        # The ratio board item 126 asked for. None when EDGAR's own count
        # is zero across every day queried, because "0 of 0" is not a
        # fraction and rendering it as 1.0 would claim a completeness
        # nothing measured. `verified` already carries that judgement.
        "ratio": round(enumerated / total, 4) if total > 0 else None,
        "days_queried": days_queried,
        "days_in_window": days_in_window,
        # How much of the lookback window the scan reached at all. Read this
        # BEFORE the ratio: a ratio of 1.0 over two days of a 366-day window
        # is an honest statement about two days and nothing more.
        "window_fraction": (
            round(days_queried / days_in_window, 4) if days_in_window > 0 else None
        ),
        "days_with_total": days_with_total,
    }
