"""Plain-language assembly of "why do we hold this" for one symbol.

**What this exists to kill.** The holding detail view rendered the desk's
stored evidence payloads verbatim — `accessions:
["0001104659-26-100306", ...] · admission reason:
material_sec_form4_purchase · broker: {"eligible":true, ...} · temporary:
true` — machine output pointed at a person. Buried in it was a genuinely
interesting fact (a $720 million open-market purchase by Cascade
Investment / Bill Gates) that the reader had no way to see.

This module is PURE: it takes rows already read from the database and
returns plain-English fields. No I/O, no LLM, no broker. That keeps every
wording rule below unit-testable without a database, and leaves the
read-only API-safety invariant in `src/api/db_reads.py` untouched.

**The shape, and why it is this shape.** Three layers, owner-agreed:

1. `lede` — ONE sentence stating the actual reason with the numbers in
   it. No jargon.
2. `readable` — the decision-relevant detail. The test for membership is
   relevance, not length: a fact belongs here if it would change how the
   owner thinks about the position.
3. `raw_evidence` — everything else, for a "show the raw evidence"
   toggle. Accession numbers, internal flags, broker-eligibility JSON,
   enum values, run identifiers. Available, not in his face.

**The rules it enforces, each one because the raw view broke it:**

* One NAMED primary driver, not a list of everything that voted. The
  precedence is `_DRIVER_PRECEDENCE` below and is derived from what put
  the name in front of the desk, never from which seat sounded most
  confident.
* Every fact assembled ONCE. The same sentence is stored in up to three
  places (the PM's `target.thesis`, the constructor's `proposed_order.
  reasoning`, and the `trades.reasoning` column with bracketed
  constructor annotations appended); the smart-money summary is stored
  in both the seat's `finding` and the PM's `provenance`. Readable lines
  are de-duplicated against each other and against the lede.
* Large dollar figures as a person reads them ("$720 million"), dates as
  a person writes them ("11 September 2026").
* A missing field SAYS it is missing. It is never omitted and never
  zeroed — "not recorded" is information, a silent gap is not.
* The take-profit price is labelled for what it actually is. Nothing in
  the desk executes against it (see `NOTHING_ACTS_ON_TARGET`), so
  showing it as an instruction would be a lie.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

#: Verbatim, because getting this wrong misleads the owner about whether
#: the desk will ever sell at the number on his screen. Established from
#: code and history on 2026-09-18: the automatic take-profit trim was
#: deleted on 2026-09-12 (`docs/INCIDENT_HISTORY.md`, "the automatic
#: take-profit trim is deleted; the trailing stop is the only exit rule");
#: no caller anywhere passes `take_profit_price` to the broker; and
#: "taking profits" / "TARGET_BREACH" are deliberately absent from the
#: list of reasons that can justify an exit (`src/pipeline.py`).
NOTHING_ACTS_ON_TARGET = (
    "Nothing sells at this price. It is a reference the desk recorded at "
    "entry, not an instruction. Since 12 September 2026 the trailing stop "
    "is the only automatic exit, and reaching a profit target is not by "
    "itself an accepted reason to sell. The number is not revisited after "
    "entry."
)

#: Also verbatim. `expected_horizon_sessions` is written only on the entry
#: row (`BUY`/`SHORT`) and never updated afterwards, and no rule anywhere
#: closes a position for running past it.
HORIZON_IS_A_PLAN = (
    "This is the plan the technical analyst pinned when the position was "
    "opened. It is never updated afterwards, and nothing sells the "
    "position for running past it."
)

NOT_RECORDED = "Not recorded."

#: Which origin wins when a name arrived by more than one route. A Form 4
#: admission is first because it is the route that ADMITTED the symbol —
#: without it the desk would not have been looking at the name at all. A
#: seat nomination is next for the same reason one step down. The
#: technical prefilter is last because it is the desk's default way of
#: noticing any name already in the universe, so it distinguishes nothing.
_DRIVER_PRECEDENCE = ("smart_money", "nomination", "technical")

_SEAT_LABELS = {
    "technical": "Technical",
    "earnings": "Earnings",
    "smart_money": "Smart money",
    "macro": "Macro",
    "news": "News",
}

#: Name tokens that must not be title-cased. SEC filer names are stored in
#: block capitals; naive title-casing turns "III" into "Iii" and "LP" into
#: "Lp". Roman numerals, personal suffixes, initials, dotted acronyms, and
#: the entity forms that are conventionally written in capitals.
_KEEP_UPPER = re.compile(
    r"^(?:[IVX]+|JR|SR|LP|LLP|LLC|PLC|AG|NV|SA|SE|[A-Z]\.(?:[A-Z]\.)+\.?|[A-Z])$"
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: Keys of the admission payload that are machine identifiers or internal
#: flags. Never rendered in the readable section; passed through to the
#: raw-evidence bucket unchanged so nothing is lost.
_MACHINE_ADMISSION_KEYS = (
    "accessions", "temporary", "broker", "signal_class",
    "signal_class_reasons", "reason", "avg_dollar_volume_20d_usd",
)


def humanize_dollars(value: Any) -> str | None:
    """`720543738.73` -> `"$720 million"`. None for anything unreadable.

    Deliberately coarse: the reader wants the magnitude, and quoting a
    Form 4 aggregate to the cent implies a precision the desk's own
    rounding of it does not have.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount in (float("inf"), float("-inf")):
        return None
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    for cutoff, unit in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if amount >= cutoff:
            scaled = amount / cutoff
            digits = 0 if scaled >= 100 else 1
            text = f"{scaled:.{digits}f}"
            if "." in text:
                # "1.0" -> "1"; must not touch "480", whose zero is a digit.
                text = text.rstrip("0").rstrip(".")
            return f"{sign}${text} {unit}"
    if amount >= 1000:
        return f"{sign}${amount:,.0f}"
    return f"{sign}${amount:,.2f}"


def humanize_name(raw: str) -> str:
    """Block-capital SEC filer name -> readable name, suffixes preserved."""
    out: list[str] = []
    for token in str(raw).split():
        stripped = token.strip(",")
        trailing = token[len(stripped):]
        upper = stripped.upper()
        if stripped.isupper() and (_KEEP_UPPER.match(upper) or "." in stripped):
            out.append(stripped + trailing)
        else:
            out.append(stripped.capitalize() + trailing)
    return " ".join(out)


def humanize_date(raw: Any) -> str | None:
    """`"2026-09-11"` -> `"11 September 2026"`. None if unparseable."""
    text = str(raw or "").strip()[:10]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year}"


def _date_range_words(first: str | None, last: str | None) -> str | None:
    """One date, or a span, written the way a person writes one."""
    lo, hi = humanize_date(first), humanize_date(last)
    if not lo and not hi:
        return None
    if not lo or not hi or lo == hi:
        return f"on {lo or hi}"
    return f"between {lo} and {hi}"


def _payload(row: dict | None) -> dict:
    if not row:
        return {}
    raw = row.get("evidence_json")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _norm(text: str) -> str:
    """Comparison key for de-duplication: case and punctuation insensitive."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _strip_machine_annotations(text: str) -> str:
    """Drop the bracketed constructor/target notes appended to
    `trades.reasoning`. They restate numbers the view shows as fields and
    read as machine chatter mid-sentence."""
    return re.sub(r"\s*\[[^\]]*\]", "", str(text or "")).strip()


#: `technical=buy`, `smart_money=bullish` — the PM writes its thesis with
#: these seat/stance tokens in it. They are internal enum values and they
#: restate, in machine shorthand, exactly what the provenance list below
#: already says in words.
_SEAT_STANCE = re.compile(
    r"\b(technical|earnings|smart[_ ]money|macro|news)\s*=\s*([a-z_]+)", re.I
)


def _condense_thesis(text: str) -> str:
    """Drop the sentence that only restates which seats agreed.

    The PM's thesis characteristically opens with "Open RSG because
    technical=buy, earnings=bullish, and smart_money=bullish all align,
    while macro is neutral context" — a machine-shorthand restatement of
    the provenance list the readable section renders in full sentences.
    Carrying both is the repetition the owner complained about.

    A sentence is dropped only when it carries TWO OR MORE seat/stance
    tokens, i.e. when it is unambiguously the alignment recap and not a
    substantive point that happens to mention a seat. If that would leave
    nothing, the original is kept — losing the thesis entirely is worse
    than repeating it.
    """
    sentences = re.split(r"(?<=[.!?])\s+", str(text or "").strip())
    kept = [s for s in sentences if len(_SEAT_STANCE.findall(s)) < 2]
    joined = " ".join(kept).strip()
    if not joined:
        joined = str(text or "").strip()
    return _SEAT_STANCE.sub(
        lambda m: f"{m.group(1).replace('_', ' ')} says {m.group(2).replace('_', ' ')}",
        joined,
    )


def _sessions_in_words(sessions: int) -> str:
    """Trading sessions into the unit a person keeps in their head."""
    if sessions <= 1:
        return "about a day"
    if sessions <= 7:
        return f"about {sessions} trading days"
    weeks = sessions / 5
    if weeks < 1.75:
        return "roughly a week and a half"
    if weeks < 2.25:
        return "roughly two weeks"
    if weeks < 2.75:
        return "roughly two and a half weeks"
    if weeks < 5:
        return f"roughly {weeks:.0f} weeks"
    return f"roughly {sessions / 21:.0f} months"


def _insider_summary(admission: dict, finding: dict) -> dict | None:
    """Who bought, how much, when, and at what price — from the Form 4
    observations the smart-money seat stored.

    The per-transaction date and price live ONLY on the `finding` rows'
    `observations`; the `admission` payload carries the aggregate value
    and the accession list but no date and no price. When the finding is
    absent (a run admitted the symbol but the paid synthesis did not
    run), the date and price fields honestly say so rather than being
    back-filled from the admission's `last_price`, which is the market
    price at scan time and NOT what the insider paid.
    """
    owners = [str(o).strip() for o in (admission.get("owners") or []) if str(o).strip()]
    buys = [
        obs for obs in (finding.get("observations") or [])
        if isinstance(obs, dict) and str(obs.get("direction") or "").lower() == "buy"
    ]
    if not owners and not buys:
        return None

    if not owners:
        owners = []
        for obs in buys:
            actor = str(obs.get("actor") or "").strip()
            if actor and actor not in owners:
                owners.append(actor)
    actor_plain = " and ".join(humanize_name(o) for o in owners) or None

    roles: list[str] = []
    for obs in buys:
        for role in obs.get("actor_roles") or []:
            label = str(role).replace("_", " ").strip()
            if label and label not in roles:
                roles.append(label)

    total_shares = 0.0
    weighted = 0.0
    total_value = 0.0
    dates: list[str] = []
    for obs in buys:
        try:
            shares = float(obs.get("shares") or 0)
            price = float(obs.get("price_per_share") or 0)
            value = float(obs.get("transaction_value_usd") or 0)
        except (TypeError, ValueError):
            continue
        total_value += value
        if shares > 0 and price > 0:
            total_shares += shares
            weighted += shares * price
        when = str(obs.get("transaction_date") or "").strip()[:10]
        if when:
            dates.append(when)

    # The admission's aggregate is the desk's own headline figure and is
    # what the owner has already seen; prefer it, and fall back to the sum
    # of the observations when only the finding survived.
    value_usd = admission.get("transaction_value_usd")
    try:
        value_usd = float(value_usd) if value_usd else (total_value or None)
    except (TypeError, ValueError):
        value_usd = total_value or None

    avg_price = round(weighted / total_shares, 2) if total_shares > 0 else None
    first_date = min(dates) if dates else None
    last_date = max(dates) if dates else None

    value_words = humanize_dollars(value_usd)
    when_words = _date_range_words(first_date, last_date)
    role_words = f", a {roles[0]}," if roles else ""

    bits: list[str] = []
    if actor_plain:
        bits.append(f"{actor_plain}{role_words} bought")
    else:
        bits.append("An insider bought")
    bits.append(f"about {value_words}" if value_words else "shares")
    bits.append("of stock in the open market")
    if when_words:
        bits.append(when_words)
    if avg_price is not None:
        bits.append(f"at an average of ${avg_price:,.2f} a share")
    plain = " ".join(bits).strip()
    plain = plain if plain.endswith(".") else plain + "."
    if len(buys) > 1:
        plain += f" That was {len(buys)} separate purchases, all disclosed on SEC Form 4s."
    elif buys:
        plain += " Disclosed on an SEC Form 4."

    missing: list[str] = []
    if avg_price is None:
        missing.append("the price the insider paid")
    if not when_words:
        missing.append("the date the insider bought")

    return {
        "plain": plain,
        "actor": actor_plain,
        "role": roles[0] if roles else None,
        "total_usd": value_usd,
        "total_usd_plain": value_words,
        "purchase_count": len(buys) or None,
        "first_transaction_date": first_date,
        "last_transaction_date": last_date,
        "average_price": avg_price,
        "not_recorded": missing,
    }


def _provenance_lines(target: dict) -> dict[str, str]:
    """`{seat: plain sentence}` from the PM's own provenance list.

    This is the desk's single best plain-English record of what each seat
    contributed, written by the PM at decision time. Reading it here is
    what lets the view avoid re-deriving the same statement from each
    seat's own raw output — the repetition the owner complained about.
    """
    lines: dict[str, str] = {}
    for item in target.get("provenance") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        relationship = str(item.get("relationship") or "").strip()
        if not source or not evidence:
            continue
        if relationship == "context" and str(item.get("observed_stance")) == "neutral":
            # A neutral "this neither helps nor hurts" line is not a reason
            # to hold anything. Carrying it made the view longer and no
            # more informative.
            continue
        lines[source] = evidence
    return lines


def _since_entry(interim: list[dict], review: dict) -> list[str]:
    """What has happened since entry that bears on the thesis.

    Two sources and no more: rows written against this position after the
    opening trade (a trail, a trim, a partial exit — each an action the
    desk actually took), and the most recent position-review snapshot.
    """
    lines: list[str] = []
    for row in interim:
        action = str(row.get("action") or "").replace("_", " ").lower()
        when = humanize_date(str(row.get("timestamp") or "")[:10])
        reason = _strip_machine_annotations(row.get("reasoning") or "")
        if action == "trail stop":
            stop = row.get("stop_loss")
            where = f" to ${float(stop):,.2f}" if stop else ""
            lines.append(f"Stop raised{where}{f' on {when}' if when else ''}.")
        else:
            lines.append(
                f"{action.capitalize()}{f' on {when}' if when else ''}"
                + (f" — {reason}" if reason else ".")
            )
    if review:
        bits: list[str] = []
        r_multiple = review.get("r_multiple")
        if isinstance(r_multiple, (int, float)):
            bits.append(
                f"the position is {r_multiple:+.2f} times the risk originally taken"
            )
        progress = review.get("thesis_progress_pct")
        if isinstance(progress, (int, float)):
            bits.append(f"about {progress:.0f}% of the way to the reference target")
        stop_pct = review.get("distance_to_stop_pct")
        if isinstance(stop_pct, (int, float)):
            bits.append(f"and the stop sits {stop_pct:.1f}% away")
        if bits:
            lines.append("Last review: " + ", ".join(bits) + ".")
    return lines


def build_holding_why(
    entry: dict | None,
    evidence_rows: list[dict] | None = None,
    interim_rows: list[dict] | None = None,
) -> dict:
    """Assemble the plain-language answer to "why do we hold this".

    `entry` is the `trades` row that OPENED the position (`BUY`/`SHORT`);
    `evidence_rows` are that run's `specialist_evidence` rows for the same
    symbol, plus any later `review_metrics` rows; `interim_rows` are the
    trades written against the position after the entry. All may be empty
    — a position opened before the desk recorded any of this, or a symbol
    held with no entry row at all, is the honest fallback path, and every
    field then says so rather than vanishing.
    """
    rows = list(evidence_rows or [])
    entry = entry or {}
    symbol = str(entry.get("symbol") or "").upper()

    by_kind: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("agent_name") or ""), str(row.get("kind") or ""))
        by_kind.setdefault(key, []).append(row)

    def first(agent: str, kind: str) -> dict:
        got = by_kind.get((agent, kind)) or []
        return _payload(got[0]) if got else {}

    admission = first("smart_money_analyst", "admission")
    finding = first("smart_money_analyst", "finding")
    tech = first("tech_analyst", "analysis")
    target = first("portfolio_manager", "target")
    reviews = by_kind.get(("position_reviewer", "review_metrics")) or []
    latest_review = _payload(reviews[-1]) if reviews else {}
    if not symbol:
        symbol = str(
            admission.get("symbol") or tech.get("symbol") or target.get("symbol") or ""
        ).upper()

    nominating_seats = [
        str(_payload(row).get("seat") or "")
        for row in rows
        if str(row.get("kind")) == "seat_stance" and _payload(row).get("nominated")
    ]
    nominating_seats = [s for s in nominating_seats if s]

    # --- the named primary driver -------------------------------------
    origins: dict[str, str] = {}
    if admission:
        origins["smart_money"] = (
            "The smart-money seat put this name in front of the desk: its "
            "SEC Form 4 scan found a large open-market insider purchase and "
            "admitted the symbol for analysis."
        )
    if nominating_seats:
        labels = [_SEAT_LABELS.get(s, s.replace("_", " ")) for s in nominating_seats]
        origins["nomination"] = (
            f"{' and '.join(labels)} asked the desk to look at this name."
        )
    if any(
        _payload(row).get("reason") == "actionable_technical_prefilter"
        for row in rows
        if str(row.get("kind")) == "pipeline_event"
    ):
        origins["technical"] = (
            "The technical seat picked this name out of the universe it "
            "screens every run."
        )

    driver_key = next((k for k in _DRIVER_PRECEDENCE if k in origins), None)
    if driver_key == "smart_money":
        driver_name = "Smart money"
    elif driver_key == "nomination":
        driver_name = _SEAT_LABELS.get(
            nominating_seats[0], nominating_seats[0].replace("_", " ").title()
        )
    elif driver_key == "technical":
        driver_name = "Technical"
    else:
        driver_name = None

    prov = _provenance_lines(target)
    insider = _insider_summary(admission, finding) if admission or finding else None

    driver_detail = None
    if driver_key == "smart_money":
        driver_detail = (insider or {}).get("plain") or prov.get("smart_money")
    elif driver_key == "nomination":
        driver_detail = prov.get(nominating_seats[0])
    elif driver_key == "technical":
        driver_detail = prov.get("technical") or str(tech.get("reasoning") or "").strip() or None

    # --- the thesis ---------------------------------------------------
    why = _condense_thesis(
        _strip_machine_annotations(target.get("thesis") or entry.get("reasoning") or "")
    )

    # --- the lede: one sentence, the real reason, with the numbers ----
    company = None
    broker = admission.get("broker")
    if isinstance(broker, dict):
        company = str(broker.get("name") or "").strip() or None
    subject = company or symbol or "this position"
    side = "Shorted" if str(entry.get("action") or "").upper() == "SHORT" else "Bought"

    if insider and insider.get("total_usd_plain"):
        actor = insider.get("actor") or "an insider"
        when = _date_range_words(
            insider.get("first_transaction_date"), insider.get("last_transaction_date")
        )
        price = insider.get("average_price")
        lede = (
            f"{side} because {actor} bought {insider['total_usd_plain']} of "
            f"{subject}"
        )
        if when:
            lede += f" {when}"
        if price is not None:
            lede += f" at about ${price:,.2f} a share"
        if not lede.endswith("."):
            lede += "."
    elif why:
        lede = f"{side} {subject}: {why}"
    else:
        lede = f"Why {subject} is held: {NOT_RECORDED}"

    # --- supporting reasons, each stated once -------------------------
    seen = {_norm(lede)}
    for text in (why, driver_detail, (insider or {}).get("plain")):
        if text:
            seen.add(_norm(text))
    supporting: list[dict[str, str]] = []
    for seat in ("technical", "earnings", "smart_money", "macro", "news"):
        if seat == driver_key or (driver_key == "nomination" and seat in nominating_seats):
            continue
        text = prov.get(seat)
        if not text or _norm(text) in seen:
            continue
        seen.add(_norm(text))
        supporting.append({"seat": _SEAT_LABELS.get(seat, seat), "reason": text})

    fundamental = prov.get("earnings")
    if fundamental and _norm(fundamental) in {_norm(lede), _norm(why)}:
        fundamental = None
    supporting = [s for s in supporting if s["reason"] != fundamental]

    # --- horizon ------------------------------------------------------
    raw_horizon = entry.get("expected_horizon_sessions")
    try:
        horizon_sessions = int(raw_horizon) if raw_horizon else None
    except (TypeError, ValueError):
        horizon_sessions = None
    if horizon_sessions and horizon_sessions > 0:
        horizon = {
            "sessions": horizon_sessions,
            "plain": (
                f"Planned hold: about {horizon_sessions} trading sessions "
                f"({_sessions_in_words(horizon_sessions)})."
            ),
            "note": HORIZON_IS_A_PLAN,
        }
    else:
        horizon = {
            "sessions": None,
            "plain": f"How long we meant to hold this: {NOT_RECORDED}",
            "note": (
                "No holding period was pinned when this position was opened, "
                "so there is none to show."
            ),
        }

    # --- take-profit --------------------------------------------------
    try:
        tp = float(entry.get("take_profit") or 0) or None
    except (TypeError, ValueError):
        tp = None
    try:
        entry_price = float(entry.get("price") or 0) or None
    except (TypeError, ValueError):
        entry_price = None
    # The target PINNED AT ENTRY. `take_profit` above is the LIVE number,
    # which a confirmed structural event can re-derive
    # (`src.risk.target_revision`); this is the one `thesis_progress_pct`
    # and `pace` are measured against, and the only yardstick a revision
    # can ever be graded on. None on rows that predate the column.
    try:
        entry_target = float(entry.get("initial_take_profit") or 0) or None
    except (TypeError, ValueError):
        entry_target = None
    # Most recent adjudicated revision flag for this symbol, whichever way
    # it went. A refusal is a first-class outcome and is shown as one.
    revision_rows = by_kind.get(("risk_manager", "target_revision")) or []
    revision = _payload(revision_rows[-1]) if revision_rows else {}
    revised = bool(
        tp and entry_target and round(tp, 2) != round(entry_target, 2)
    )
    if tp:
        move = ""
        if entry_price:
            move = (
                f", about {abs(tp - entry_price) / entry_price * 100:.1f}% from "
                "where we bought"
            )
        plain = f"Reference target: ${tp:,.2f}{move}."
        if revised and entry_target:
            plain = (
                f"Reference target: ${tp:,.2f}{move} — re-derived from "
                f"${entry_target:,.2f}, which is still what progress and pace "
                f"are measured against."
            )
        take_profit = {
            "price": tp,
            "plain": plain,
            "acted_on": False,
            "note": NOTHING_ACTS_ON_TARGET,
            "entry_price_target": entry_target,
            "revised": revised,
            "basis": str(revision.get("basis") or "") if revised else "",
            "last_revision_code": str(revision.get("code") or ""),
            "last_revision_detail": str(revision.get("detail") or ""),
        }
    else:
        take_profit = {
            "price": None,
            "plain": f"Take-profit target: {NOT_RECORDED}",
            "acted_on": False,
            "note": NOTHING_ACTS_ON_TARGET,
            "entry_price_target": entry_target,
            "revised": False,
            "basis": "",
            "last_revision_code": str(revision.get("code") or ""),
            "last_revision_detail": str(revision.get("detail") or ""),
        }

    # --- what would prove the thesis wrong ----------------------------
    invalid_if = str(entry.get("thesis_invalid_if") or "").strip() or None
    try:
        stop = float(entry.get("stop_loss") or 0) or None
    except (TypeError, ValueError):
        stop = None
    if invalid_if:
        invalidation_plain = f"We are wrong if: {invalid_if}"
    elif stop:
        invalidation_plain = (
            f"No invalidation condition was recorded. The position is "
            f"protected by a stop at ${stop:,.2f}."
        )
    else:
        invalidation_plain = f"What would prove this wrong: {NOT_RECORDED}"

    missing: list[str] = []
    if not why:
        missing.append("why we opened it")
    if driver_name is None:
        missing.append("which seat raised it")
    if not fundamental:
        missing.append("the fundamental reason")
    if horizon_sessions is None:
        missing.append("the intended holding period")
    if tp is None:
        missing.append("the take-profit target")
    if not invalid_if:
        missing.append("what would prove the thesis wrong")
    missing.extend((insider or {}).get("not_recorded") or [])

    raw_admission = {
        k: admission[k] for k in _MACHINE_ADMISSION_KEYS if k in admission
    }
    return {
        "symbol": symbol,
        "company_name": company,
        "lede": lede,
        "readable": {
            "why": why or None,
            "primary_driver": driver_name,
            "primary_driver_detail": driver_detail,
            "raised_by": origins.get(driver_key) if driver_key else None,
            "insider": insider,
            "supporting": supporting,
            "fundamental_reason": fundamental,
            "invalidation": invalidation_plain,
            "stop_price": stop,
            "horizon": horizon,
            "take_profit": take_profit,
            "since_entry": _since_entry(list(interim_rows or []), latest_review),
        },
        "not_recorded": missing,
        "raw_evidence": {
            "admission": raw_admission,
            "identifiers": {
                k: entry[k]
                for k in ("run_id", "decision_id", "position_id", "broker_order_id",
                          "setup_type", "requested_risk_pct", "allocated_risk_pct",
                          "conviction", "decision_model", "timestamp")
                if entry.get(k) is not None
            },
        },
    }
