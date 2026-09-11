#!/usr/bin/env python3
"""Proposal-to-fill funnel census for quant-agent.

Answers one question with numbers, not impressions: of everything the
Portfolio Manager proposed as a new entry (BUY/SHORT), how many became a
fill, and — for the ones that did not — which specific rule or failure
killed each one? Built 2026-09-02 because the desk's own "23% conversion"
figure had gone stale and its breakdown had never been done in full (see
`docs/INCIDENT_HISTORY.md`, 2026-09-02 entry, for the narrative writeup this
script was built to keep from going stale again).

Run from the project root:

    ./scripts/blocked_proposals_census.py                  # full history
    ./scripts/blocked_proposals_census.py --since 2026-08-14
    ./scripts/blocked_proposals_census.py --db /path/to/quant_agent.db

All numbers come directly from SQLite (`specialist_evidence` + `trades`) —
no pipeline imports, no broker API. Meant to be cheap and safe to re-run
against a read-only connection.

WHAT THIS SCRIPT CAN AND CANNOT SEE
------------------------------------------------------------------------
The join key is `decision_id` + `symbol`, mirroring
`Database.get_proposal_funnel_rows` / `EveningAnalyst._build_blocked_proposals`
(`src/pipeline.py`) — same tables, same idea, extended here to cover the
WHOLE funnel and to fix one misattribution that tool has: when the AI Risk
Manager vetoes a whole plan (`verdict.approved is False`), the existing
helper blames every one of that decision's ORIGINAL PM targets on the
veto — even a symbol the deterministic constructor had ALREADY dropped
before the Risk Manager ever saw it. This script checks `proposed_order`
membership first, so a constructor-dropped symbol is correctly counted as
a constructor casualty, not an AI Risk Manager one.

2026-09-11: this script now also reads the durable `pipeline_event` rows
`src/pipeline_stages.py` writes for the other three deterministic/agent
causes that used to fall into the unexplained buckets below even though the
pipeline already recorded a real reason for them: a symbol dropped by the
symbol guard (`reason="symbol_guard"`), a symbol dropped by the portfolio-
level hard risk filter, which includes the sector-concentration rule among
others (`reason="hard_risk"`), and every symbol on a plan the AI Risk
Manager's response failed to parse at all (`stage="risk"`,
`reason="risk_manager_unparseable_output"`). `proposed_order` rows are
written for a target BEFORE any of these three checks run, so a symbol they
block already has a `proposed_order` row and, before this fix, had nothing
else — no verdict, no execution_skip — so `classify()` fell through to
`order_not_placed`, indistinguishable from a genuinely stalled run. See
`_load_recorded_reasons` below for the exact (stage, outcome, reason)
tuples this reads. A fourth durable cause, insufficient cash
(`reason="insufficient_cash"`), was ALREADY correctly attributed before
this change: it is written as an `execution_skip` row (a kind this script
has always read via `_load_skips`), not a `pipeline_event` row.

What this script still CANNOT recover from the database alone: the specific
reason the deterministic constructor (`PortfolioConstructor._widen_stop_past_noise` /
`_resolve_entry_and_stop`, `src/portfolio_constructor.py`) dropped a target
is captured (see `_load_recorded_reasons`'s `constructor_dropped` case,
2026-09-03), but only going forward — a drop from before that date left
only `logger.info`/`logger.warning` text, never persisted to a table, and
cannot be reconstructed retroactively. Rows still unexplained after all of
the above are counted under `no_order_built` (PM proposed it, no order was
ever built for it) or `order_not_placed` (an order WAS built and
apparently reviewed, but no trade, no execution_skip and none of the
`pipeline_event` reasons above exist for it either — a stalled/incomplete
run is the most likely explanation for that second shape specifically).

If `journalctl --user` retention still reaches back far enough on the box
this runs on, those two buckets can often be resolved further by grepping
for `"Constructor: <SYMBOL>"` around the proposal's timestamp. As of
2026-09-02 the live box's own `logs/` directory is empty (rotated away) but
the systemd --user journal for the qamc user went back to 2026-08-09 —
that will not last forever, so treat any further split of `no_order_built`
/ `order_not_placed` as a best-effort, time-limited enrichment, not
something this script can guarantee.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "quant_agent.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _entry_targets(con: sqlite3.Connection, since: str | None) -> list[dict]:
    """PM proposals that are ENTRIES (positive size), not exit instructions.

    Mirrors `_build_blocked_proposals`'s own filter: a target sized to zero
    is PM asking to exit a position, not asking to get into one, and
    counting it here would overstate the entry-side block rate exactly the
    way that function's docstring warns against.
    """
    sql = "SELECT decision_id, symbol, run_id, timestamp, evidence_json FROM specialist_evidence WHERE kind='target'"
    args: tuple = ()
    if since:
        sql += " AND timestamp >= ?"
        args = (since,)
    sql += " ORDER BY id"
    out = []
    for r in con.execute(sql, args).fetchall():
        try:
            d = json.loads(r["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        size = d.get("risk_allocation_pct")
        if size is None:
            size = d.get("target_weight_pct")
        try:
            sz = float(size) if size is not None else None
        except (TypeError, ValueError):
            sz = None
        if sz is None or sz <= 0.0:
            continue
        out.append({
            "decision_id": r["decision_id"],
            "symbol": (r["symbol"] or "").strip().upper(),
            "run_id": r["run_id"],
            "ts": r["timestamp"],
        })
    return out


def _load_pairs(con: sqlite3.Connection, kind: str) -> set[tuple[str, str]]:
    rows = con.execute(
        "SELECT decision_id, symbol FROM specialist_evidence WHERE kind=?", (kind,),
    ).fetchall()
    return {(r["decision_id"], (r["symbol"] or "").strip().upper()) for r in rows}


def _load_verdicts(con: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for r in con.execute(
        "SELECT decision_id, evidence_json FROM specialist_evidence WHERE kind='verdict'",
    ).fetchall():
        try:
            out[r["decision_id"]] = json.loads(r["evidence_json"] or "{}")
        except (TypeError, ValueError):
            out[r["decision_id"]] = {}
    return out


def _load_skips(con: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    out = {}
    for r in con.execute(
        "SELECT decision_id, symbol, evidence_json FROM specialist_evidence WHERE kind='execution_skip'",
    ).fetchall():
        try:
            d = json.loads(r["evidence_json"] or "{}")
        except (TypeError, ValueError):
            d = {}
        out[(r["decision_id"], (r["symbol"] or "").strip().upper())] = d
    return out


# (stage, outcome, reason) tuples that `pipeline_stages.py` writes as a
# `pipeline_event` row for a REAL, durable cause this census can now cite
# by name instead of dumping the symbol into an unexplained bucket. The
# dict value is the census's own label for that cause — kept identical to
# the `reason` string already used elsewhere so a new occurrence groups
# with any historical text that mentions the same cause.
_RECORDED_REASON_KINDS: dict[tuple[str, str, str], str] = {
    # 2026-09-03 — constructor drops a target before ever building a
    # `proposed_order` row (see `PortfolioConstructor.last_drop_reasons`).
    ("deterministic_gate", "blocked", "constructor_dropped"): "constructor_dropped",
    # 2026-09-11 — the deterministic symbol guard blocks a symbol AFTER a
    # `proposed_order` row already exists for it (RiskStage runs after
    # DecisionStage), so before this it fell through to `order_not_placed`.
    ("deterministic_gate", "blocked", "symbol_guard"): "symbol_guard",
    # 2026-09-11 — the portfolio-level hard risk filter (includes the
    # sector-concentration rule among others in `src/risk/rules.py`) blocks
    # a symbol the same way, same previously-misclassified shape.
    ("deterministic_gate", "blocked", "hard_risk"): "hard_risk",
    # 2026-09-11 — the AI Risk Manager's response failed to parse at all
    # (no verdict row is ever written in this case, since `RiskStage` only
    # persists kind='verdict' when a verdict object exists), so every
    # symbol on that plan used to fall through to `order_not_placed` too.
    ("risk", "failed", "risk_manager_unparseable_output"): "risk_manager_unparseable_output",
}


def _load_recorded_reasons(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Durable `pipeline_event` reasons this census can attribute by name.

    Before 2026-09-03 these causes had NO row of any kind and fell into
    `no_order_built` / `order_not_placed` with no further explanation
    recoverable from the database (module docstring above) — the pipeline
    logged a real sentence explaining each one, but only ever to
    `logger.info`/`logger.warning`, never to a table. `_record_pipeline_event`
    now persists one for each cause in `_RECORDED_REASON_KINDS`. Runs from
    before a given cause's own persistence date still have no such row and
    still fall through to the old unexplained buckets — this only narrows
    them going forward, it cannot retroactively explain history.
    """
    out: dict[tuple[str, str], str] = {}
    for r in con.execute(
        "SELECT decision_id, symbol, evidence_json FROM specialist_evidence "
        "WHERE kind='pipeline_event'",
    ).fetchall():
        try:
            d = json.loads(r["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        label = _RECORDED_REASON_KINDS.get(
            (d.get("stage"), d.get("outcome"), d.get("reason")),
        )
        if label is None:
            continue
        key = (r["decision_id"], (r["symbol"] or "").strip().upper())
        # First recorded reason wins if a symbol somehow matches more than
        # one tuple (should not happen given the pipeline's own ordering,
        # but a duplicate should never overwrite a real reason with another
        # real reason non-deterministically).
        out.setdefault(key, label)
    return out


def _load_fills(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Winner-take-'filled' per (decision_id, symbol): a retry/repeg can emit
    more than one trades row for the same proposal; one fill converts it
    regardless of what else happened first.
    """
    fills: dict[tuple[str, str], str] = {}
    rows = con.execute(
        "SELECT decision_id, symbol, fill_status FROM trades "
        "WHERE decision_id IS NOT NULL ORDER BY id",
    ).fetchall()
    for r in rows:
        key = (r["decision_id"], (r["symbol"] or "").strip().upper())
        status = (r["fill_status"] or "").strip().lower()
        if not status:
            continue
        if fills.get(key) == "filled":
            continue
        fills[key] = status
    return fills


def classify(
    did: str, sym: str, *, ordered: set, verdicts: dict, skips: dict, fills: dict,
    recorded_reasons: dict | None = None,
) -> str | None:
    """None == converted (filled). Otherwise the verbatim/derived cause.

    Precedence follows the pipeline's OWN order of operations: constructor
    -> deterministic symbol guard -> portfolio-level hard risk filter
    (includes sector concentration) -> AI Risk Manager -> execution ->
    broker. A verdict rejection or zeroing is only attributed to a symbol
    that is confirmed to have reached the constructor's OWN order list
    (`ordered`) -- the fix over the existing `_outcome` helper. `skips`
    (execution_skip rows — covers, among others, `insufficient_cash`) and
    `recorded_reasons` (pipeline_event rows — covers `constructor_dropped`,
    `symbol_guard`, `hard_risk`, `risk_manager_unparseable_output`) are both
    checked BEFORE the verdict/`ordered` fallback, because every one of
    those causes can leave a `proposed_order` row with no verdict and no
    fill — the exact shape that used to be misread as `order_not_placed`.
    """
    key = (did, sym)
    status = fills.get(key)
    if status == "filled":
        return None
    if status:
        return f"order_{status}"
    if key in skips:
        return skips[key].get("reason") or "execution_skip_unlabeled"
    if recorded_reasons and key in recorded_reasons:
        return recorded_reasons[key]
    v = verdicts.get(did)
    if isinstance(v, dict) and key in ordered:
        if v.get("approved") is False:
            cat = (v.get("reason_category") or "").strip()
            return f"rm_rejected:{cat}" if cat else "rm_rejected"
        for mod in (v.get("modifications") or []):
            if not isinstance(mod, dict):
                continue
            if (mod.get("symbol") or "").strip().upper() != sym:
                continue
            try:
                if float(mod.get("new_value")) == 0.0:
                    return "rm_zeroed"
            except (TypeError, ValueError):
                continue
    if key in ordered:
        return "order_not_placed"
    return "no_order_built"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to quant_agent.db (read-only)")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD or full timestamp; default is all recorded history")
    ap.add_argument("--min-repeat", type=int, default=3, help="symbol repeat threshold for the 'never fills' section")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the plain-language report")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 2

    con = _connect(args.db)
    try:
        entry = _entry_targets(con, args.since)
        if not entry:
            print("No entry proposals found in the selected window.")
            return 0
        ordered = _load_pairs(con, "proposed_order")
        verdicts = _load_verdicts(con)
        skips = _load_skips(con)
        fills = _load_fills(con)
        recorded_reasons = _load_recorded_reasons(con)

        results = []
        for t in entry:
            reason = classify(
                t["decision_id"], t["symbol"], ordered=ordered, verdicts=verdicts,
                skips=skips, fills=fills, recorded_reasons=recorded_reasons,
            )
            results.append({**t, "reason": reason})

        total = len(results)
        converted = sum(1 for r in results if r["reason"] is None)
        by_reason = Counter(r["reason"] for r in results if r["reason"])

        byday: dict[str, list[dict]] = defaultdict(list)
        for r in results:
            byday[r["ts"][:10]].append(r)

        bysym: dict[str, dict] = defaultdict(lambda: {"proposed": 0, "filled": 0, "reasons": Counter()})
        for r in results:
            b = bysym[r["symbol"]]
            b["proposed"] += 1
            if r["reason"] is None:
                b["filled"] += 1
            else:
                b["reasons"][r["reason"]] += 1

        if args.json:
            payload = {
                "db": str(args.db),
                "since": args.since,
                "date_range": [min(r["ts"] for r in results), max(r["ts"] for r in results)],
                "total_proposals": total,
                "filled": converted,
                "conversion_pct": round(100 * converted / total, 1),
                "causes": dict(by_reason.most_common()),
                "by_day": {
                    day: {
                        "proposed": len(rs),
                        "filled": sum(1 for r in rs if r["reason"] is None),
                        "zero_fill": all(r["reason"] is not None for r in rs),
                        "causes": dict(Counter(r["reason"] for r in rs if r["reason"])),
                    }
                    for day, rs in sorted(byday.items())
                },
                "repeat_never_filled": {
                    sym: {"proposed": b["proposed"], "reasons": dict(b["reasons"])}
                    for sym, b in bysym.items()
                    if b["proposed"] >= args.min_repeat and b["filled"] == 0
                },
            }
            print(json.dumps(payload, indent=2, default=str))
            return 0

        print("=" * 72)
        print("PROPOSAL -> FILL CENSUS")
        print("=" * 72)
        print(f"db: {args.db}")
        print(f"window: {min(r['ts'] for r in results)}  to  {max(r['ts'] for r in results)}")
        print(f"entry proposals: {total}")
        print(f"filled: {converted} ({100 * converted / total:.1f}%)")
        print(f"blocked: {total - converted} ({100 * (total - converted) / total:.1f}%)")
        print()
        print("-- ranked causes, worst first (share of ALL proposals, share of BLOCKED) --")
        for reason, n in by_reason.most_common():
            print(f"  {n:3d}  {n / total * 100:5.1f}% of all   {n / (total - converted) * 100:5.1f}% of blocked   {reason}")
        print()
        print("-- per-day: proposed / filled --")
        for day in sorted(byday):
            rs = byday[day]
            filled_n = sum(1 for r in rs if r["reason"] is None)
            flag = "  <- ZERO FILLS" if filled_n == 0 else ""
            dominant = Counter(r["reason"] for r in rs if r["reason"]).most_common(1)
            dom_txt = f" dominant={dominant[0][0]}" if dominant else ""
            print(f"  {day}  proposed={len(rs):2d}  filled={filled_n:2d}{dom_txt}{flag}")
        print()
        print(f"-- symbols proposed {args.min_repeat}+ times with ZERO fills --")
        never = [(s, b) for s, b in bysym.items() if b["proposed"] >= args.min_repeat and b["filled"] == 0]
        if not never:
            print("  none")
        else:
            for sym, b in sorted(never, key=lambda kv: -kv[1]["proposed"]):
                print(f"  {sym:6s} proposed {b['proposed']}x, filled 0x — {dict(b['reasons'])}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
