#!/usr/bin/env python3
"""The daily floor under the alert-channel watchdog.

WHAT PROVES THE ALARM WORKS, AND WHAT THIS ADDS
-----------------------------------------------
The primary watchdog is the sessions themselves: every one of the five to
six sessions a weekday ends by exercising the whole Telegram path end to
end and writing the verdict to SQLite, where Mission Control renders it.
See `src/alert_watchdog.py` — that module owns the design and the reasoning.

This script runs the SAME check on a timer, once a day, SEVEN days a week.
It exists for one reason: sessions only run Mon-Fri. Without a weekend
floor, "no successful check in the last N hours" could not be an alarm —
it would fire every Saturday — and the whole staleness signal would have to
be switched off. One daily probe makes the longest legal gap between checks
24h plus timer slack, which is what lets `alert_watchdog.STALE_AFTER_HOURS`
be a real threshold instead of a formality.

It also covers the case where every session is failing to start at all: the
sessions cannot report that they did not run, and this unit can.

WHY A PROBE AND NOT A CREDENTIAL CHECK
--------------------------------------
Checking that TELEGRAM_BOT_TOKEN is set answers a question nobody has. The
failures that actually silence this desk all pass a variable check: a token
that is present but revoked, a chat id that is present but wrong, a bot the
operator blocked, an egress rule that drops api.telegram.org. Only a send
catches those, so this sends — one real message with `disable_notification`,
deleted a moment later. See `TelegramNotifier.probe`.

THE WEEKLY DIGEST IS GONE
-------------------------
An earlier version of this script sent the operator one "still alive"
message every Sunday, and its absence was supposed to be how he learned the
channel had died. That was wrong twice over: a routine confirmation is a
message an operator learns to swipe away, and a channel that can be dead for
seven days before anyone notices is not monitored. The durable check history
plus the Mission Control red state replaced it; the operator now hears
nothing at all until something is actually wrong.

NO EXTERNAL MONITORING SERVICE
------------------------------
An earlier version carried a dormant `ALERT_HEARTBEAT_HEALTHCHECK_URL` hook
that would ping a healthchecks.io-style check on success and `/fail` on
failure. The owner refused that outright: this desk does not depend on an
outside service to know whether its own alarm works. The hook is gone
rather than left switched off, because a rejected design sitting unused in
the repo is how it gets turned on by mistake later — and a test asserts it
stays gone.

The consequence is stated plainly rather than worked around: if the BOX
itself dies, nothing running on the box reports it, and no local
engineering can change that. This script and `src/alert_watchdog.py` cover
the channel breaking while the box runs, which is the likely failure and is
fully detectable from here. They do not cover the box being dead, and they
do not pretend to.

TWO RECORDS, TWO JOBS
---------------------
  * SQLite `alert_channel_checks` — the durable record. What Mission
    Control reads, what the sessions also write to, the single source of
    truth for "is the alarm working".
  * data/alerting/heartbeat.json — the on-box record. Deliberately kept:
    `--status` must still answer on a box where the DATABASE is the thing
    that is broken or locked, and it is what the systemd journal points at.

Neither can stop the probe: the probe is the thing that matters, the
records are only how it is remembered.

WHAT IT COSTS
-------------
Two HTTPS requests a day to api.telegram.org. No LLM call, no paid
dependency, no new channel, no new credential, no external service.

USAGE
    python scripts/alert_heartbeat.py                 # probe, record, exit 0/1
    python scripts/alert_heartbeat.py --status        # print the record, send nothing

EXIT CODES
    0  the alert channel was exercised and worked
    1  it did not — the desk currently has no way to reach the operator
    2  bad arguments
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: Absolute, not relative to the working directory. `data/` is gitignored,
#: so this record can never dirty the checkout and become deploy drift —
#: the same reasoning the status-board and pricing-refresh units give.
STATE_PATH = PROJECT_ROOT / "data" / "alerting" / "heartbeat.json"

#: ~2 months of daily probes. Enough to answer "has this been flapping?"
#: without the file growing without bound.
HISTORY_LIMIT = 60



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def load_state(path: Path | None = None) -> dict[str, Any]:
    """The heartbeat record, or an empty one. Never raises.

    A corrupt or unreadable record must not stop the probe: the probe is
    the thing that matters, the record is how it is remembered.
    """
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"history": [], "last_ok": None, "last_failure": None,
                "consecutive_failures": 0}
    raw.setdefault("history", [])
    raw.setdefault("last_ok", None)
    raw.setdefault("last_failure", None)
    raw.setdefault("consecutive_failures", 0)
    if not isinstance(raw["history"], list):
        raw["history"] = []
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write (tmp + os.replace). Returns False rather than raising.

    Atomic because `--status` may be reading this file while a probe
    writes it; a half-written record read as "no successes ever" would
    manufacture an alarm out of a scheduling coincidence.
    """
    path = path or STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        print(f"alert_heartbeat: could not write {path}: {exc}", file=sys.stderr)
        return False
    return True


def record(
    state: dict[str, Any],
    *,
    kind: str,
    ok: bool,
    stage: str,
    detail: str = "",
    residue: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold one probe outcome into the on-box record."""
    moment = _iso(now or _now())
    entry = {
        "ts": moment,
        "kind": kind,
        "ok": bool(ok),
        "stage": stage,
        "detail": detail,
        "residue": bool(residue),
    }
    history = list(state.get("history") or [])
    history.append(entry)
    state["history"] = history[-HISTORY_LIMIT:]
    state["updated_at"] = moment
    if ok:
        state["last_ok"] = moment
        state["consecutive_failures"] = 0
    else:
        state["last_failure"] = moment
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    return state


def failure_text(stage: str, detail: str) -> str:
    """Best-effort alert when the probe fails.

    Almost always futile — if the channel is broken this cannot get through
    either — but not always: a probe can fail at a stage the ordinary send
    path survives, and then this is the fastest warning available. It costs
    one request to try.
    """
    return (
        "🛑 FAILED: QAMC alert channel FAILED its self-test\n\n"
        f"Stage: {stage}\n"
        f"Detail: {detail or 'no detail'}\n\n"
        "Every alarm on this desk — deploy drift, pricing cache, session "
        "crash, missing stop — goes out over this channel. Until it is "
        "fixed, silence from QAMC means nothing at all.\n\n"
        "Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env and outbound "
        "access to api.telegram.org, then run "
        "`scripts/run_alert_heartbeat.sh` by hand."
    )


def build_notifier():
    """The same `TelegramNotifier` every alarm on this desk uses.

    Constructed with no arguments on purpose: it reads the environment
    exactly as `check_deploy_drift.py`, `refresh_pricing.py` and the
    shutdown/hold alerts do, so a probe failure here is a real alarm
    failure and not an artifact of a differently-built notifier.
    """
    from src.notifier import TelegramNotifier

    return TelegramNotifier()


def run_probe(now: datetime | None = None) -> tuple[int, str]:
    """Exercise the channel, record the verdict. Returns (exit_code, line)."""
    from src.notifier import ProbeResult

    notifier = build_notifier()
    result = notifier.probe()
    if not isinstance(result, ProbeResult):  # pragma: no cover - defensive
        result = ProbeResult(bool(result), "unknown", "")

    if result.stage == "rehearsal":
        # A rehearsal proves nothing either way. Recording it would put a
        # fabricated outage in the durable history — and "we did not check"
        # versus "we checked and it is broken" is precisely the distinction
        # this whole design exists to preserve.
        return 0, f"alert_heartbeat: {result.summary()} (not recorded)"

    state = record(
        load_state(),
        kind="probe",
        ok=result.ok,
        stage=result.stage,
        detail=result.detail,
        residue=result.residue,
        now=now,
    )
    save_state(state)
    # The durable record — the same table every session writes to, and the
    # one Mission Control reads. Never raises; a DB that cannot be written
    # must not stop the probe or change its verdict.
    record_durably(result, now=now)

    if result.ok:
        return 0, f"alert_heartbeat: {result.summary()}"

    message = failure_text(result.stage, result.detail)
    print(message, file=sys.stderr)
    delivered = bool(notifier.send(message))
    return 1, (
        f"alert_heartbeat: {result.summary()}; "
        f"failure alert {'delivered' if delivered else 'could NOT be delivered'}"
    )


def record_durably(result: Any, now: datetime | None = None) -> bool:
    """Write the verdict to `alert_channel_checks`. Never raises."""
    try:
        from src.alert_watchdog import record_check

        return record_check(
            ok=bool(result.ok),
            stage=str(result.stage),
            detail=str(result.detail or ""),
            residue=bool(result.residue),
            source="heartbeat_timer",
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"alert_heartbeat: could not write the durable record: {exc}",
            file=sys.stderr,
        )
        return False


def durable_status_lines() -> list[str]:
    """What the DURABLE record says — the same rows Mission Control renders.

    Separated from the on-box JSON record above so a disagreement between
    the two is visible rather than averaged away.
    """
    try:
        from src.alert_watchdog import read_health

        health = read_health()
        return [
            "durable record (SQLite alert_channel_checks — what Mission Control reads):",
            f"  status:               {health.status.upper()}",
            f"  last check:           {health.last_check_at or 'never'}",
            f"  last ok:              {health.last_ok_at or 'never'}",
            f"  consecutive failures: {health.consecutive_failures}",
            f"  stale after:          {health.stale_after_hours}h",
        ] + ([f"  note:                 {health.error}"] if health.error else [])
    except Exception as exc:  # noqa: BLE001
        return [f"durable record: UNREADABLE ({exc})"]


def run_status() -> tuple[int, str]:
    """Print the record. Sends nothing, exercises nothing."""
    state = load_state()
    lines = durable_status_lines() + [
        "",
        f"on-box record: {STATE_PATH}",
        f"  last ok:              {state.get('last_ok') or 'never'}",
        f"  last failure:         {state.get('last_failure') or 'never'}",
        f"  consecutive failures: {state.get('consecutive_failures', 0)}",
        f"  entries kept:         {len(state.get('history') or [])}",
    ]
    for entry in (state.get("history") or [])[-10:]:
        verdict = "ok " if entry.get("ok") else "FAIL"
        lines.append(
            f"  {entry.get('ts')}  {verdict}  {entry.get('kind')}/"
            f"{entry.get('stage')}  {entry.get('detail', '')}".rstrip()
        )
    return 0, "\n".join(lines)


#: Which entry point this process is — `main` sets it; the run record
#: carries it so a reader can tell the 06:15 heartbeat's reporting-only pass
#: from the every-30-minutes sweep that places stops.
_RUN_ENTRY = "alert_heartbeat"


def _attach_desk_log(project_root: Path | None = None) -> None:
    """Send this process's log records to the log the desk actually writes.

    Board item 131: the sweep's records went only to Python's last-resort
    stderr handler (warnings and errors, nothing at INFO), so the systemd
    journal of this one unit was the only place a run could be seen and
    `quant_agent.log` held nothing from it. Same file and same format as
    `main.py`; a plain appending FileHandler — this process is short-lived
    and never rotates the file, the session processes do. Warnings and
    errors still reach stderr, so the journal keeps what it showed before.
    Called only from the `__main__` block, so tests never touch a real log.
    """
    import logging

    root_dir = project_root or Path(__file__).resolve().parent.parent
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        fh = logging.FileHandler(root_dir / "quant_agent.log", mode="a")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)
    except OSError as exc:
        print(f"alert_heartbeat: could not open quant_agent.log ({exc})", file=sys.stderr)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    root.addHandler(err)


def _build_broker():
    """Read-only broker handle, built the narrow way `src/api/broker_reads.py`
    does — two strings, never the whole config object."""
    from src.api.deps import get_alpaca_credentials, get_alpaca_paper
    from src.execution.broker import AlpacaBroker

    key, secret = get_alpaca_credentials()
    return AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())


def _cash_sweep_symbol() -> str:
    """The stopless cash vehicle to skip. Falls back to the schema default
    rather than to None: an unreadable config must not turn SGOV into a
    daily false alarm."""
    try:
        from src.api.deps import get_cash_sweep_symbol

        return str(get_cash_sweep_symbol())
    except Exception:  # noqa: BLE001
        from src.config import CashSweepConfig

        return str(CashSweepConfig().symbol)


def _coverage_db_and_last_buy():
    """`(Database, last_buy callable)` or `(None, None)` if the trading DB
    cannot be opened.

    The callable is `symbol, action='BUY'|'SHORT' -> that position's own
    last opening row`. `Database` is used rather than a private read-only
    query so the executed-trade predicate has one home — a hand-rolled copy
    of that SQL here is how the repair would start reading a different row
    from the one the in-session sweep reads. The same handle is handed to
    repair so a successful replace writes the level back.

    Returning None (rather than raising, or guessing a level) means the
    coverage check stays a pure reader for this run and says so.
    """
    try:
        from src.api.deps import get_db_path
        from src.storage.db import Database

        db = Database(str(get_db_path()))
        db.initialize()
    except Exception as exc:  # noqa: BLE001
        print(
            f"coverage_watchdog: no recorded-stop lookup available ({exc}) — "
            "reporting only, placing nothing",
            file=sys.stderr,
        )
        return None, None
    last_buy = lambda symbol, action="BUY": db.get_symbol_last_buy(
        symbol, include_in_flight=True, action=action,
    )
    return db, last_buy


def run_coverage_check(now: datetime | None = None) -> str:
    """Stop-coverage watchdog for a desk that is not running sessions — see
    `src/coverage_watchdog.py`.

    Reads the broker and the session record; while the exchange calendar says
    the session is OPEN it also puts back the protective stop over any shares
    the broker is not watching, through the same
    `src.execution.stop_repair.repair_stop_coverage` a normal session uses. It
    never sells, resizes, closes or cancels anything.

    Two owner alerts, each at most once per trading day: shares still with no
    stop and no session to have re-placed one, and a placement that was
    attempted during open hours and did not land. A failed placement is never
    swallowed.

    Returns the journal line; raises only if the broker cannot be built, and
    `main` contains that.
    """
    import logging
    import uuid

    from src.coverage_watchdog import (
        SWEEP_AGENT_NAME, SWEEP_LOG_NAME, alert_text, check_coverage,
        record_sweep_run, repair_failure_text, status_line, sweep_log_line,
        sweep_summary, unreadable_stop_text,
    )

    # Board item 131: every run leaves a named line in the desk's log and
    # one row in the desk's event record, started and finished, whatever
    # happens in between.
    sweep_log = logging.getLogger("src.coverage_watchdog")
    entry = _RUN_ENTRY
    run_id = f"{SWEEP_AGENT_NAME}-{uuid.uuid4().hex[:8]}"
    sweep_log.info("%s %s (%s): started", SWEEP_LOG_NAME, run_id, entry)
    db, last_buy = _coverage_db_and_last_buy()
    try:
        status = check_coverage(
            _build_broker(), now=now, sweep_symbol=_cash_sweep_symbol(),
            last_buy=last_buy, db=db,
        )
    except Exception as exc:  # noqa: BLE001 — record it, then let main report it
        summary = sweep_summary(None, entry=entry, run_id=run_id, error=str(exc))
        sweep_log.error("%s", sweep_log_line(summary))
        record_sweep_run(db, summary)
        raise
    line = status_line(status)
    sent: list[str] = []
    if (
        status.should_alert or status.should_alert_repair_failure
        or status.should_alert_unreadable
    ):
        from src.notifier import send_owner_alert

        # Board item 172, sent FIRST. A stop the broker could not be asked
        # about is the only one of these three conditions where the desk
        # does not know what it is looking at, and it must not arrive after
        # two messages about measured gaps.
        #
        # Claim-before-send is already done inside `check_coverage`, which
        # wrote the per-symbol marker into the shared state file the same
        # way it does for a placement failure. Re-claiming here would find
        # the marker it just wrote and silence the message it was written
        # for. The live session's own reconcile reads that same marker, so
        # whichever process sees the symbol first is the one that tells him.
        if status.should_alert_unreadable:
            text = unreadable_stop_text(status.unreadable)
            print(text, file=sys.stderr)
            ok = bool(send_owner_alert(
                text, symbols=[r.symbol for r in status.unreadable],
            ))
            sent.append(
                f"unreadable-stop alert "
                f"{'delivered' if ok else 'could NOT be delivered'}"
            )
        if status.should_alert_repair_failure:
            text = repair_failure_text(status)
            print(text, file=sys.stderr)
            ok = bool(send_owner_alert(
                text, symbols=[r.symbol for r in status.repair_failures],
            ))
            sent.append(
                f"placement-failure alert "
                f"{'delivered' if ok else 'could NOT be delivered'}"
            )
        if status.should_alert:
            text = alert_text(status)
            print(text, file=sys.stderr)
            ok = bool(send_owner_alert(
                text, symbols=[g.symbol for g in status.gaps],
            ))
            sent.append(
                f"exposure alert "
                f"{'delivered' if ok else 'could NOT be delivered'}"
            )
    summary = sweep_summary(status, entry=entry, run_id=run_id, alerts=sent)
    finished = sweep_log_line(summary)
    if summary["outcome"] in (
        "repair_failed", "could_not_check", "unreadable_stops",
    ):
        # WARNING, not ERROR: the failure itself is already logged at ERROR
        # by the module that hit it, under wording `src/log_health.py`
        # already classifies; a second ERROR here would be counted twice.
        sweep_log.warning("%s", finished)
    else:
        sweep_log.info("%s", finished)
    record_sweep_run(db, summary)
    return "; ".join([line, *sent])


def run_refusal_signature_check(now: datetime | None = None) -> str:
    """Unvarying-refusal watchdog — see `src/refusal_signature.py`
    (docs/WORK.md item 59). Reads the durable per-candidate evidence rows
    and sends one owner alert when every candidate, over consecutive
    sessions with CHANGING candidates, was refused for the same single
    reason. Introduces no day count and no threshold. Silent while the
    trading timers are paused, because the streak it needs must end on the
    most recent trading day. Returns the journal line."""
    from src.refusal_signature import (
        alert_text, check_refusal_signature, status_line,
    )

    status = check_refusal_signature(now=now, broker=_build_broker())
    line = status_line(status)
    if not status.should_alert:
        return line
    from src.notifier import send_owner_alert

    text = alert_text(status)
    print(text, file=sys.stderr)
    delivered = bool(send_owner_alert(text, symbols=status.symbols))
    return f"{line}; alert {'delivered' if delivered else 'could NOT be delivered'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the operator alert channel still works.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="print the record and exit; sends nothing, exercises nothing",
    )
    parser.add_argument(
        "--coverage-only", action="store_true",
        help=(
            "run ONLY the stop-coverage check (no channel probe, no Telegram "
            "self-test). This is what the every-30-minutes coverage-sweep "
            "unit runs: it puts back a missing protective stop while the "
            "market is open and does nothing at all while it is shut."
        ),
    )
    args = parser.parse_args(argv)

    global _RUN_ENTRY
    _RUN_ENTRY = "coverage_sweep" if args.coverage_only else "alert_heartbeat"

    if args.coverage_only:
        # The probe is skipped on purpose. This entry point exists to run
        # OFTEN — the market-hours tick that actually re-places a lapsed
        # fractional DAY stop — and a channel self-test every 30 minutes
        # would be 30-odd needless Telegram round trips a day to prove
        # something the 06:15 run already proves once.
        try:
            print(run_coverage_check())
        except Exception as exc:  # noqa: BLE001
            print(f"coverage_watchdog: could NOT run ({exc})", file=sys.stderr)
            return 1
        return 0

    if args.status:
        code, line = run_status()
    else:
        code, line = run_probe()

    print(line)

    if not args.status:
        # The coverage watchdog rides on this unit because it is the one
        # thing that still runs while the trading timers are off. It must
        # never change the probe's verdict: this unit's exit code means
        # "can the desk reach the owner", nothing else.
        try:
            print(run_coverage_check())
        except Exception as exc:  # noqa: BLE001
            print(f"coverage_watchdog: could NOT run ({exc})", file=sys.stderr)
        # Same rule as the coverage watchdog above: it rides this unit
        # because this unit runs whether or not the trading timers do, and
        # it must never change the probe's own verdict or exit code.
        try:
            print(run_refusal_signature_check())
        except Exception as exc:  # noqa: BLE001
            print(f"refusal_signature: could NOT run ({exc})", file=sys.stderr)
    return code


if __name__ == "__main__":
    _attach_desk_log()
    raise SystemExit(main())
