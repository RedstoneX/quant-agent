"""How much paid-model credit is left, and is that enough for tomorrow?

On 2026-09-29 the single OpenRouter balance ran out and every paid seat was
refused at once. This module is the early warning, read by the dashboard's
/health payload and spoken by every out-of-credit message. It reuses the
desk's own spend record (`llm_budget_days`, written by the cost circuit) and
invents nothing. It makes NO network call of its own.

WHERE THE BALANCE COMES FROM, in order:
  1. `data/openrouter_balance.json` -- the provider's own figure (OpenRouter
     GET /api/v1/credits: total_credits - total_usage), written by
     `src.openrouter_balance.record_openrouter_balance()` from the twice-daily
     pricing-refresh timer. That fetch goes through cost_table, the
     module that already reaches openrouter.ai (replay-seam guard). When the
     provider refuses the call the timer logs the reason at ERROR, no file is
     written, and step 2 applies. Spend recorded after the snapshot day is
     subtracted so the figure keeps falling between refreshes.
  2. DERIVED: the recorded top-up (`llm_cost_circuit.openrouter_topup_usd`
     and `_date`) minus the desk's recorded spend from that date on.
The payload says which one it used (`source`). A snapshot file that exists
but cannot be read is reported as `unknown` naming the file -- it does not
quietly fall through to the derived figure.

THE TRIGGER IS MEASURED, NOT PICKED. One full trading day costs what the
desk's own record says it cost; the worst recorded full day is the yardstick.
The warning fires when the balance is below TWO of those: the balance seen
now must pay for the day in progress AND leave one more full session.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from src.openrouter_balance import OPENROUTER_BALANCE_PATH as SNAPSHOT_PATH


def _day_costs(conn: sqlite3.Connection) -> dict[str, float]:
    return {
        r[0]: float(r[1])
        for r in conn.execute("SELECT day, baseline_cost_usd + incremental_cost_usd FROM llm_budget_days")
    }


def compute_state(
    day_costs: dict[str, float],
    *,
    snapshot: dict | None,
    topup_usd: float | None,
    topup_date: str | None,
) -> dict:
    """Pure: spend record + balance source in, plain-English state out."""
    spend_days = {d: c for d, c in day_costs.items() if c > 0}
    if not spend_days:
        return {
            "status": "unknown",
            "message": "No paid-model spend recorded yet, so the remaining credit cannot be timed.",
        }
    worst = max(spend_days.values())
    mean = sum(spend_days.values()) / len(spend_days)

    if snapshot and snapshot.get("remaining_usd") is not None:
        source = "provider"
        since = str(snapshot["as_of_day"])
        remaining = float(snapshot["remaining_usd"]) - sum(c for d, c in day_costs.items() if d > since)
    elif topup_usd is not None and topup_date:
        source = "derived"
        remaining = float(topup_usd) - sum(c for d, c in day_costs.items() if d >= topup_date)
    else:
        return {
            "status": "unknown",
            "message": "The remaining paid-model credit is not known: the provider gave no balance and no top-up is recorded.",
        }

    remaining = max(remaining, 0.0)
    trigger = 2 * worst
    days_left = remaining / mean if mean else None
    how = "from the provider" if source == "provider" else "estimated from your last top-up minus recorded spend"
    if remaining < trigger:
        status = "low"
        message = (
            f"Paid-model credit is low: about ${remaining:.2f} left ({how}), "
            f"roughly {days_left:.1f} trading days at the usual pace and "
            f"{remaining / worst:.1f} of the most expensive day on record. "
            "Top up OpenRouter now, or the decision seats lose their paid routes."
        )
    else:
        status = "ok"
        message = (
            f"Paid-model credit: about ${remaining:.2f} left ({how}), "
            f"roughly {days_left:.0f} trading days at the usual pace."
        )
    return {
        "status": status,
        "source": source,
        "message": message,
        "remaining_usd": round(remaining, 2),
        "worst_day_usd": round(worst, 3),
        "mean_day_usd": round(mean, 3),
        "warn_below_usd": round(trigger, 2),
        "days_left": round(days_left, 1) if days_left is not None else None,
    }


def read_state(db_path: str, *, topup_usd, topup_date, snapshot_path: Path = SNAPSHOT_PATH) -> dict:
    """Never raises: a failed read is `unknown`, never `ok`."""
    try:
        if snapshot_path.exists():
            try:
                snapshot = json.loads(snapshot_path.read_text())
            except (OSError, ValueError) as exc:
                return {
                    "status": "unknown",
                    "message": f"The provider balance snapshot {snapshot_path} exists but cannot be read, so the remaining credit is not known.",
                    "error": str(exc),
                }
        else:
            snapshot = None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            costs = _day_costs(conn)
        finally:
            conn.close()
        return compute_state(costs, snapshot=snapshot, topup_usd=topup_usd, topup_date=topup_date)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "message": "The credit check could not run.", "error": str(exc)}


UNKNOWN_LINE = "Paid-model credit left: balance unknown."


def balance_line() -> str:
    """THE one wording for the balance, called by every site that reports a
    credit refusal and by the dashboard. Unreadable reads as "balance
    unknown" -- never blank, never $0.00. Never raises."""
    try:
        # Read the YAML directly: src.config sits ABOVE this module (it pulls in
        # src.agents.base, which reports refusals through here), so importing
        # it would close an import cycle.
        raw = yaml.safe_load(Path("config/settings.yaml").read_text()) or {}
        cc = raw.get("llm_cost_circuit") or {}
        state = read_state(
            raw["storage"]["db_path"],
            topup_usd=cc.get("openrouter_topup_usd"),
            topup_date=cc.get("openrouter_topup_date"),
        )
        if state.get("status") == "unknown":
            return UNKNOWN_LINE
        return state["message"]
    except Exception:  # noqa: BLE001
        return UNKNOWN_LINE
