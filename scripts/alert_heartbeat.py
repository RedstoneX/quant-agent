#!/usr/bin/env python3
"""Prove, on a schedule, that the operator alert channel still works.

THE DEFECT THIS CLOSES
----------------------
Every alarm on this desk is a Telegram message: deploy drift, a pricing
cache that did not land, a session that crashed, a stop that went missing.
Nothing ever checked that a Telegram message can still be sent. So "no
alert arrived" meant two completely different things at once —

    nothing is wrong        (the desk is healthy)
    the alarm is broken     (the desk may be on fire)

— and no evidence anywhere on the box could tell them apart. That is the
same defect class as a scheduled job that has been failing silently, one
level up: the failure detector itself failing undetected.

WHAT IT DOES
------------
Two modes, one script:

  --probe  (default)   Send a real message through the real notifier with
                       `disable_notification`, then delete it. Records the
                       result under data/alerting/heartbeat.json. Sends the
                       operator NOTHING on success. Exits non-zero on
                       failure so the systemd unit goes `failed` and is
                       visible in `systemctl --user status`.

  --digest             Send the operator ONE message. Its content is the
                       week's probe record; its arrival is the point.

WHY A PROBE AND NOT A CREDENTIAL CHECK
--------------------------------------
Checking that TELEGRAM_BOT_TOKEN is set answers a question nobody has. The
failures that actually silence this desk all pass a variable check: a token
that is present but revoked, a chat id that is present but wrong, a bot the
operator blocked, an egress rule that drops api.telegram.org. Only a send
catches those, so this sends. See `TelegramNotifier.probe`.

THE CADENCE, AND WHY
--------------------
Probe daily, at 06:15 ET, seven days a week — ahead of the first scheduled
job of the day (the 06:30 pricing refresh), so the channel is proved before
anything that might need to shout. Daily, not hourly: the alert path breaks
for structural reasons — a rotated credential, a deleted chat, a firewall
rule — none of which arrive on a minute scale, and an hourly probe would
multiply Bot API traffic 24x while turning every transient network blip
into a recorded failure.

Digest weekly, Sunday 17:00 ET — one message, on a fixed day, outside every
session window and on a day the market is closed so it can never be
mistaken for a trading message. Weekly rather than daily because a routine
"still alive" arriving every morning is a message an operator learns to
swipe away, and an alarm confirmation nobody reads is worth exactly as much
as no confirmation. One a week stays legible.

THE BOOTSTRAP PROBLEM, HONESTLY
-------------------------------
If the alert channel is what is broken, no message sent over it can say so.
That is not solvable from inside the channel, and this script does not
pretend otherwise. What it does is make the channel's own silence carry
information:

  * The weekly digest is a standing appointment. If Sunday passes with no
    heartbeat, the alert channel is dead — that is the entire contract, and
    the digest message states it in its own body so the operator relearns
    it every week. The absence of an expected message is the only signal
    that survives a broken channel.
  * Detection latency on the box is 24h, not a week: the daily probe writes
    a dated verdict to data/alerting/heartbeat.json and fails its unit, so
    `systemctl --user status` and the journal already hold the answer
    whenever anyone looks.
  * An out-of-band dead-man's switch closes the gap entirely, and is
    supported here but NOT currently configured: set
    ALERT_HEARTBEAT_HEALTHCHECK_URL in .env to a healthchecks.io-style
    check (free tier) and a stopped ping alerts from a host that is not
    this one, over a path that is not Telegram. Until that is set, the
    worst case is up to seven days between the channel dying and its
    silence being noticed.

WHAT IT COSTS
-------------
Two or three HTTPS requests a day to api.telegram.org. No LLM call, no paid
dependency, no new channel, no new credential.

USAGE
    python scripts/alert_heartbeat.py                 # probe, record, exit 0/1
    python scripts/alert_heartbeat.py --digest        # weekly operator message
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
from datetime import datetime, timedelta, timezone
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

#: The digest reports on this window.
DIGEST_WINDOW_DAYS = 7


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

    Atomic because the digest may be reading this file while a probe writes
    it; a half-written record read as "no successes ever" would manufacture
    an alarm out of a scheduling coincidence.
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
    """Fold one probe or digest outcome into the record."""
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


def _recent(state: dict[str, Any], days: int, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    out = []
    for entry in state.get("history") or []:
        try:
            when = datetime.fromisoformat(str(entry.get("ts")))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            out.append(entry)
    return out


def digest_text(state: dict[str, Any], now: datetime | None = None) -> str:
    """The one message the operator gets each week.

    It states the standing contract in its own body on purpose. The value of
    this message is not its content — it is that it arrived, and a contract
    the operator has to remember unaided is a contract that decays.
    """
    moment = now or _now()
    window = [e for e in _recent(state, DIGEST_WINDOW_DAYS, moment)
              if e.get("kind") == "probe"]
    total = len(window)
    good = sum(1 for e in window if e.get("ok"))

    if total == 0:
        record_line = (
            f"No probe ran in the last {DIGEST_WINDOW_DAYS} days. The channel "
            f"works — you are reading this — but the daily check is not "
            f"running. Check quant-agent-alert-heartbeat.timer."
        )
    elif good == total:
        record_line = f"Daily channel probes: {good}/{total} delivered."
    else:
        failed = [e for e in window if not e.get("ok")]
        worst = failed[-1]
        record_line = (
            f"Daily channel probes: {good}/{total} delivered — "
            f"{total - good} FAILED. Most recent failure {worst.get('ts')} "
            f"at stage '{worst.get('stage')}': {worst.get('detail') or 'no detail'}."
        )

    last_failure = state.get("last_failure")
    failure_line = (
        f"Last failure ever recorded: {last_failure}."
        if last_failure else
        "No failure has ever been recorded."
    )

    return (
        "🫀 QAMC alerting heartbeat — weekly\n\n"
        "You are reading this because the alert channel still works. That is "
        "the whole message.\n\n"
        f"{record_line}\n"
        f"{failure_line}\n\n"
        "The contract: this arrives every Sunday. If a Sunday goes by and it "
        "does not, assume the alert channel is dead and go and look at the "
        "box — a broken channel cannot tell you it is broken, so its silence "
        "is the only warning you get."
    )


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
    """Optional out-of-band dead-man's switch. No-op when unconfigured.

    Deliberately a SEPARATE variable from the sessions' HEALTHCHECKS_URL:
    run_if_et_window.sh already documents why sharing one check across many
    jobs pins it green and defeats the purpose.
    """
    url = os.environ.get("ALERT_HEARTBEAT_HEALTHCHECK_URL", "").strip()
    if not url:
        return
    try:
        import requests

        requests.get(f"{url}{suffix}", timeout=10)
    except Exception:  # noqa: BLE001 - a dead-man's switch must never raise
        pass


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
        # A rehearsal proves nothing either way; recording it as a failure
        # would put a fake outage in the operator's weekly digest.
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

    if result.ok:
        ping_healthcheck()
        return 0, f"alert_heartbeat: {result.summary()}"

    message = failure_text(result.stage, result.detail)
    print(message, file=sys.stderr)
    delivered = bool(notifier.send(message))
    ping_healthcheck("/fail")
    return 1, (
        f"alert_heartbeat: {result.summary()}; "
        f"failure alert {'delivered' if delivered else 'could NOT be delivered'}"
    )


def run_digest(now: datetime | None = None) -> tuple[int, str]:
    """Send the weekly message. Its delivery is itself a channel proof."""
    state = load_state()
    notifier = build_notifier()
    text = digest_text(state, now=now)

    if not notifier.enabled:
        state = record(
            state, kind="digest", ok=False, stage="credentials",
            detail="no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in this process",
            now=now,
        )
        save_state(state)
        print(text, file=sys.stderr)
        ping_healthcheck("/fail")
        return 1, (
            "alert_heartbeat: digest NOT sent — the notifier has no "
            "credentials; the message above went to the journal only"
        )

    delivered = bool(notifier.send(text))
    state = record(
        state,
        kind="digest",
        ok=delivered,
        stage="delivered" if delivered else "send",
        detail="" if delivered else "TelegramNotifier.send() returned False",
        now=now,
    )
    save_state(state)
    ping_healthcheck("" if delivered else "/fail")
    if delivered:
        return 0, "alert_heartbeat: weekly digest delivered"
    return 1, (
        "alert_heartbeat: weekly digest was NOT delivered — the operator "
        "has no working alert channel"
    )


def run_status() -> tuple[int, str]:
    """Print the record. Sends nothing, exercises nothing."""
    state = load_state()
    lines = [
        f"alert_heartbeat record: {STATE_PATH}",
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--digest", action="store_true",
        help="send the weekly operator message instead of a silent probe",
    )
    mode.add_argument(
        "--status", action="store_true",
        help="print the local record and exit; sends nothing",
    )
    args = parser.parse_args(argv)

    if args.status:
        code, line = run_status()
    elif args.digest:
        code, line = run_digest()
    else:
        code, line = run_probe()

    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
