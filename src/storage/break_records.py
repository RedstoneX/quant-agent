"""Cross-session break-confirmation records.

A STANDALONE store, not a mixin: built from plain collaborators (an open
sqlite connection and the process write-lock that serialises writes to it),
reaching back into nothing. Lifted whole out of `src.storage.db` so the two
cross-day confirmation reads keep living next to each other and cannot
drift apart.
"""

import json
import math
import sqlite3
import threading

from src.storage.locked_write import locked_write


class BreakRecords:
    """The `specialist_evidence` rows carrying cross-session break state."""

    def __init__(self, *, conn: sqlite3.Connection, lock: threading.Lock):
        self.conn = conn
        self._lock = lock

    def _insert_evidence(
        self,
        *,
        run_id: str,
        agent_name: str,
        kind: str,
        scope: str,
        evidence_json: str,
        symbol: str | None = None,
    ) -> int:
        """Persist one evidence row under the retrying process write lock.

        This is the only WRITE path in this store; every `get_*` below takes
        the plain lock and never commits, so read and write stay telling
        apart at a glance."""

        def _do():
            cur = self.conn.execute(
                "INSERT INTO specialist_evidence "
                "(run_id, decision_id, agent_name, kind, scope, symbol, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, None, agent_name, kind, scope, symbol, evidence_json),
            )
            self.conn.commit()
            return cur.lastrowid or 0

        return locked_write(self._lock, _do, label="insert_specialist_evidence")

    HOLDING_PROTECTION_BREAK_KIND = "holding_protection_break"

    def save_holding_protection_break(
        self,
        *,
        run_id: str,
        symbol: str,
        raw_broken: bool,
        bar_date: str,
        close: float | None = None,
        basis: str | None = None,
        detail: str | None = None,
    ) -> int:
        """Record whether the close dated `bar_date` came back broken for
        `symbol`, so a LATER, DIFFERENT bar_date's read can require it to
        still be broken before treating the break as confirmed.

        `close` is that session's closing price, persisted so a later read can
        re-test whether it cleared the break margin IN FORCE under the current
        trend regime (the margin-consistency guard #4 in
        `src.risk.exit_guard`), rather than trusting a stale broken flag."""
        payload: dict = {"raw_broken": bool(raw_broken), "bar_date": str(bar_date)}
        # SETTLEMENT RECORDINGS, 2026-10-01. The structural-protection check
        # builds two machine-readable `rule=` payloads into its `detail`
        # string -- the break-confirmation margin in both ATR multiples and
        # percent of close (board item 70) and the noise-band read (item 109)
        # -- and this call was the ONLY thing that persisted the check at
        # all. It kept three scalars and threw the payload away, so both
        # recordings had produced zero observations in production: measured
        # read-only 2026-10-01, 0 of 13,822 `specialist_evidence` rows carry
        # any `rule=` text. Keeping `basis` and `detail` is what turns those
        # recordings from code that exists into evidence that accrues. Both
        # stay OPTIONAL and stay NULL-equivalent when absent -- nothing is
        # reconstructed, and no exit behaviour is touched by this.
        if basis is not None:
            payload["basis"] = str(basis)
        if detail is not None:
            payload["detail"] = str(detail)
        try:
            if close is not None:
                cf = float(close)
                if math.isfinite(cf):
                    payload["close"] = cf
        except (TypeError, ValueError):
            pass
        return self._insert_evidence(
            run_id=run_id,
            agent_name="risk_manager",
            kind=self.HOLDING_PROTECTION_BREAK_KIND,
            scope="symbol",
            symbol=symbol.upper(),
            evidence_json=json.dumps(payload),
        )

    def get_prior_holding_protection_break(
        self,
        symbols,
        *,
        today_bar_date: str,
        exclude_run_id: str | None = None,
    ) -> dict[str, bool]:
        """The most recent `raw_broken` flag per symbol from a close dated
        STRICTLY BEFORE `today_bar_date` — i.e. the last completed prior
        trading day's read, skipping any rows that share today's own
        bar_date (repeat intraday reads of the same close).

        A symbol absent from the result has no qualifying prior-day read on
        record (first look at this position, a gap, or the last read failed
        to persist) — callers must treat that as `False` (no confirmed-
        eligible break seen yet), never as True, so a missing row can never
        manufacture a confirmed break.
        """
        # Body shared with the target-side twin below (`_prior_break_flags`)
        # so the two cross-day confirmation reads cannot drift apart.
        return self._prior_break_flags(
            symbols,
            kind=self.HOLDING_PROTECTION_BREAK_KIND,
            today_bar_date=today_bar_date,
            exclude_run_id=exclude_run_id,
        )

    def get_recent_holding_protection_breaks(
        self,
        symbol: str,
        *,
        before_bar_date: str,
        exclude_run_id: str | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """The recent per-session holding-protection break records for one
        `symbol`, dated STRICTLY BEFORE `before_bar_date`, most-recent first and
        DEDUPED to one record per `bar_date` (the latest write for that session
        wins). Each record is `{"bar_date", "raw_broken", "close"}` (close may be
        absent on a legacy row).

        The exit guard reconstructs the CONSECUTIVE-confirming-close streak from
        these, so it can enforce adjacency (a gap session resets — #3) and
        margin-consistency (a prior close counts only if it cleared the margin
        now in force — #4). Returning the raw records rather than a precomputed
        count keeps this method free of the trend/margin policy, which lives in
        `src.risk.exit_guard`."""
        sym = str(symbol).strip().upper()
        if not sym:
            return []
        sql = "SELECT evidence_json FROM specialist_evidence WHERE agent_name='risk_manager' AND kind=? AND symbol=?"
        params: list = [self.HOLDING_PROTECTION_BREAK_KIND, sym]
        if exclude_run_id:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT 500"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        out: list[dict] = []
        seen_dates: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(dict(row).get("evidence_json") or "{}")
            except (TypeError, ValueError):
                continue
            bar_date = str(payload.get("bar_date") or "")
            if not bar_date or bar_date >= str(before_bar_date):
                continue
            if bar_date in seen_dates:
                continue
            seen_dates.add(bar_date)
            rec = {"bar_date": bar_date, "raw_broken": bool(payload.get("raw_broken"))}
            if payload.get("close") is not None:
                rec["close"] = payload.get("close")
            out.append(rec)
            if len(out) >= max(1, int(limit)):
                break
        return out

    # --- Gross-exposure de-lever ceiling state (docs/WORK.md item 112) ---
    #
    # One boolean per de-lever session: did the book finish STILL over its
    # ceiling? Persisted so the owner page fires only on the transition INTO
    # that state, not every session a chronically-over book runs the ladder.
    # On `specialist_evidence` like every other cross-session flag here — no
    # new table.
    DELEVER_CEILING_STATE_KIND = "delever_ceiling_state"

    def save_delever_ceiling_state(
        self,
        *,
        run_id: str,
        over_ceiling: bool,
    ) -> int:
        """Record whether this session's gross-exposure de-lever finished with
        the book still over its ceiling.

        Written on EVERY session that runs the ceiling enforcement, for both
        outcomes, so the next session can tell a fresh transition into
        still-over apart from a book that has sat over the ceiling for days.
        Observability/state only — no order, sizing or sequencing reads it."""
        return self._insert_evidence(
            run_id=run_id,
            agent_name="pipeline",
            kind=self.DELEVER_CEILING_STATE_KIND,
            scope="run",
            evidence_json=json.dumps({"over_ceiling": bool(over_ceiling)}),
        )

    def get_last_delever_over_ceiling(
        self,
        *,
        exclude_run_id: str | None = None,
    ) -> bool | None:
        """The most recent recorded de-lever ceiling state — True (still over),
        False (cleared), or None when there is no prior record.

        None and False both mean 'not currently in the still-over state', so a
        transition into still-over pages in either case. `exclude_run_id` drops
        rows from the current run, so a read-before-write in the same session
        sees only PRIOR sessions."""
        sql = "SELECT evidence_json FROM specialist_evidence WHERE kind = ?"
        params: list = [self.DELEVER_CEILING_STATE_KIND]
        if exclude_run_id:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT 1"
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(dict(row).get("evidence_json") or "{}")
        except (TypeError, ValueError):
            return None
        val = payload.get("over_ceiling")
        return val if isinstance(val, bool) else None

    # --- Take-profit revision record (`src.risk.target_revision`) -------
    #
    # Two kinds, both on `specialist_evidence` rather than a new table: it
    # already carries exactly what a revision record needs (run_id, agent,
    # kind, symbol, JSON payload, timestamp) and the cross-day confirmation
    # read above is the same shape.
    #
    # TARGET_LEVEL_BREAK_KIND is the two-consecutive-closes state for
    # "the level THE TARGET was measured against has been closed through",
    # keyed by the close's own `bar_date` for the same reason
    # HOLDING_PROTECTION_BREAK_KIND is — several intraday cycles reading one
    # close must not count as two confirming days. It is deliberately a
    # SEPARATE kind from the holding-protection break: that one asks whether
    # the STOP's support broke (adverse, lifts protection), this one asks
    # whether the TARGET's ceiling broke (favourable, re-derives a number).
    # Sharing a key would let one question confirm the other.
    TARGET_LEVEL_BREAK_KIND = "target_level_break"

    def save_target_level_break(
        self,
        *,
        run_id: str,
        symbol: str,
        raw_broken: bool | None,
        bar_date: str,
        raw_reach: bool | None = None,
        raw_wall: bool | None = None,
    ) -> int:
        """Record the close dated `bar_date`'s RAW trigger state, so a
        LATER, DIFFERENT bar_date's read can require the same condition to
        still hold before treating it as confirmed.

        THREE FLAGS, ONE ROW, ONE MECHANISM (item 194). `raw_broken` is the
        original: the level the target was measured against was closed
        through. `raw_reach` and `raw_wall` are the other two triggers' raw
        state, carried in the SAME row under the same `bar_date` key so all
        three are confirmed by one definition of "the prior trading day
        agreed" rather than three. A flag omitted or passed None is written
        as absent and read back as False — a question that could not be
        asked can never be half of a confirmation.
        """
        payload: dict = {"bar_date": str(bar_date)}
        for key, val in (
            ("raw_broken", raw_broken),
            ("raw_reach", raw_reach),
            ("raw_wall", raw_wall),
        ):
            if val is not None:
                payload[key] = bool(val)
        return self._insert_evidence(
            run_id=run_id,
            agent_name="risk_manager",
            kind=self.TARGET_LEVEL_BREAK_KIND,
            scope="symbol",
            symbol=symbol.upper(),
            evidence_json=json.dumps(payload),
        )

    def get_prior_target_level_break(
        self,
        symbols,
        *,
        today_bar_date: str,
        exclude_run_id: str | None = None,
        flag: str = "raw_broken",
    ) -> dict[str, bool]:
        """The most recent target-revision trigger flag per symbol from a
        close dated STRICTLY BEFORE `today_bar_date`.

        `flag` picks which of the three raw states written by
        `save_target_level_break` to read; the row selection, the
        strictly-earlier bar_date rule and the missing-row rule are
        identical for all three.

        A symbol absent from the result has no qualifying prior-day read, and
        callers must read that as False — a missing row can never manufacture
        a confirmed break, exactly as for the stop-side twin above.
        """
        return self._prior_break_flags(
            symbols,
            kind=self.TARGET_LEVEL_BREAK_KIND,
            today_bar_date=today_bar_date,
            exclude_run_id=exclude_run_id,
            flag=flag,
        )

    def _prior_break_flags(
        self,
        symbols,
        *,
        kind: str,
        today_bar_date: str,
        exclude_run_id: str | None = None,
        flag: str = "raw_broken",
    ) -> dict[str, bool]:
        """Shared body of the prior-close break reads."""
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        sql = (
            "SELECT symbol, evidence_json, timestamp FROM specialist_evidence "
            f"WHERE agent_name='risk_manager' AND kind=? AND symbol IN ({placeholders})"
        )
        params: list = [kind, *wanted]
        if exclude_run_id:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        # KEYED ON THE CLOSE, NOT ON THE LAST ROW WRITTEN.
        #
        # This used to take the most recent row by timestamp. That is not
        # "the prior trading day's close": several intraday cycles can
        # re-read one close, and whichever of them happened to run last
        # then decided the flag for the next day. The confirmation is a
        # statement about a CLOSE, so it is now keyed on `bar_date` — the
        # latest bar date strictly before today's that actually ANSWERED
        # this question — and, among several readings of that same close,
        # the EARLIEST recorded one (lowest id). Earliest, because it is
        # the reading taken closest to the close itself and because it is
        # the one choice that does not depend on how many cycles ran.
        #
        # A row that does not carry `flag` at all is a row that could not
        # answer this question, and is SKIPPED rather than read as False —
        # otherwise a degraded cycle's silence would erase an answer.
        #
        # [MEASURED 2026-10-01, production DB read-only] the stop-side
        # twin holds 6 rows, exactly one per symbol+bar_date, and the
        # target-side kind holds none at all, so no production row set is
        # affected by this change today; it is a correctness fix against
        # the intraday re-read, not a repair of an observed wrong answer.
        sql += " ORDER BY id ASC LIMIT 500"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        best: dict[str, tuple[str, bool]] = {}
        for row in rows:
            row = dict(row)
            sym = row["symbol"]
            try:
                payload = json.loads(row.get("evidence_json") or "{}")
                bar_date = payload.get("bar_date")
            except (TypeError, ValueError):
                continue
            if not bar_date or str(bar_date) >= str(today_bar_date):
                continue
            if flag not in payload:
                continue
            prior = best.get(sym)
            # Rows arrive id-ascending, so the first one seen for a given
            # bar date wins it; a strictly later bar date replaces it.
            if prior is None or str(bar_date) > prior[0]:
                best[sym] = (str(bar_date), bool(payload.get(flag)))
        return {sym: val for sym, (_bd, val) in best.items()}


def build_break_records(
    *,
    conn: sqlite3.Connection,
    lock: threading.Lock,
) -> BreakRecords:
    """Build the store from its collaborators BY VALUE."""
    return BreakRecords(conn=conn, lock=lock)
