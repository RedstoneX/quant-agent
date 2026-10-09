"""Seat words, data-status and evidence-freshness wording, universe-change descriptions.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.notifier.base import (
    _clip_text,
)
from src.notifier.sections import (
    _new_section,
)


# === Data-quality alert (own message, not bundled) ===
#
# Before this existed, a bad analyst seat (`data_status` anything that
# `evidence_gate.counts_as_degraded` — failed, truncated, parse_error,
# partial, …) only ever showed up as one line INSIDE the routine
# session-result message (see `_append_trade_session_body`'s "degraded:"
# line below). Same-session reuse (`carried_from_morning`) and an
# intentional skip (`not_run_intraday`) are not in that set: they are
# usable, not a lost seat. That is exactly what the owner's alert-design
# rule forbids: "alerts get their OWN
# Telegram message, never bundled into a run summary." A bundled line is
# easy to miss inside a normal-looking "session OK" message, and this
# desk's whole thesis depends on the analysts' data being trustworthy — see
# docs/OUTCOME.md. This fires a SEPARATE, standalone alert through the same
# `send_owner_alert` path already used for a naked position with no stop.
#
# Severity is carried in TEXT, never colour, per the owner's alert-design
# rule — no emoji standing in as the only signal here.
#
# Deliberately NOT deduplicated: if the same seat is still broken next run,
# it alerts again. A repeated alert on a genuinely unresolved problem is
# correct, not noise — silence is what let this go unnoticed before.
#
# `"low_confidence"` (tech, 2026-09-04) is deliberately excluded from this
# page for TECH SPECIFICALLY, even though it is not "ok" — a tech batch
# that resolved every symbol but had the model itself flag one read as
# low-conviction is a real, different thing from a seat that failed or went
# silent, and paging the owner identically for both would train him to
# ignore this alert. It still reaches `_append_trade_session_body`'s
# bundled "degraded:" line below and still counts toward `RiskStage`'s
# ">= 2 degraded sources" `data_degraded` advisory (`src/pipeline_stages.py`,
# class `RiskStage`) — both softer, non-paging responses that fit a
# low-confidence-but-present read better than a standalone alert does.
#
# This exclusion is PER-SEAT, not a bare string match, on purpose: news
# and macro also use the literal value `"low_confidence"` (same day, same
# convention), but for those two seats it means the WHOLE report/analysis
# is low-confidence, not one symbol out of many resolved — a materially
# worse situation than tech's per-symbol case, and one that SHOULD still
# page. A flat `"low_confidence" not in (...)` check would have silently
# suppressed those two seats' real alerts as a side effect of tech's own,
# narrower exception — caught before merge, not after.
_ALERT_EXEMPT_PER_SEAT: dict[str, set[str]] = {
    "tech": {"low_confidence"},
}
#
# 2026-09-04: `data_status["macro"]` gained the same "low_confidence" value
# — a technically clean call (coverage fine, parsed fine) whose own
# self-reported `MacroAnalysis.confidence` came back "low" (see
# pipeline_stages.py's macro branch). Deliberately NOT added to the
# exemption dict above: a critical seat's own stated self-doubt belongs in
# the SAME "do not trust this session blind" alert as a coverage failure,
# not a quieter side channel. Considered and rejected suppressing it to
# avoid alert fatigue: the seat's OTHER known noisy self-check
# (`regime_shift`'s stale-data gate, which fired on ~52% of runs) was driven
# by a `staleness_days<=1` bar that real FRED lag could never reach, so
# "low" was not expected to duplicate it. That day-count gate is GONE as of
# 2026-09-11 — both macro gates now test whether the held reading is the
# latest FRED has published and whether a newer print is overdue, never its
# age (`src/data/macro.py::SeriesFreshness`). An overdue print gets its own
# `data_status["macro"] = "release_overdue"` value and pages through this
# same alert, which is intended: an overdue macro release is a real
# publication or fetch failure, not normal cadence.


# Board item 89 clarity defect — "a 'data degraded' warning that names
# internal components". `data_status` is keyed by the desk's internal seat
# names and valued with internal state tokens. Both maps below say what
# each one MEANS in the words a person would use; the key never reaches
# the message. An unmapped seat or value is DESCRIBED and its raw text is
# labelled as kept-for-the-record, never paraphrased into a claim.
#
# "smart_money" is deliberately NOT a fixed string here. Congressional
# trading disclosures (`src/data/congressional_trading.py`) are gated by
# `config.smart_money.congress_enabled`, switched ON 2026-09-20 per owner
# ruling (see `docs/INCIDENT_HISTORY.md`'s 2026-09-04 and 2026-09-20
# entries). Naming "congressional" in this label when that switch is off
# would tell the owner the desk reads a feed it never actually reads.
# `_smart_money_seat_label` below reads the real switch at call time, so
# the wording can never drift from what the running desk actually does.
_SEAT_WORDS: dict[str, str] = {
    "macro": "the market-backdrop research",
    "tech": "the chart research",
    "news": "the news research",
    "earnings": "the earnings-filing research",
    "sector": "the sector research",
}


def _congress_enabled_now() -> bool:
    """Whether `config.smart_money.congress_enabled` is on right now.

    Read directly from `config/settings.yaml` (the one key, falling back to
    the pydantic field default) instead of being threaded through as a
    parameter: this module renders owner-facing text for dozens of call
    sites (Telegram alerts, the intraday tick, stored-run replays) that do
    not otherwise carry a config object, and several read stored historical
    run data with no config in scope at all. Any failure to read it
    (missing file in a test environment, credential delivery issues, bad
    yaml) conservatively assumes the switch is off, `False`, regardless of
    the field's own live default — a wording helper must never raise or
    break an alert, and must never claim a feed is running when it could
    not actually confirm the setting.
    """
    # Reads only the one key, NOT through `load_config`: that also collects
    # the systemd-delivered broker credentials, which a wording helper has
    # no business touching on every alert it renders.
    try:
        import yaml

        from src.config import SmartMoneyConfig

        settings_path = Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml"
        with open(settings_path) as f:
            raw = yaml.safe_load(f) or {}
        section = raw.get("smart_money") or {}
        if "congress_enabled" in section:
            return bool(section["congress_enabled"])
        return bool(SmartMoneyConfig.model_fields["congress_enabled"].default)
    except Exception:
        return False


def _smart_money_seat_label(congress_enabled: bool) -> str:
    """The smart-money seat's plain name, true to what it actually reads."""
    if congress_enabled:
        return "the insider-and-congressional-trading feed"
    return "the insider-trading feed"


_DATA_STATUS_WORDS: dict[str, str] = {
    "failed": "did not return an answer",
    "partial": "returned only part of an answer",
    "parse_error": "returned an answer the desk could not read",
    "truncated": "was cut off before it finished",
    "empty": "returned nothing",
    "low_confidence": "rated its own answer low-confidence",
    "provider_error": "could not reach its data provider",
    "release_overdue": "is waiting on a scheduled data release that is overdue",
    "symbol_dropped": "dropped at least one symbol from its answer",
    "field_unreadable": (
        "answered, but part of its answer came back in a word the desk "
        "cannot read — that part was dropped so the rest survived, and it "
        "now reads as MISSING rather than being guessed at"
    ),
    "degraded": "returned a degraded answer",
    "market_wide_blind": (
        "read none of the wider market's insider filings this session, while "
        "filings it had not read were still waiting — so it can speak for "
        "the desk's own holdings and for nothing else"
    ),
    # The four remaining CATEGORY_LOST states in src/evidence_gate.py had no
    # plain wording, so an evidence-gate skip naming one of them showed the
    # owner the raw token instead. Each phrase below is read straight off
    # that module's own comment for the state — not a guess at what it might
    # mean.
    "expired": ("had only an out-of-date answer, and this check did not fetch a fresh one"),
    "content_missing": "answered, but the answer had no content in it",
    "carry_forward_empty": (
        "had nothing to carry forward from this morning — the morning never wrote an answer for today"
    ),
    "carry_forward_failed": ("could not be carried forward from this morning — the lookup itself failed"),
}


def seat_words(seat: Any) -> str:
    """Plain words for one research seat's internal name."""
    key = str(seat or "").strip().lower()
    if key == "smart_money":
        return _smart_money_seat_label(_congress_enabled_now())
    return _SEAT_WORDS.get(key) or (f"a research seat the desk has no plain name for (recorded as: {key or 'blank'})")


def describe_data_status(bad: dict) -> list[str]:
    """One plain sentence per degraded seat — "the chart research did not
    return an answer" — from a `{seat: state}` map. A state with no plain
    wording is described as such, with the raw token kept for the record,
    so nothing is ever guessed at on the owner's behalf."""
    lines: list[str] = []
    for seat, state in sorted((bad or {}).items()):
        token = str(state or "").strip().lower()
        words = _DATA_STATUS_WORDS.get(token)
        if words:
            lines.append(f"{seat_words(seat)} {words}")
        else:
            lines.append(
                f"{seat_words(seat)} reported a state the desk has no plain "
                f"wording for (kept for the record: {token or 'blank'})"
            )
    return lines


def _seat_list_words(seats: Any) -> str:
    """ "the chart research and the news research" — never internal keys."""
    words = [seat_words(seat) for seat in (seats or []) if str(seat).strip()]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def describe_evidence_freshness(freshness: Any) -> list[str]:
    """How much of this decision's evidence was read on THIS tick, in words.

    Owner mandate 2026-09-18 made every seat but the chart research
    advisory. That means a decision can now rest on ONE freshly-read seat
    plus a book carried over from the morning, and every one of those
    carried seats reports green. Nothing anywhere said so. This says so.

    It is DISCLOSURE, not a threshold: it states a count, it never judges
    one. No minimum number of fresh seats exists in this desk and none may
    be invented here — that number is the owner's (docs/WORK.md item 20).

    Takes the dict produced by `evidence_freshness.EvidenceFreshness.to_evidence`
    and returns [] for anything it cannot read, so a missing or malformed
    record costs the disclosure line and never the message.
    """
    if not isinstance(freshness, dict):
        return []
    fresh = [s for s in (freshness.get("fresh_seats") or []) if str(s).strip()]
    carried = [s for s in (freshness.get("carried_seats") or []) if str(s).strip()]
    absent = [s for s in (freshness.get("absent_seats") or []) if str(s).strip()]
    unknown = [s for s in (freshness.get("unknown_freshness_seats") or []) if str(s).strip()]
    stale = [s for s in (freshness.get("known_out_of_date_seats") or []) if str(s).strip()]
    total = len(fresh) + len(carried) + len(absent) + len(unknown)
    if not total:
        return []
    lines = [f"<b>HOW FRESH THIS DECISION'S EVIDENCE WAS</b> ({len(fresh)} of {total} research seats read just now)"]
    if fresh:
        lines.append(f"   • read just now: {_seat_list_words(fresh)}")
    else:
        lines.append("   • read just now: none of them")
    if carried:
        lines.append(f"   • carried over from earlier, not re-read: {_seat_list_words(carried)}")
    if stale:
        lines.append(f"   • already known to be out of date: {_seat_list_words(stale)}")
    if absent:
        lines.append(f"   • no answer at all: {_seat_list_words(absent)}")
    if unknown:
        lines.append(f"   • state the desk cannot classify, so not counted as read: {_seat_list_words(unknown)}")
    return lines


def describe_short_handed_decision(freshness: Any) -> list[str]:
    """When the desk WENT AHEAD with a research seat absent, mark it as such.

    Symmetric to `describe_skipped_decision`. Board item 20 REFUSES when a
    blocking seat's answer is lost, and that refusal is marked out loud
    ("DECISION SKIPPED — NOTHING WAS TRADED"). Board item 154 is the
    OTHER half: an advisory seat was unreachable, the desk proceeded and
    decided anyway, and nothing said the decision was made short-handed —
    the absence read exactly like a seat that had nothing to say. This is
    that missing mark.

    Disclosure only, not a threshold: it names the seat(s) that returned no
    answer and states the decision was made without them. It refuses
    nothing and grades nothing — the minimum-seat count is the owner's
    (docs/WORK.md item 20), and none may be invented here.

    Reads the same `evidence_freshness.to_evidence()` record the freshness
    block does, using its `absent_seats` (a seat unreachable this tick, or
    one whose answer never arrived — the case item 154 names). Returns []
    when no seat was absent, so a fully-staffed decision costs nothing, and
    the CALLER is responsible for not rendering it on a refusal, where the
    skip banner already speaks for the missing seats.
    """
    if not isinstance(freshness, dict):
        return []
    absent = [s for s in (freshness.get("absent_seats") or []) if str(s).strip()]
    if not absent:
        return []
    return [
        "<b>DECIDED SHORT-HANDED — a research seat could not be reached</b>",
        f"   • the desk went ahead and decided without {_seat_list_words(absent)}",
        "   • that research returned no answer this tick, so its view is missing from this decision",
    ]


def describe_universe_changes(block: Any) -> list[str]:
    """The owner-facing account of what the universe screen changed.

    Owner design 2026-09-01: "the owner must never discover the universe
    changed by accident" — every addition, flag and removal since the last
    morning message, in plain words. Removals, flags and held names kept
    past a failed check get one line EACH with the reason (they are the ones
    that matter and are few); additions are one line of names, clipped,
    because the first weeks can add hundreds. Silent only when the screen
    is off (no block). With it on and nothing changed, it says so.
    """
    if not isinstance(block, dict):
        return []
    from src.universe_screen import describe_event, plain_reasons

    events = [e for e in (block.get("events") or []) if isinstance(e, dict)]
    admitted = block.get("admitted_count")
    flagged = block.get("flagged_count")
    size = (
        f" — {admitted} screened stock(s) on the list, {flagged} flagged"
        if isinstance(admitted, int) and isinstance(flagged, int)
        else ""
    )
    if not events:
        return [f"\U0001f50e Stock list: no changes since the last morning{size}"]
    out = [f"\U0001f50e Stock list changed: {len(events)} change(s){size}"]
    grouped = {
        "added": "Added {n} (passed every check): ",
        "cleared": "Flag cleared on {n} (passing again): ",
    }
    for action, label in grouped.items():
        names = [str(e.get("symbol", "?")) for e in events if e.get("action") == action]
        if names:
            out.append(
                _clip_text(
                    "\u2022 " + label.format(n=len(names)) + ", ".join(names),
                    600,
                )
            )
    flagged_events = [e for e in events if e.get("action") == "flagged"]
    if flagged_events:
        out.append(
            _clip_text(
                f"\u2022 Flagged {len(flagged_events)} (removed if they fail again "
                "next week): "
                + "; ".join(
                    "{} ({})".format(
                        e.get("symbol", "?"),
                        plain_reasons(e.get("reasons") or []),
                    )
                    for e in flagged_events
                ),
                600,
            )
        )
    for event in events:
        if event.get("action") in ("removed", "removal_deferred_held"):
            out.append(_clip_text(f"\u2022 {describe_event(event)}", 300))
    return out


def _append_universe_changes(lines: list[str], result: dict) -> None:
    if not isinstance(result, dict):
        return
    block = describe_universe_changes(result.get("universe_changes"))
    if block:
        _new_section(lines, *block)
