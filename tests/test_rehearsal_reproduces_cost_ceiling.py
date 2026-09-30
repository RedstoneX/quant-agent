"""Acceptance test: the rehearsal harness reproduces, offline and for free,
the failure the desk suffered on the morning of 2026-08-28 — the spending
circuit refusing the Portfolio Manager's call, so nothing was proposed.

WHAT HAPPENED THAT MORNING
--------------------------
Run `run-be9f8f06` (still in production's `agent_logs`) recorded four analyst
calls and no Portfolio Manager call at all. Its one `llm_circuit_events` row
says why, verbatim:

    quota_held / projected_session_cost_limit / portfolio_manager
    "next portfolio_manager call would project session cost to $1.9118,
     above reserved-exposure ceiling $1.80"

The four analyst calls had actually settled at $0.0460784 between them. The
circuit stopped the desk on a projection forty times the real spend.

WHY THIS TEST HAD TO BE REWRITTEN (docs/WORK.md item 28)
--------------------------------------------------------
Item 14 (2026-09-02) deleted the entire per-call reservation layer, and with
it every projection-based trigger — `projected_session_cost_limit` and its two
siblings no longer exist anywhere in `src/cost_circuit.py`. The version of
this test that survived that rewrite asserted only that those three dead codes
did NOT appear, which is true of any run of any code and guards nothing. It
then failed on something else entirely: it demanded that tech_analyst never
run out of recorded chunk responses, which a rehearsal against a snapshot
taken TODAY cannot honour — today's watchlist needs more chunks than
2026-08-28's recording contains, and `ops/rehearsal/runner.py` says outright
that a rehearsal is "a fresh session against a snapshot of production's state,
not a re-enactment of a past one". So it was red on main continuously, for a
reason unrelated to spending.

WHAT REPRODUCES THE INCIDENT TODAY
-----------------------------------
The same failure — the ceiling refusing the Portfolio Manager before it can
spend — is still reachable, through the mechanism that replaced the
projections: `session_cost_limit`, checked against REAL SETTLED spend
(`_enforce_settled_limits_locked`). This test drives it in two runs against
byte-identical inputs:

  PHASE 1, at production's own configured ceiling: the session gets all the
  way to the Portfolio Manager and asks it a question. Measure what had
  actually settled by that point. (This half is also the regression guard for
  the chunk un-merge fix: before it, replay ran dry on tech_analyst's second
  chunk and the session died long before the Portfolio Manager.)

  PHASE 2, with the ceiling set to that measured figure and nothing else
  changed: the settled-cost circuit must trip on the decision seat, the run
  must settle no more than the ceiling, and the refusal must end the session
  cleanly rather than as an unhandled fault.

No invented number anywhere: the phase-2 ceiling is read out of phase 1's own
ledger, and the two runs read the same bytes because phase 2 runs against a
`fork()` of the prepared sandbox rather than a second snapshot of a production
that keeps moving.

WHY THE REPLAY IS NO LONGER PINNED TO THE INCIDENT RUN (2026-09-26)
--------------------------------------------------------------------
The version above pinned `replay_run` to the 2026-08-28 recording while the
sandbox was a snapshot of production TAKEN TODAY. That pairing decayed, and
by 2026-09-26 it had gone all the way: phase 1 ended `no_data`, never
reaching the Portfolio Manager, so the ceiling was never measured at all.

The mechanism, from the run's own log: `tech_analyst` batches are split into
chunks by RENDERED PROMPT SIZE (`_split_to_budget` /`pack_to_budget` in
src/agents/tech_analyst.py), so the chunk a symbol lands in is a function of
today's universe and today's per-symbol payload. Replaying August's four
recorded chunk answers into today's chunks put every recorded row in the
wrong chunk: all four answers were discarded whole by the
"emitted rows for symbols not in the submitted chunk" guard (33 rows dropped,
0 kept), `ctx.analyses` came back empty, and src/pipeline.py stopped the
session at `no_data` well before any spending decision. Nothing about the
cost circuit was involved or exercised.

Widening the drift tolerance would have made the test permanently vacuous.
The fix is to stop pairing a fixed old recording with a moving snapshot:
the recording is now SELECTED from the snapshot, with the harness's own
`select_replay_run` — the most recent COMPLETE morning run in the database
being rehearsed — and the rehearsed date comes from that recording. Recording
and snapshot are then contemporaneous by construction, so the chunking lines
up, and the pin is still explicit and printed in the report (an unpinned
replay is a non-reproducible verdict; see ops/rehearsal/runner.py).

WHICH PORTFOLIO MANAGER CALL THE CEILING CAN ACTUALLY REFUSE
--------------------------------------------------------------
With the replay contemporaneous, phase 1 reaches the Portfolio Manager and
the measurement that comes back is this [measured, sandbox `agent_logs`,
rehearsal of the 2026-09-25 morning, 2026-09-26]:

    news_analyst_morning  gemini-3.5-flash-lite   $0.000000
    smart_money_analyst   gemini-3.5-flash-lite   $0.000000
    macro_analyst         gemini-3.5-flash-lite   $0.000000
    tech_analyst          gemini-3.5-flash-lite   $0.000000
    portfolio_manager     openai/gpt-5.5          $0.210995

Every research seat now runs on the direct Gemini route, which
`src/cost_table.py` pins at 0.0/0.0 input/output on purpose (owner decision,
free tier). So the spend SETTLED at the moment the Portfolio Manager's FIRST
call is authorized is exactly $0.00 — and `src/config.py` requires
`session_cost_limit_usd > 0`. No valid session ceiling can pre-empt that
first call, and setting one below a figure the session never spent would be
an invented number, so this test no longer claims otherwise.

What it does claim, measured and unweakened: with the ceiling set to phase
1's own settled total, the circuit refuses the Portfolio Manager's NEXT call
(`session LLM spend $0.2110 reached safe limit $0.21`, agent
`portfolio_manager`), the run finishes having settled no more than the
ceiling it was given, and the refusal ends the session cleanly instead of as
an unhandled provider fault.

That the FIRST decision call cannot be pre-empted — the single most expensive
call of the desk's day, and now the only one that costs anything before it —
is a finding about the control's REACH, not about this test. It belongs on
the board, not in a weakened assertion here.

REQUIRES real production data (`sudo -n -u qamc` read access to
`/home/qamc/quant-agent/data`) — this is an ops tool for one specific
deployment, not a portable unit test, and the harness's whole design (see
`ops/rehearsal/isolation.py`) is built around exactly this read path. Skips
cleanly wherever that access isn't available. Everything past the snapshot
copy is offline: no provider call, no network, no write to production — see
the isolation checks this test itself asserts on.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest

PRODUCTION_DB = "/home/qamc/quant-agent/data/quant_agent.db"
PRODUCTION_DATA = "/home/qamc/quant-agent/data"
SUDO_USER = "qamc"

# The real run_id of the 2026-08-28 morning session that this test exists to
# describe. It is documentation now, NOT the recording that gets replayed —
# see "WHY THE REPLAY IS NO LONGER PINNED TO THE INCIDENT RUN" above.
INCIDENT_RUN_ID = "run-be9f8f06"

# Time of day the rehearsal is frozen at: mid-morning, inside the session, on
# whatever DATE the selected recording was actually made. Unchanged from the
# incident's own 09:35 ET; only the date now comes from the recording.
REHEARSED_TIME_ET = (9, 35)

# The settled-spend ceiling that replaced the reserved-exposure projections
# (item 14, 2026-09-02). `_enforce_settled_limits_locked` in src/cost_circuit.py
# raises exactly this code when session spend reaches
# `llm_cost_circuit.session_cost_limit_usd`, and it is a session-scoped quota
# trigger, so it self-heals on a fresh run_id rather than needing an operator.
SETTLED_SESSION_CEILING_CODE = "session_cost_limit"


def _qamc_reachable() -> bool:
    try:
        result = subprocess.run(
            ["sudo", "-n", "-u", SUDO_USER, "test", "-f", PRODUCTION_DB],
            capture_output=True, timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _qamc_reachable(),
    reason=(
        f"requires 'sudo -n -u {SUDO_USER}' read access to {PRODUCTION_DB} — "
        "this is a single-deployment ops acceptance test, not a portable "
        "unit test; not available in this environment"
    ),
)


def _settled_session_spend(db_path, run_id: str) -> float:
    """What the circuit's own ledger says this rehearsal really spent.

    Read from `llm_budget_sessions`, which is the row
    `_enforce_settled_limits_locked` compares against the ceiling — not from
    `agent_logs`, which is the pipeline's separate record of the same calls
    and would only happen to agree.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT actual_cost_usd, costs_exact FROM llm_budget_sessions "
            "WHERE run_id = ?", (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        f"the rehearsal left no cost-circuit session ledger row for {run_id}; "
        "nothing was accounted, so there is no measured ceiling to re-run with"
    )
    assert row[1], (
        "the rehearsal's own spend is not exactly accounted (costs_exact=0), "
        "so it cannot be used as a ceiling"
    )
    return float(row[0] or 0.0)


def _paid_for_agent(db_path, run_id: str, agent_prefix: str) -> float:
    """Settled spend attributed to one agent, from the pipeline's own log."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_logs "
            "WHERE run_id = ? AND agent_name LIKE ?",
            (run_id, agent_prefix + "%"),
        ).fetchone()
    finally:
        conn.close()
    return float(rows[0] or 0.0)


def _settled_before_agent(db_path, run_id: str, agent_prefix: str) -> float:
    """Settled spend logged before this run first called `agent_prefix`.

    `agent_logs.id` is the insertion order of the pipeline's own per-call
    records, which is the order the calls settled in, so everything with a
    lower id had already been paid for when that agent was reached.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        first = conn.execute(
            "SELECT MIN(id) FROM agent_logs WHERE run_id = ? AND agent_name LIKE ?",
            (run_id, agent_prefix + "%"),
        ).fetchone()
        assert first is not None and first[0] is not None, (
            f"{run_id} logged no {agent_prefix} call at all, so there is no "
            "boundary to measure the ceiling at"
        )
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_logs "
            "WHERE run_id = ? AND id < ?", (run_id, first[0]),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0.0)


def _recording_started_utc(db_path, run_id: str) -> str:
    """When the recorded run this rehearsal replays actually began (UTC).

    `agent_logs.timestamp` is SQLite's `datetime('now')`, i.e. UTC — the same
    assumption `select_replay_run` documents and orders on.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(timestamp) FROM agent_logs WHERE run_id = ?", (run_id,),
        ).fetchone()
    finally:
        conn.close()
    return str((row or [None])[0] or "")


def _reached_provider(report, agent: str) -> bool:
    """True when `agent` got past the circuit and asked replay for an answer.

    `missing_recorded_response` is raised by `ResponseLibrary.match`, which
    `ops/rehearsal/replay.py` calls only AFTER `authorize(model)` has already
    let the call through. So the finding's presence is proof the circuit
    permitted the call; its absence, for an agent the session definitely
    reaches, is proof the circuit stopped it first.
    """
    return any(
        f["kind"] == "missing_recorded_response" and f["agent"] == agent
        for f in report.findings
    ) or any(a["agent"] == agent for a in report.agents_ran)


def test_the_settled_cost_ceiling_still_suspends_paid_analysis(tmp_path):
    from ops.rehearsal.isolation import Sandbox
    from ops.rehearsal.replay import select_replay_run
    from ops.rehearsal.runner import run_rehearsal
    from src.trading_calendar import ET

    prepared = Sandbox.prepare(
        source_db=PRODUCTION_DB,
        root=tmp_path / "sandbox",
        source_data_dir=PRODUCTION_DATA,
        sudo_user=SUDO_USER,
    )

    # The recording is chosen FROM the snapshot, so the two are contemporaneous
    # and tech_analyst's size-packed chunks line up. `complete` means the run
    # has a recording for both tech_analyst and portfolio_manager, which is
    # exactly what phase 1 needs to reach the spending boundary.
    choice = select_replay_run(str(prepared.db_path), "morning")
    if not choice.run_id or not choice.complete:
        pytest.skip(
            "no COMPLETE recorded morning run exists in this snapshot, so "
            "there is nothing to replay the Portfolio Manager's call from "
            f"and the ceiling cannot be measured: {choice.reason}"
        )
    started = _recording_started_utc(prepared.db_path, choice.run_id)
    assert started, (
        f"the selected recording {choice.run_id} has no timestamp to rehearse "
        "at, so the frozen clock would be arbitrary"
    )
    recorded_date = (
        datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=timezone.utc).astimezone(ET).date()
    )
    # Its own date — a day the desk demonstrably traded, because it recorded a
    # whole morning on it — at the incident's own mid-morning hour.
    now_et = datetime(
        recorded_date.year, recorded_date.month, recorded_date.day,
        *REHEARSED_TIME_ET, tzinfo=ET,
    )

    # Fork BEFORE the first session writes to it, so phase 2 reads the same
    # bytes phase 1 read and the only difference between the two runs is the
    # ceiling. A second Sandbox.prepare() would re-snapshot a production that
    # has moved on.
    forked = prepared.fork(tmp_path / "sandbox-2")

    # ---------------------------------------------------------- phase 1
    # Production's own configured ceiling, untouched.
    baseline = run_rehearsal(
        prepared,
        session="morning",
        now_et=now_et,
        replay_run=choice.run_id,
        production_db=PRODUCTION_DB,
        sudo_user=SUDO_USER,
    )

    # Isolation actually held — belt-and-suspenders on top of the harness's
    # own internal asserts, which would have raised already.
    assert any("byte-identical" in c for c in baseline.isolation_checks)

    # docs/WORK.md item 20 (2026-09-14): the session now refuses to DECIDE
    # when a research seat was asked and its answer never arrived. A
    # rehearsal replays the LLM side but calls the DATA providers for real,
    # so on a box without working FRED / wire-feed credentials the macro and
    # news seats genuinely have no answer and the gate correctly stops the
    # session before the Portfolio Manager. That is the harness's
    # environment, not the spending behaviour this test exists to measure —
    # skip rather than assert against it, and never bypass the gate to make
    # a test reach further than a real session would.
    if baseline.status == "evidence_gate_skip":
        pytest.skip(
            "the rehearsal's research seats had no data on this box, so the "
            "evidence gate refused the decision before the Portfolio "
            "Manager; the cost ceiling cannot be measured from here "
            f"(status={baseline.status!r}, agents_ran={baseline.agents_ran})"
        )

    # The chunk un-merge regression guard. Before that fix, replay had one
    # recorded answer for four real tech_analyst chunk calls and the session
    # died on the second chunk, nowhere near the Portfolio Manager. Reaching
    # the Portfolio Manager at all is what proves it is still fixed.
    #
    # Deliberately NOT asserted: that tech_analyst never runs out of recorded
    # chunks. The snapshot is taken now and today's universe needs more chunks
    # than 2026-08-28 recorded, so running dry on the last one is expected
    # drift, reported as a finding. Asserting on it is what kept this test red.
    assert _reached_provider(baseline, "portfolio_manager"), (
        "the session never reached the Portfolio Manager at production's own "
        "ceiling, so there is no 'before' to compare the ceiling run against. "
        f"agents_ran={[a['agent'] for a in baseline.agents_ran]} "
        f"status={baseline.status!r} error={baseline.error!r}"
    )
    assert not any(
        b["trigger_code"] == SETTLED_SESSION_CEILING_CODE
        for b in baseline.blocked_agents
    ), (
        "production's configured ceiling stopped this session on its own, so "
        "phase 2 would prove nothing. "
        f"blocked_agents={baseline.blocked_agents}"
    )

    baseline_run_id = f"rehearsal-morning-{now_et.strftime('%Y%m%d')}"
    settled_total = _settled_session_spend(prepared.db_path, baseline_run_id)
    assert settled_total > 0, (
        "the rehearsal settled no spend at all, so there is no measured "
        "ceiling to re-run against"
    )

    # It really did get an answer and really was billed for it, so the session
    # total below is a decision that was actually paid for.
    assert _paid_for_agent(prepared.db_path, baseline_run_id, "portfolio_manager") > 0.0

    # MEASURED, and the reason this test can no longer claim the ceiling
    # pre-empts the Portfolio Manager — see the docstring section of the same
    # name. What had settled by the moment the Portfolio Manager was reached:
    pre_decision_usd = _settled_before_agent(
        prepared.db_path, baseline_run_id, "portfolio_manager",
    )
    assert pre_decision_usd < settled_total, (
        "the spend settled before the decision is not below the session "
        f"total, so the Portfolio Manager's own call is unaccounted: "
        f"pre={pre_decision_usd!r} total={settled_total!r}"
    )

    ceiling_usd = settled_total

    # ---------------------------------------------------------- phase 2
    # Identical inputs, identical replay, one changed number: the ceiling is
    # the money this very session settled, measured from its own ledger.
    blocked = run_rehearsal(
        forked,
        session="morning",
        now_et=now_et,
        replay_run=choice.run_id,
        production_db=PRODUCTION_DB,
        sudo_user=SUDO_USER,
        config_overrides={
            "llm_cost_circuit.session_cost_limit_usd": ceiling_usd,
        },
    )

    assert any("byte-identical" in c for c in blocked.isolation_checks)

    ceiling_trips = [
        b for b in blocked.blocked_agents
        if b["trigger_code"] == SETTLED_SESSION_CEILING_CODE
    ]
    assert ceiling_trips, (
        "the settled-cost ceiling did not fire even though the session spent "
        f"${ceiling_usd:.7f} against a ${ceiling_usd:.7f} limit — the "
        "protection that replaced the 2026-08-28 reserved-exposure ceiling is "
        f"gone. blocked_agents={blocked.blocked_agents} "
        f"status={blocked.status!r} error={blocked.error!r}"
    )

    # It is the DECISION seat the ceiling refuses. The Portfolio Manager's
    # first call is what carries the session over the cap (every research seat
    # ahead of it is on a $0 route), and the re-ask that follows is the call
    # the circuit stops — so the agent named on the hold is the one this test
    # has always been about.
    assert any(b["agent"] == "portfolio_manager" for b in ceiling_trips), (
        "the settled ceiling fired, but not on the Portfolio Manager — the "
        "one seat whose spend is the whole session's. "
        f"blocked_agents={blocked.blocked_agents}"
    )

    # And nothing got through above the cap: the money this run finished
    # having settled must not exceed the ceiling it was given. This is the
    # direct measurement, in the circuit's own ledger, that the refusal above
    # actually prevented spend rather than merely being recorded.
    spent_under_ceiling = _settled_session_spend(forked.db_path, baseline_run_id)
    assert spent_under_ceiling <= ceiling_usd, (
        f"the session settled ${spent_under_ceiling:.7f} against a "
        f"${ceiling_usd:.7f} ceiling — a call was paid for above the cap. "
        f"blocked_agents={blocked.blocked_agents}"
    )
    # ...and the refusal ends the run cleanly. Either the circuit suspended
    # paid analysis outright (what happens when the refused call was one the
    # session could not proceed without), or the session finished on the
    # answer it already had — as here, where the refused call is the decision
    # seat's optional re-ask and the desk correctly decides on what it has.
    # What must NEVER happen is the shape this rehearsal produced while route
    # demotions leaked between runs: an unhandled provider fault. A refusal
    # the code does not handle is a refusal that takes the desk down.
    assert blocked.error is None or "paid analysis suspended" in blocked.error, (
        "the ceiling fired and the run then died on something the pipeline "
        f"did not handle: status={blocked.status!r} error={blocked.error!r}"
    )
    assert blocked.status != "did_not_finish", (
        "the ceiling fired and the session did not reach a terminal state: "
        f"status={blocked.status!r} error={blocked.error!r}"
    )

    # Deliberately NOT asserted: `report.proposed == 0`. That count is every
    # BUY/SELL row the run wrote, whatever wrote it — and a paid-analysis
    # suspension deliberately preserves deterministic loss protection and the
    # broker-resident stops (the circuit's own alert says so). Exit management
    # acting while the thinking is switched off is the design, not a leak past
    # the ceiling. What must hold is that nothing the circuit refused ran
    # anyway, and the assertion above is the direct measurement of that.
