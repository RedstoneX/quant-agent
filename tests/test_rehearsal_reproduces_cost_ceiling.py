"""Acceptance test: the rehearsal harness reproduces the 2026-08-28 morning's
Portfolio Manager cost-ceiling failure — offline, deterministically, for free.

This is what `ops/rehearsal` was built for (see its module docstrings): on
2026-08-28 the Portfolio Manager never reached a provider. Its `_execute`
call was stopped by `cost_circuit.begin_call` because the assembled prompt's
pre-call estimate projected session spend past the reserved-exposure ceiling
($1.80 at the time; `config/settings.yaml` still pins that value). No trade
was proposed, and the session ended `paid_analysis_suspended`.

Before the fix in `ops/rehearsal/replay.py` (see test_rehearsal_replay.py),
the harness could not even reach that point: tech_analyst's 4 real provider
calls that morning collapsed into one `agent_logs` row (a pre-existing,
documented limitation of `analyze_batch`'s chunk merge — see
src/agents/tech_analyst.py), replay had only 1 recorded answer to hand back
to 4 real chunk calls, and the run died on the second chunk with
`MissingRecordedResponse` — masking the actual incident behind an unrelated
`failed_call_unknown_cost` circuit trip on tech_analyst itself. This test
would have failed for that reason before the fix; it is the harness's own
acceptance test, not a test of a pipeline feature.

REQUIRES real production data (`sudo -n -u qamc` read access to
`/home/qamc/quant-agent/data`) — this is an ops tool for one specific
deployment, not a portable unit test, and the harness's whole design (see
`ops/rehearsal/isolation.py`) is built around exactly this read path. Skips
cleanly wherever that access isn't available (any machine other than this
one). Everything it does past the snapshot copy is offline: no provider
call, no network, no write to production — see the isolation checks this
test itself asserts on.

WHAT "REPRODUCE" MEANS HERE: the FAILURE MODE, not a byte-identical replay.
`ops/rehearsal/runner.py` says outright a rehearsal is "a fresh session
against a snapshot of production's state, not a re-enactment of a past one".
The snapshot is taken NOW, not frozen at 2026-08-28 09:30 ET, so current
watchlist/position drift and the offline asset-eligibility gate (broker
stubbed — see ops/rehearsal/broker.py) can legitimately change which exact
symbols reach tech_analyst and therefore the exact dollar figure the
Portfolio Manager's prompt projects. What must reproduce, and does, is the
qualitative incident: the reserved-exposure ceiling — not a different limit,
not a crash, not a silent no-op — stops the Portfolio Manager before it
proposes anything.
"""

from __future__ import annotations

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

# The reserved-exposure ceiling family of circuit trips — see
# src/cost_circuit.py. `begin_call` checks it once before any provider
# attempt (`projected_session_cost_limit`); `before_provider_attempt`
# re-checks it at every actual network boundary
# (`provider_projected_session_cost_limit`,
# `outstanding_projected_session_cost_limit`) because "a reservation can wait
# behind a provider semaphore while an earlier call settles above its
# estimate ... an old reservation is not a blank cheque to spend past the
# cap" (cost_circuit.py). Which one trips first for the Portfolio Manager can
# legitimately shift between runs depending on retry/failover timing; all
# three are the same defense enforced at different points, and any of them
# tripping on portfolio_manager is the incident reproducing.
RESERVED_EXPOSURE_TRIP_CODES = {
    "projected_session_cost_limit",
    "provider_projected_session_cost_limit",
    "outstanding_projected_session_cost_limit",
}


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


def test_rehearsal_reproduces_2026_08_28_pm_cost_ceiling_failure(tmp_path):
    from ops.rehearsal.isolation import Sandbox
    from ops.rehearsal.runner import run_rehearsal
    from src.trading_calendar import ET

    sandbox = Sandbox.prepare(
        source_db=PRODUCTION_DB,
        root=tmp_path / "sandbox",
        source_data_dir=PRODUCTION_DATA,
        sudo_user=SUDO_USER,
    )
    report = run_rehearsal(
        sandbox,
        session="morning",
        now_et=datetime(2026, 8, 28, 9, 35, tzinfo=ET),
        replay_run=INCIDENT_RUN_ID,
        production_db=PRODUCTION_DB,
        sudo_user=SUDO_USER,
    )

    # --- isolation actually held (belt-and-suspenders on top of the
    # harness's own internal asserts, which would have raised already) ---
    assert any("byte-identical" in c for c in report.isolation_checks)

    # --- the defect this test exists to catch from ever coming back:
    # tech_analyst must not run out of recorded chunk responses ---
    missing_tech = [
        f for f in report.findings
        if f["kind"] == "missing_recorded_response" and f["agent"] == "tech_analyst"
    ]
    assert not missing_tech, (
        f"tech_analyst ran out of recorded chunk responses again: {missing_tech}"
    )
    assert any(a["agent"] == "tech_analyst" for a in report.agents_ran), (
        "tech_analyst should have run (replayed), not merely avoided the "
        "missing-response finding"
    )

    # --- the actual acceptance criterion: the Portfolio Manager gets
    # stopped by the reserved-exposure ceiling, exactly as it was on
    # 2026-08-28 — zero trades, session ends suspended ---
    assert report.status == "paid_analysis_suspended", (
        f"expected the session to end paid_analysis_suspended, got "
        f"{report.status!r} (error={report.error!r})"
    )
    assert report.executed == 0
    assert report.proposed == 0
    assert not report.orders_recorded

    pm_blocks = [b for b in report.blocked_agents if b["agent"] == "portfolio_manager"]
    assert pm_blocks, (
        f"portfolio_manager was not blocked at all — blocked_agents="
        f"{report.blocked_agents}"
    )
    ceiling_blocks = [
        b for b in pm_blocks if b["trigger_code"] in RESERVED_EXPOSURE_TRIP_CODES
    ]
    assert ceiling_blocks, (
        f"portfolio_manager was blocked, but not by the reserved-exposure "
        f"ceiling: {pm_blocks}"
    )
    assert any("reserved-exposure ceiling" in b["detail"] for b in ceiling_blocks)
