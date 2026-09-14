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
  changed: the settled-cost circuit must trip, and the Portfolio Manager must
  never reach the provider boundary at all.

No invented number anywhere: the phase-2 ceiling is read out of phase 1's own
ledger, and the two runs read the same bytes because phase 2 runs against a
`fork()` of the prepared sandbox rather than a second snapshot of a production
that keeps moving.

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
from datetime import datetime

import pytest

PRODUCTION_DB = "/home/qamc/quant-agent/data/quant_agent.db"
PRODUCTION_DATA = "/home/qamc/quant-agent/data"
SUDO_USER = "qamc"

# The real run_id of the 2026-08-28 morning session whose recorded model
# responses this test replays. Historical agent_logs rows are retained for
# 2 years (src/storage/db.py), so this should stay resolvable for a long time.
INCIDENT_RUN_ID = "run-be9f8f06"

REHEARSED_AT = (2026, 8, 28, 9, 35)

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


def test_the_settled_cost_ceiling_still_stops_the_portfolio_manager(tmp_path):
    from ops.rehearsal.isolation import Sandbox
    from ops.rehearsal.runner import run_rehearsal
    from src.trading_calendar import ET

    now_et = datetime(*REHEARSED_AT, tzinfo=ET)
    prepared = Sandbox.prepare(
        source_db=PRODUCTION_DB,
        root=tmp_path / "sandbox",
        source_data_dir=PRODUCTION_DATA,
        sudo_user=SUDO_USER,
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
        replay_run=INCIDENT_RUN_ID,
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
    ceiling_usd = _settled_session_spend(prepared.db_path, baseline_run_id)
    assert ceiling_usd > 0, (
        "the rehearsal settled no spend at all, so there is no measured "
        "ceiling to re-run against"
    )
    # The Portfolio Manager contributed nothing to that figure — it never got
    # an answer to bill for — so the measured ceiling is exactly the spend that
    # had settled by the time it was reached, which is the boundary the
    # circuit checks.
    assert _paid_for_agent(prepared.db_path, baseline_run_id, "portfolio_manager") == 0.0

    # ---------------------------------------------------------- phase 2
    # Identical inputs, identical replay, one changed number: the ceiling is
    # now the money this very session had already spent by the time it got to
    # the Portfolio Manager.
    blocked = run_rehearsal(
        forked,
        session="morning",
        now_et=now_et,
        replay_run=INCIDENT_RUN_ID,
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

    # The incident itself: the Portfolio Manager is refused BEFORE it can
    # spend. In phase 1 it reached the provider boundary and asked a question;
    # here it must not get that far.
    assert not _reached_provider(blocked, "portfolio_manager"), (
        "the Portfolio Manager still reached the provider boundary with the "
        "ceiling already spent — the circuit let a call through above its "
        f"cap. findings={blocked.findings} "
        f"blocked_agents={blocked.blocked_agents}"
    )
    # ...and the session ends the way 2026-08-28 ended: paid analysis stopped
    # by the spending circuit, not by a crash and not by a decision.
    assert "paid analysis suspended" in (blocked.error or ""), (
        "the ceiling fired but the session did not end as a spending "
        f"suspension: status={blocked.status!r} error={blocked.error!r}"
    )

    # Deliberately NOT asserted: `report.proposed == 0`. That count is every
    # BUY/SELL row the run wrote, whatever wrote it — and a paid-analysis
    # suspension deliberately preserves deterministic loss protection and the
    # broker-resident stops (the circuit's own alert says so). Exit management
    # acting while the thinking is switched off is the design, not a leak past
    # the ceiling. What must be zero is anything the Portfolio Manager
    # decided, and the assertion above — it never reached a provider — is the
    # direct measurement of that.
