"""Escalate when the desk refuses every idea for the SAME reason, session
after session — the shape of a jammed gate, not of a quiet market.

THE GAP THIS CLOSES (docs/WORK.md item 59)
------------------------------------------------------------------------
Every individual empty day is already reported honestly and distinctly:
`no_trades`, `no_orders` and `buys_unfunded` are three separate status
words for three different kinds of nothing (`src/pipeline.py`,
`src/notifier.py`), and after 2026-09-13 every single-day path by which the
desk can produce nothing has an alarm behind it (docs/INCIDENT_HISTORY.md,
"can the desk still die quietly?"). Nothing watched the SEQUENCE. A gate
defect that refused every candidate would tell the truth each morning,
never raise its voice, and stay invisible for as long as it lasted — and
the measured record shows 6 zero-fill sessions out of 11 in the window the
census examined, so a run of empty days is the ORDINARY state here, not an
exotic one.

WHY THIS INTRODUCES NO DAY COUNT
------------------------------------------------------------------------
The tempting alarm — "N identical empty days" — was ruled out on the spot:
there is no honest way to read N off anything. N would be invented, and an
invented number is exactly what this desk refuses to ship
(docs/WORK.md, the no-arbitrary-numbers principle).

A jam and a quiet market are separable by SHAPE instead. A quiet market
kills different candidates for different reasons: this one has no usable
structure, that one's reward:risk is thin, another is too young to
measure. A jammed gate kills EVERY candidate with the SAME reason, and
goes on doing it while the candidates underneath it change. So the trigger
is stated entirely in terms the evidence answers by itself:

    every candidate, in every consecutive no-entry session back to the
    last session that ended any other way, was refused by ONE reason —
    the same one — while the set of candidates was NOT the same set each
    time.

There is no threshold in that sentence. The run of sessions is bounded by
the data (it ends at the last session that did something else). "The
candidate set was not the same set each time" is what forces more than one
session to be involved — you cannot observe "the reason did not vary" from
a single observation, and you cannot observe "the input varied" from
identical inputs. Nothing here is tuned, and nothing was fitted to the
desk's own history.

WHAT COUNTS AS "THE REASON"
------------------------------------------------------------------------
The durable `pipeline_event` rows `src/pipeline_stages.py` writes for every
candidate (read today by `scripts/blocked_proposals_census.py` and by the
evening blocked-proposals digest in `src/pipeline.py`). Each carries
`stage`, `outcome`, `reason`, and — for the two paths that record a refusal
AS DATA rather than recovering it from a log line — a named `refusal` or
`fault` code beside a human detail string.

Two normalisations make those keys comparable ACROSS symbols, and both are
mechanical, not judgemental:

  * the candidate's own ticker is removed from the text (several reasons
    are the constructor's own log line, which opens with the symbol), and
  * every numeric literal is replaced by a placeholder, so "reward:risk
    1.12 below the floor" and "reward:risk 1.31 below the floor" are one
    reason, which they are.

Both can only ever MERGE two reasons that are really the same rule. They
can never split one rule into two — so the failure mode of this normaliser
is a missed alarm, never a false one. Where a reason mentions a SECOND
symbol (sector crowding names the position it collides with), the keys stay
distinct and this check stays quiet. That is a known and deliberate
false-negative, recorded here rather than papered over.

The key is taken from the LAST event recorded for that candidate in that
run, not from an allow-list of "refusal" outcomes. An allow-list rots the
moment a new gate is added; "what killed it last" cannot.

CADENCE, AND WHY IT CANNOT PAGE A PAUSED DESK
------------------------------------------------------------------------
At most one alert per trading day while the condition holds — docs/WORK.md
item 41's existing ruling, already used by `src/coverage_watchdog.py`. No
new cadence is invented here.

And the streak must be CURRENT: the newest session in it has to fall on the
most recent completed trading day, judged by the same
`coverage_watchdog.most_recent_trading_day` the stop watchdog uses. The
trading timers were paused on 2026-09-03. A paused desk runs no sessions,
so its newest session is not on the most recent trading day, so this check
is silent — a deliberately paused desk is not a defect. It re-arms itself
with no flag to flip the moment sessions resume, because that is the same
moment the newest session becomes current again.

WHERE IT RUNS
------------------------------------------------------------------------
From `scripts/alert_heartbeat.py`, beside the coverage watchdog: the one
unit that fires daily whether or not the trading timers are enabled. It is
a READER — one read-only SQLite connection, one owner alert, no writes to
the trading database and no broker orders of any kind.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.coverage_watchdog import most_recent_trading_day
from src.trading_calendar import ET

logger = logging.getLogger(__name__)

#: Same database every session writes its evidence to.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "quant_agent.db"

#: On-box record, gitignored like its siblings under data/alerting/.
STATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "data" / "alerting" / "refusal_signature.json"
)

#: Actions that mean the session actually entered something. A SELL is an
#: exit and does not make a day non-empty for this purpose — the item is
#: about the desk never taking a NEW idea.
ENTRY_ACTIONS = ("BUY", "SHORT")

#: `outcome` values that mean the candidate SURVIVED that stage rather than
#: being killed by it. A session where any candidate's terminal event is one
#: of these did not refuse every idea, whatever the trades table says (the
#: cash-sweep vehicle fills without ever being a BUY/SHORT row — measured on
#: the 2026-09-02 close run, the only real session whose evidence this check
#: can still read). Kept deliberately small and positive: an outcome nobody
#: has thought of yet reads as a refusal, which can only ever LENGTHEN a
#: streak that still has to be monomorphic over changing candidates before
#: anything is sent.
SURVIVED_OUTCOMES = frozenset({
    "submitted", "filled", "allowed", "approved", "placed",
    "buy_submitted", "funded",
})

#: Any numeric literal, including a signed decimal or an exponent.
_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")

_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# the signature of one refusal
# ---------------------------------------------------------------------------

def normalise(text: Any, symbol: str = "") -> str:
    """Strip the candidate's own ticker and every number out of a reason.

    Both substitutions can only merge two texts that describe the same
    rule; neither can split one rule into two. See the module docstring.
    """
    out = str(text or "").strip().lower()
    ticker = str(symbol or "").strip().lower()
    if ticker:
        out = re.sub(rf"\b{re.escape(ticker)}\b", "&", out)
    out = _NUMBER.sub("#", out)
    return _WHITESPACE.sub(" ", out).strip()


def signature_key(payload: dict, symbol: str = "") -> str:
    """The comparable identity of one candidate's terminal outcome.

    `refusal` and `fault` are the two NAMED codes the constructor records
    as data rather than recovering from a log line; they are included
    verbatim (they are codes, not prose) so two different rules never
    collapse into one key just because their prose normalises alike.
    """
    stage = str(payload.get("stage") or "")
    outcome = str(payload.get("outcome") or "")
    code = str(payload.get("refusal") or payload.get("fault") or "")
    return "|".join((
        stage, outcome,
        normalise(payload.get("reason"), symbol),
        code,
        normalise(payload.get("detail"), symbol),
    ))


# ---------------------------------------------------------------------------
# sessions, read out of the evidence stream
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionShape:
    """One run's candidates and how each of them ended."""

    run_id: str
    trading_day: str                      # ET calendar date, YYYY-MM-DD
    last_seen: datetime                   # newest event in the run, UTC
    placed_entry: bool
    keys_by_symbol: dict[str, str] = field(default_factory=dict)
    outcomes_by_symbol: dict[str, str] = field(default_factory=dict)

    @property
    def candidates(self) -> frozenset[str]:
        return frozenset(self.keys_by_symbol)

    @property
    def any_survived(self) -> bool:
        """At least one candidate got through — so "every idea was refused"
        is simply false for this session, whatever the trades table shows."""
        return any(
            outcome in SURVIVED_OUTCOMES
            for outcome in self.outcomes_by_symbol.values()
        )

    @property
    def distinct_keys(self) -> frozenset[str]:
        return frozenset(self.keys_by_symbol.values())

    @property
    def is_monomorphic(self) -> bool:
        """Every candidate this session considered died the same way."""
        return len(self.distinct_keys) == 1


@dataclass(frozen=True)
class RefusalSignatureStatus:
    """`should_alert` is the only field callers act on."""

    trading_day: str | None = None        # the day judged (ET)
    streak: list[SessionShape] = field(default_factory=list)
    key: str | None = None                # the one unvarying reason, if any
    current: bool = False                 # newest streak session is that day
    inputs_varied: bool = False           # the candidate sets were not all equal
    db_error: str | None = None
    already_alerted_for_day: bool = False

    @property
    def is_jammed(self) -> bool:
        """The full trigger, in one place and with no number in it."""
        return bool(self.key) and self.current and self.inputs_varied

    @property
    def should_alert(self) -> bool:
        return self.is_jammed and not self.already_alerted_for_day

    @property
    def symbols(self) -> list[str]:
        seen: set[str] = set()
        for session in self.streak:
            seen |= set(session.candidates)
        return sorted(seen)


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return datetime.now(timezone.utc)


def _parse_iso(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _connect_ro(path: str) -> sqlite3.Connection:
    """Read-only by OS enforcement — this module never writes the trading DB."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def load_sessions(
    db_path: str | Path | None = None,
) -> tuple[list[SessionShape], str | None]:
    """Every run that considered at least one candidate, oldest first.

    A run with no symbol-scoped `pipeline_event` row at all considered
    nothing — an intraday tick that found no mover, an evening review — and
    is absent from this list entirely. It neither joins a streak nor breaks
    one: "the desk considered nothing" is item 11's question, already
    alarmed elsewhere, and is not this check's business.
    """
    path = str(db_path) if db_path is not None else str(DB_PATH)
    try:
        conn = _connect_ro(path)
    except Exception as exc:  # noqa: BLE001
        return [], f"database unreadable: {exc}"
    try:
        rows = conn.execute(
            "SELECT run_id, symbol, timestamp, evidence_json "
            "FROM specialist_evidence "
            "WHERE kind='pipeline_event' AND symbol IS NOT NULL "
            "ORDER BY id",
        ).fetchall()
        entered = {
            str(r["run_id"])
            for r in conn.execute(
                "SELECT DISTINCT run_id FROM trades "
                "WHERE run_id IS NOT NULL AND UPPER(action) IN (?, ?)",
                ENTRY_ACTIONS,
            ).fetchall()
            if r["run_id"]
        }
    except Exception as exc:  # noqa: BLE001
        return [], f"query failed (table missing on a fresh database?): {exc}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    keys: dict[str, dict[str, str]] = {}
    outcomes: dict[str, dict[str, str]] = {}
    newest: dict[str, datetime] = {}
    order: list[str] = []
    for row in rows:
        run_id = str(row["run_id"] or "")
        symbol = str(row["symbol"] or "").strip().upper()
        if not run_id or not symbol:
            continue
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if run_id not in keys:
            keys[run_id] = {}
            outcomes[run_id] = {}
            order.append(run_id)
        # Rows arrive in id order, so the LAST write for a symbol wins —
        # that is the outcome the candidate actually ended on.
        keys[run_id][symbol] = signature_key(payload, symbol)
        outcomes[run_id][symbol] = str(payload.get("outcome") or "")
        when = _parse_iso(row["timestamp"])
        if when is not None and when > newest.get(run_id, datetime.min.replace(tzinfo=timezone.utc)):
            newest[run_id] = when

    sessions: list[SessionShape] = []
    for run_id in order:
        when = newest.get(run_id)
        if when is None:
            continue
        sessions.append(SessionShape(
            run_id=run_id,
            trading_day=when.astimezone(ET).date().isoformat(),
            last_seen=when,
            placed_entry=run_id in entered,
            keys_by_symbol=dict(keys[run_id]),
            outcomes_by_symbol=dict(outcomes[run_id]),
        ))
    sessions.sort(key=lambda s: s.last_seen)
    return sessions, None


def unvarying_streak(sessions: list[SessionShape]) -> list[SessionShape]:
    """The consecutive newest sessions that ALL refused every candidate for
    the same single reason. Empty when the newest session is not of that
    shape — which is the common, healthy case.

    Walks backwards from the newest session and stops at the first one that
    ends any other way: a session that entered something, a session where
    any candidate survived to a positive terminal outcome, a session whose
    candidates died for more than one reason, or a session whose one reason
    is a different reason. No length is imposed; the data ends the walk.
    """
    streak: list[SessionShape] = []
    key: str | None = None
    for session in reversed(sessions):
        if session.placed_entry or session.any_survived or not session.is_monomorphic:
            break
        only = next(iter(session.distinct_keys))
        if key is None:
            key = only
        elif only != key:
            break
        streak.append(session)
    streak.reverse()
    return streak


# ---------------------------------------------------------------------------
# on-box state — one alert per trading day
# ---------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("alerted_for_day", None)
    raw.setdefault("last_result", None)
    raw.setdefault("updated_at", None)
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write, same shape as the sibling watchdogs. Never raises."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def check_refusal_signature(
    *,
    now: datetime | None = None,
    broker: Any = None,
    db_path: str | Path | None = None,
    state_path: Path | None = None,
) -> RefusalSignatureStatus:
    """Read the evidence stream, decide, persist the once-per-day marker.

    Never raises, never writes to the trading database, never touches the
    broker beyond the calendar lookup `most_recent_trading_day` already
    does for the coverage watchdog.
    """
    moment = now or _utc_now()
    state = load_state(state_path)
    day = most_recent_trading_day(moment, broker).isoformat()

    sessions, db_error = load_sessions(db_path)
    streak = unvarying_streak(sessions)
    key = next(iter(streak[-1].distinct_keys)) if streak else None
    # CURRENT: the newest session in the streak is the most recent trading
    # day's. A paused desk fails this and stays silent.
    current = bool(streak) and streak[-1].trading_day == day
    # The input changed and the outcome did not. This is also what forces
    # more than one session into the streak: identical sets cannot vary.
    sets = {s.candidates for s in streak}
    inputs_varied = len(sets) > 1

    status = RefusalSignatureStatus(
        trading_day=day,
        streak=streak,
        key=key,
        current=current,
        inputs_varied=inputs_varied,
        db_error=db_error,
        already_alerted_for_day=(state.get("alerted_for_day") == day),
    )
    if status.should_alert:
        state["alerted_for_day"] = day
    state["last_result"] = {
        "trading_day": day,
        "streak_runs": [s.run_id for s in streak],
        "key": key,
        "current": current,
        "inputs_varied": inputs_varied,
        "db_error": db_error,
    }
    state["updated_at"] = moment.replace(microsecond=0).isoformat()
    save_state(state, state_path)
    return status


def _human_reason(session: SessionShape) -> str:
    """The one reason, as a reader-facing line. Falls back to the key."""
    return next(iter(session.distinct_keys), "")


def alert_text(status: RefusalSignatureStatus) -> str:
    """Severity in the leading word, never colour alone (Rex is red-green
    colour blind; `src/notifier.py` convention)."""
    sessions = status.streak
    lines = []
    for session in sessions:
        lines.append(
            f"  {session.trading_day} ({session.run_id}): "
            f"{len(session.candidates)} candidate(s), all refused — "
            + ", ".join(sorted(session.candidates))
        )
    return (
        "🔴 THE DESK HAS REFUSED EVERY IDEA FOR THE SAME REASON\n"
        f"Across the last {len(sessions)} session(s) with candidates, every "
        "single candidate was refused, and every refusal carried the SAME "
        "reason — while the candidates themselves changed. That is the shape "
        "of a jammed gate, not of a quiet market: a quiet market kills "
        "different names for different reasons.\n\n"
        f"The one reason: {_human_reason(sessions[-1]) if sessions else 'unknown'}\n\n"
        + "\n".join(lines) + "\n\n"
        "Nothing has been changed, placed or cancelled. This is not a count "
        "of empty days — an empty day is normal here and no number of them "
        "would trigger this on its own. What triggered it is that the reason "
        "never varied while the input did.\n"
        "This message repeats at most once per trading day while the "
        "condition holds, and stops by itself the moment one candidate is "
        "refused for a different reason or any entry is placed."
    )


def status_line(status: RefusalSignatureStatus) -> str:
    """One journal line for the heartbeat unit."""
    if status.db_error:
        return f"refusal_signature: could NOT read the evidence ({status.db_error})"
    if not status.streak:
        return (
            "refusal_signature: OK — the most recent session either entered "
            "something or refused its candidates for more than one reason"
        )
    if not status.current:
        newest = status.streak[-1].trading_day
        return (
            f"refusal_signature: an unvarying refusal run ends {newest}, but "
            f"the most recent trading day is {status.trading_day} — the desk "
            "is not running sessions, not alerting"
        )
    if not status.inputs_varied:
        return (
            "refusal_signature: one session refused everything for one "
            "reason; the candidate set has not changed yet, so nothing is "
            "shown to be unvarying"
        )
    if status.already_alerted_for_day:
        return (
            f"refusal_signature: STILL JAMMED over {len(status.streak)} "
            f"session(s); already alerted for {status.trading_day}"
        )
    return (
        f"refusal_signature: JAMMED — {len(status.streak)} consecutive "
        f"session(s) refused every candidate for one unvarying reason"
    )
