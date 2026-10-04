"""Who put this name in front of the desk, named in plain English.

Split out of `src/api/holding_why.py` so the answer to "which seat raised
it" has one home and can be tested without assembling the whole holding
view.

**Why this module exists at all.** Measured against the production
database on 2026-10-04, eight of the eleven open positions rendered
*"which seat raised it: Not recorded"* on the holding page — the only
channel the owner reads. The provenance was never missing. It was
recorded and then not read, in two separate ways:

* Six names (AMD, ETN, META, MRVL, UPS, VLO) were last bought by an
  intraday add, and the view loaded only that add's run. The run that
  OPENED the position — the one carrying the discovery event — was never
  queried. Fixed on the read side in `src/api/holding_entry_evidence.py`.
* Two names (NET, RKLB) carried the discovery event in the very run the
  view already loaded, written as
  `{"stage": "opportunity", "outcome": "discovered",
  "reason": "intraday_move_threshold", "move_pct": 7.95}`, and this
  module simply did not recognise that origin. Fixed here.

A name arriving by the intraday mover scan is a real, stated reason the
desk looked at it, and it is the origin behind most of the desk's recent
entries. Leaving it unnamed told the owner the desk could not say why it
bought, when the desk had written the answer down all along.
"""

from __future__ import annotations

from typing import Any, Callable

#: Which origin wins when a name arrived by more than one route. A Form 4
#: admission is first because it is the route that ADMITTED the symbol —
#: without it the desk would not have been looking at the name at all. A
#: seat nomination is next for the same reason one step down. The
#: intraday mover scan comes next: it is a specific, dated event ("this
#: name moved 7.95% today"), so it distinguishes the name from the rest
#: of the universe. The technical prefilter is last because it is the
#: desk's default way of noticing any name already in the universe, so it
#: distinguishes nothing.
DRIVER_PRECEDENCE = ("smart_money", "nomination", "intraday_move", "technical")

SEAT_LABELS = {
    "technical": "Technical",
    "earnings": "Earnings",
    "smart_money": "Smart money",
    "macro": "Macro",
    "news": "News",
}

#: The `reason` value the intraday opportunity scan writes on its
#: `pipeline_event` row when a name clears the move threshold. Verified
#: against production on 2026-10-04: 340 rows carry it, more than any
#: origin reason except the daily prefilter.
INTRADAY_DISCOVERY_REASON = "intraday_move_threshold"

#: The daily technical prefilter's equivalent.
TECHNICAL_PREFILTER_REASON = "actionable_technical_prefilter"

_SMART_MONEY_ORIGIN = (
    "The smart-money seat put this name in front of the desk: its "
    "SEC Form 4 scan found a large open-market insider purchase and "
    "admitted the symbol for analysis."
)

_TECHNICAL_ORIGIN = (
    "The technical seat picked this name out of the universe it "
    "screens every run."
)


def nominating_seats(rows: list[dict], payload: Callable[[dict], dict]) -> list[str]:
    """Seats that explicitly asked the desk to look at this name."""
    seats = [
        str(payload(row).get("seat") or "")
        for row in rows
        if str(row.get("kind")) == "seat_stance" and payload(row).get("nominated")
    ]
    return [seat for seat in seats if seat]


def _move_percent(value: Any) -> str | None:
    """`3.5393168759310507` -> `"3.5%"`. None for anything unreadable.

    One decimal place, because the scan's threshold is stated in whole
    percent and quoting sixteen digits of it implies a precision the
    reader should not act on.
    """
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct != pct or pct in (float("inf"), float("-inf")):
        return None
    return f"{abs(pct):.1f}%"


def _intraday_origin(rows: list[dict], payload: Callable[[dict], dict]) -> str | None:
    """The intraday mover sentence, with the move in it when recorded."""
    for row in rows:
        if str(row.get("kind")) != "pipeline_event":
            continue
        data = payload(row)
        if data.get("reason") != INTRADAY_DISCOVERY_REASON:
            continue
        moved = _move_percent(data.get("move_pct"))
        if moved:
            return (
                "The intraday scan picked this name up during the session "
                f"because it had already moved {moved} that day, which is "
                "past the threshold at which the desk stops to look."
            )
        return (
            "The intraday scan picked this name up during the session "
            "because it had moved far enough that day for the desk to stop "
            "and look. The size of the move was not recorded."
        )
    return None


def name_the_origin(
    rows: list[dict],
    admission: dict,
    payload: Callable[[dict], dict],
) -> dict[str, Any]:
    """`{origins, driver_key, driver_name, nominating_seats}`.

    `rows` are the `specialist_evidence` rows the holding view loaded,
    `admission` the smart-money admission payload (empty when there was
    none), and `payload` the caller's JSON-decoding helper so this module
    does no decoding of its own.
    """
    seats = nominating_seats(rows, payload)
    origins: dict[str, str] = {}
    if admission:
        origins["smart_money"] = _SMART_MONEY_ORIGIN
    if seats:
        labels = [SEAT_LABELS.get(s, s.replace("_", " ")) for s in seats]
        origins["nomination"] = f"{' and '.join(labels)} asked the desk to look at this name."
    intraday = _intraday_origin(rows, payload)
    if intraday:
        origins["intraday_move"] = intraday
    if any(
        payload(row).get("reason") == TECHNICAL_PREFILTER_REASON
        for row in rows
        if str(row.get("kind")) == "pipeline_event"
    ):
        origins["technical"] = _TECHNICAL_ORIGIN

    driver_key = next((k for k in DRIVER_PRECEDENCE if k in origins), None)
    if driver_key == "smart_money":
        driver_name: str | None = "Smart money"
    elif driver_key == "nomination":
        driver_name = SEAT_LABELS.get(seats[0], seats[0].replace("_", " ").title())
    elif driver_key == "intraday_move":
        driver_name = "Intraday move"
    elif driver_key == "technical":
        driver_name = "Technical"
    else:
        driver_name = None

    return {
        "origins": origins,
        "driver_key": driver_key,
        "driver_name": driver_name,
        "nominating_seats": seats,
    }
