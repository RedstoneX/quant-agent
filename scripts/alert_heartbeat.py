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
        "🔴 QAMC alert channel FAILED its self-test\n\n"
        f"Stage: {stage}\n"
        f"Detail: {detail or 'no detail'}\n\n"
        "Every alarm on this desk — deploy drift, pricing cache, session "
        "crash, missing stop — goes out over this channel. Until it is "
        "fixed, silence from QAMC means nothing at all.\n\n"
        "Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env and outbound "
        "access to api.telegram.org, then run "
        "`scripts/run_alert_heartbeat.sh` by hand."
    )


def ping_healthcheck(suffix: str = "") -> None:
    """DORMANT out-of-band hook. Unconfigured, and a no-op until it is not.

    NOT the primary mechanism, and deliberately not enabled. The alert
    channel breaking while the box runs (`src/alert_watchdog.py` case A) is
    covered locally, by the sessions, with no outside dependency. This hook
    only addresses case B — the box itself being dead — which nothing
    running on the box can report, and which is currently UNCOVERED.

    Turning it on means accepting a dependency on a third-party service.
    That is the owner's call, not this script's default, so it stays off:
    with `ALERT_HEARTBEAT_HEALTHCHECK_URL` unset this function does nothing
    at all and nothing on the box changes.

    Deliberately a SEPARATE variable from the sessions' HEALTHCHECKS_URL:
    run_if_et_window.sh already documents why sharing one check across many
    jobs pins it green and defeats the purpose.
    """
    url = os.environ.get("ALERT_HEARTBEAT_HEALTHCHECK_URL", "").strip()
    if not url:
        return
    try:
        import requests

        # Bounded, and every failure swallowed: a monitoring endpoint that
        # hangs or 500s must never stall or fail the job it is monitoring.
        requests.get(f"{url}{suffix}", timeout=10)
    except Exception:  # noqa: BLE001 - a dead-man's switch must never raise
        pass


def deadman_state() -> str:
    """How the dormant hook is reported wherever health is printed.

    Never "fine" by omission: an unconfigured switch that renders as blank
    reads as a working one, which is the exact failure this whole area
    exists to remove.
    """
    if os.environ.get("ALERT_HEARTBEAT_HEALTHCHECK_URL", "").strip():
        return "configured (out-of-band ping enabled)"
    return (
        "NOT CONFIGURED — nothing outside this box is watching; if the box "
        "itself dies, nothing reports it"
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
        ping_healthcheck()
        return 0, f"alert_heartbeat: {result.summary()}; out-of-band switch: {deadman_state()}"

    message = failure_text(result.stage, result.detail)
    print(message, file=sys.stderr)
    delivered = bool(notifier.send(message))
    ping_healthcheck("/fail")
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
        f"out-of-band switch:     {deadman_state()}",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the operator alert channel still works.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="print the record and exit; sends nothing, exercises nothing",
    )
    args = parser.parse_args(argv)

    if args.status:
        code, line = run_status()
    else:
        code, line = run_probe()

    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
