"""The sessions watch the alarm they depend on.

THE DEFECT THIS CLOSES
----------------------
Every alarm on this desk is a Telegram message. Nothing checked that a
Telegram message could still be sent, so "no alert arrived" meant both
"nothing is wrong" and "the alarm is broken", and nothing on the box could
separate them.

The previous attempt at an answer was a weekly "still alive" message whose
ABSENCE was the signal. That is not monitoring: a routine confirmation is a
message an operator learns to swipe away, and a channel that can be dead for
seven days before anyone notices is dead for seven days.

TWO FAILURE MODES, TESTED SEPARATELY
------------------------------------
  (A) The channel is broken while the box is alive — revoked token, wrong
      chat id, blocked bot, egress rule. Fully detectable locally, and this
      file is almost entirely about proving that it IS detected, recorded,
      and surfaced.
  (B) The box itself is dead. Nothing running on the box can report this.
      There is no test here for (B) because there is nothing to test: it is
      uncovered, deliberately and explicitly. See README.

THE LOAD-BEARING TESTS ARE THE NEGATIVE ONES. A watchdog that goes green on
a healthy path proves nothing on its own — the failure that matters is a
BROKEN path that still reports healthy. Those are grouped in section 2.

Nothing here touches the network, the production box, or a real Telegram
chat: every transport is mocked at `src.notifier.requests.post`. No live
message is ever sent, and no external monitoring service is contacted.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import alert_watchdog
from src.notifier import ProbeResult, TelegramNotifier

WATCHDOG_SRC = Path(__file__).resolve().parent.parent / "src" / "alert_watchdog.py"


def _response(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {"ok": True}
    return resp


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An isolated database, wired in as the watchdog's default."""
    path = tmp_path / "alerting" / "quant_agent.db"
    monkeypatch.setattr(alert_watchdog, "DB_PATH", path)
    return path


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:FAKE-TOKEN-FOR-TESTS")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)


def _healthy_transport():
    """sendMessage accepted, deleteMessage accepted."""
    return [
        _response(200, {"ok": True, "result": {"message_id": 1}}),
        _response(200, {"ok": True, "result": True}),
    ]


# ===========================================================================
# 1. A healthy path is recorded as healthy — and stays silent
# ===========================================================================

def test_a_verified_channel_is_recorded_ok_and_reads_ok(db, telegram_env):
    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport()
        result = alert_watchdog.verify_alert_channel(source="morning")

    assert result.ok is True
    health = alert_watchdog.read_health()
    assert health.status == "ok"
    assert health.consecutive_failures == 0
    assert health.last_ok_at == health.last_check_at
    assert health.degraded is False


def test_a_working_channel_says_nothing_to_the_operator(db, telegram_env):
    """The entire noise budget for a healthy alarm is zero messages. A
    routine 'still working' is the thing the operator learns to ignore, and
    then he ignores the one that matters."""
    before = alert_watchdog.read_health()
    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport()
        result = alert_watchdog.verify_alert_channel(source="morning")

    assert alert_watchdog.session_note(before, result) is None
    assert alert_watchdog.annotate_session_message(
        None, mode="intra_check", before=before, result=result,
    ) is None
    assert alert_watchdog.annotate_session_message(
        "🟢 morning done", mode="morning", before=before, result=result,
    ) == "🟢 morning done"


def test_the_source_of_every_check_is_kept(db, telegram_env):
    """Which session proved it matters when the sessions themselves start
    failing: 'only the daily timer has checked for two days' is a different
    fault from 'the channel is broken'."""
    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport()
        alert_watchdog.verify_alert_channel(source="midday")

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            f"SELECT source, ok FROM {alert_watchdog.TABLE}"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("midday", 1)]


# ===========================================================================
# 2. THE LOAD-BEARING SECTION — a broken path is caught, recorded, surfaced
# ===========================================================================
#
# If a broken alert path can still report healthy, everything else in this
# design is decoration. Each case below is a real way this desk has been or
# could be silenced, and each must produce: a recorded failure, a `broken`
# verdict, and an operator-visible line — including from a session whose
# noise policy would normally keep it completely quiet.

BROKEN_TRANSPORTS = {
    # The egress case: a firewall rule, DNS, TLS, a dead proxy. The POST
    # never completes at all.
    "transport_raises": (
        ConnectionError("failed to reach api.telegram.org"),
        "transport",
    ),
    # The revoked-token case. Telegram answers, and refuses.
    "revoked_token_401": (
        [_response(401, {"ok": False, "description": "Unauthorized"})],
        "api",
    ),
    # The wrong/deleted chat case — the credential is valid, the
    # destination is not.
    "wrong_chat_400": (
        [_response(400, {"ok": False, "description": "Bad Request: chat not found"})],
        "api",
    ),
    # The nastiest one: HTTP 200 with ok:false. Anything that checked only
    # the status code would call this a success.
    "http_200_but_ok_false": (
        [_response(200, {"ok": False, "description": "Forbidden: bot was blocked by the user"})],
        "api",
    ),
}


@pytest.fixture(params=sorted(BROKEN_TRANSPORTS))
def broken_channel(request, telegram_env):
    """Patch `requests.post` so the alert path is genuinely broken."""
    behaviour, expected_stage = BROKEN_TRANSPORTS[request.param]
    with patch("src.notifier.requests.post") as post:
        if isinstance(behaviour, BaseException):
            post.side_effect = behaviour
        else:
            post.side_effect = behaviour * 8
        yield SimpleNamespace(
            name=request.param, post=post, expected_stage=expected_stage,
        )


def test_a_broken_channel_is_recorded_as_broken(db, broken_channel):
    result = alert_watchdog.verify_alert_channel(source="morning")

    assert result.ok is False, f"{broken_channel.name} reported a working channel"
    assert result.stage == broken_channel.expected_stage

    health = alert_watchdog.read_health()
    assert health.status == "broken", (
        f"{broken_channel.name}: the desk cannot reach the operator and the "
        f"record says {health.status!r}"
    )
    assert health.degraded is True
    assert health.consecutive_failures == 1
    assert health.last_ok_at is None


def test_a_broken_channel_forces_a_message_out_of_a_silent_session(
    db, broken_channel,
):
    """intra_check's ~14 OK ticks a day are deliberately silent. A broken
    alarm that is only reported by sessions that happened to be chatty is an
    alarm that can stay quiet all day."""
    before = alert_watchdog.read_health()
    result = alert_watchdog.verify_alert_channel(source="intra_check")

    message = alert_watchdog.annotate_session_message(
        None, mode="intra_check", before=before, result=result,
    )
    assert message is not None, (
        f"{broken_channel.name}: a silent session swallowed a broken alarm"
    )
    assert "ALERT CHANNEL FAILED" in message
    assert broken_channel.expected_stage in message


def test_a_broken_channel_makes_mission_control_degraded(db, broken_channel):
    """The one place the operator can see this when Telegram itself is the
    thing that is dead."""
    alert_watchdog.verify_alert_channel(source="morning")
    payload = alert_watchdog.read_health().to_dict()

    assert payload["status"] == "broken"
    # Exactly the predicate `/health` uses to flip the whole board.
    assert payload["status"] in ("broken", "stale")


def test_consecutive_failures_accumulate(db, broken_channel):
    for _ in range(3):
        alert_watchdog.verify_alert_channel(source="intra_check")
    assert alert_watchdog.read_health().consecutive_failures == 3


def test_a_credential_less_session_is_recorded_as_broken(db, monkeypatch):
    """The exact defect PR #175 found on the live box: a unit that never
    sourced `.env`, so the notifier disabled itself and every alarm it
    raised went to the journal and nowhere else. A detector that passes by
    being misconfigured is worse than none."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_DISABLED"):
        monkeypatch.delenv(name, raising=False)

    with patch("src.notifier.requests.post") as post:
        result = alert_watchdog.verify_alert_channel(source="morning")

    assert post.call_count == 0
    assert result.ok is False
    assert result.stage == "credentials"
    assert alert_watchdog.read_health().status == "broken"


def test_a_muted_channel_is_reported_broken_not_healthy(db, telegram_env, monkeypatch):
    """TELEGRAM_DISABLED is an operator convenience, but while it is set the
    desk genuinely cannot raise an alarm. Reporting that as healthy would be
    the same lie in a friendlier costume."""
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")

    with patch("src.notifier.requests.post") as post:
        result = alert_watchdog.verify_alert_channel(source="morning")

    assert post.call_count == 0
    assert result.stage == "credentials"
    assert alert_watchdog.read_health().status == "broken"


# ===========================================================================
# 3. Recovery — the one message that reaches him without him looking
# ===========================================================================

def test_recovery_is_reported_over_the_channel_that_just_came_back(
    db, telegram_env,
):
    """While the channel is down nothing can reach him; the record and
    Mission Control are all there is. The moment it works again, the first
    session says how long the desk was unable to shout."""
    with patch("src.notifier.requests.post") as post:
        post.side_effect = ConnectionError("egress blocked")
        alert_watchdog.verify_alert_channel(source="morning")
        alert_watchdog.verify_alert_channel(source="intra_check")

    before = alert_watchdog.read_health()
    assert before.status == "broken"
    assert before.consecutive_failures == 2

    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport()
        result = alert_watchdog.verify_alert_channel(source="midday")

    message = alert_watchdog.annotate_session_message(
        None, mode="midday", before=before, result=result,
    )
    assert message is not None
    assert "RECOVERED" in message
    assert "2 consecutive check(s)" in message
    assert alert_watchdog.read_health().status == "ok"


# ===========================================================================
# 4. Staleness — "nothing is known" is not "everything is fine"
# ===========================================================================

def test_a_stale_record_is_not_reported_as_healthy(db, telegram_env):
    """The checks themselves stopping is its own failure mode: sessions not
    firing, the daily timer disabled, the box asleep. Nothing is known to be
    wrong and nothing is known to be right, and the second half is the part
    that must not be rounded up."""
    old = datetime.now(timezone.utc) - timedelta(
        hours=alert_watchdog.STALE_AFTER_HOURS + 2,
    )
    alert_watchdog.record_check(
        ok=True, stage="delivered", source="morning", now=old,
    )

    health = alert_watchdog.read_health()
    assert health.status == "stale"
    assert health.degraded is True
    assert health.age_hours is not None
    assert health.age_hours > alert_watchdog.STALE_AFTER_HOURS


def test_a_check_inside_the_window_is_not_stale(db):
    recent = datetime.now(timezone.utc) - timedelta(
        hours=alert_watchdog.STALE_AFTER_HOURS - 2,
    )
    alert_watchdog.record_check(
        ok=True, stage="delivered", source="heartbeat_timer", now=recent,
    )
    assert alert_watchdog.read_health().status == "ok"


def test_the_staleness_window_survives_a_weekend_of_no_sessions(db):
    """Sessions run Mon-Fri only. If the threshold were shorter than the
    daily backstop's period the board would go red every Saturday, and a
    board that is red every weekend is a board nobody reads."""
    assert alert_watchdog.STALE_AFTER_HOURS > 24.0


def test_a_broken_check_stays_broken_even_when_it_is_also_stale(db):
    """Precedence matters: 'we have not checked lately' must never mask
    'the last thing we know is that it was broken'."""
    old = datetime.now(timezone.utc) - timedelta(days=5)
    alert_watchdog.record_check(ok=False, stage="api", detail="Unauthorized", now=old)
    assert alert_watchdog.read_health().status == "broken"


# ===========================================================================
# 5. Unknown — an unconfigured/unmeasured channel never reads as healthy
# ===========================================================================

def test_a_database_with_no_checks_reports_unknown_not_ok(db):
    health = alert_watchdog.read_health()
    assert health.status == "unknown"
    assert health.status != "ok"
    assert health.last_ok_at is None
    assert health.error


def test_a_database_that_predates_the_table_reports_unknown(db):
    """An upgrade lands on a database with no `alert_channel_checks`. That
    must degrade to 'nothing known', not to a crash and not to 'fine'."""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    health = alert_watchdog.read_health()
    assert health.status == "unknown"
    assert "no check history" in (health.error or "")


def test_a_missing_database_reports_unknown_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(alert_watchdog, "DB_PATH", tmp_path / "nope" / "gone.db")
    health = alert_watchdog.read_health()
    assert health.status == "unknown"
    assert health.degraded is False  # a missing measurement, not a detected fault


# ===========================================================================
# 6. The watchdog must never become the fault
# ===========================================================================

def test_an_unwritable_database_does_not_break_the_check(tmp_path, monkeypatch, telegram_env):
    """A watchdog that can take a session down is worse than no watchdog.
    The probe verdict still stands; only its persistence is lost."""
    monkeypatch.setattr(alert_watchdog, "DB_PATH", tmp_path)  # a directory

    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport()
        result = alert_watchdog.verify_alert_channel(source="morning")

    assert result.ok is True
    assert alert_watchdog.record_check(ok=True, stage="delivered") is False


def test_a_probe_that_raises_is_not_recorded_as_an_outage(db):
    """'We could not check' and 'we checked and it is broken' are the two
    things this design exists to keep apart. Collapsing them manufactures
    incidents that never happened."""
    notifier = MagicMock()
    notifier.probe.side_effect = RuntimeError("probe itself is broken")

    assert alert_watchdog.verify_alert_channel(notifier, source="morning") is None
    assert alert_watchdog.read_health().status == "unknown"


def test_a_mocked_notifier_never_writes_a_fake_incident(db):
    """Half the suite hands `main()` a MagicMock notifier. `probe()` then
    returns a MagicMock, which is truthy — recording it would put fabricated
    outages in a real database on every test run."""
    notifier = MagicMock()

    assert alert_watchdog.verify_alert_channel(notifier, source="morning") is None
    assert alert_watchdog.read_health().status == "unknown"


def test_a_rehearsal_is_never_recorded(db, telegram_env, monkeypatch):
    """A rehearsal replays a real session offline and must not transmit. Its
    non-send is not an outage."""
    import src.notifier as notifier_module

    monkeypatch.setattr(notifier_module, "_REHEARSAL_MODE", True)

    with patch("src.notifier.requests.post") as post:
        result = alert_watchdog.verify_alert_channel(source="evening")

    assert post.call_count == 0
    assert result.stage == "rehearsal"
    assert alert_watchdog.read_health().status == "unknown"
    assert alert_watchdog.session_note(None, result) is None


def test_a_junk_database_path_is_refused_rather_than_creating_one(db):
    """`main.py` passes `config.storage.db_path`, and a test that mocks
    `load_config` passes a MagicMock. `str(MagicMock())` is a valid
    filename, so without the type check this silently creates a junk
    database in whatever directory the session happened to start in."""
    assert alert_watchdog.record_check(ok=True, stage="x", db_path=MagicMock()) is False
    assert alert_watchdog.record_check(ok=True, stage="x", db_path="") is False
    assert alert_watchdog.read_health(MagicMock()).status == "unknown"


def test_the_record_does_not_grow_without_bound(db, monkeypatch):
    monkeypatch.setattr(alert_watchdog, "ROW_LIMIT", 5)
    for _ in range(12):
        alert_watchdog.record_check(ok=True, stage="delivered", source="intra_check")

    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {alert_watchdog.TABLE}"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 5


def test_the_health_read_is_read_only_by_construction():
    """Same structural guarantee `src/api/db_reads.py` relies on: the API
    reads this table on every /health poll, and a future bug that added a
    write must fail at the database layer rather than touch trading state."""
    text = WATCHDOG_SRC.read_text()
    assert "mode=ro" in text
    assert "uri=True" in text


def test_the_detail_never_carries_the_bot_token(db, telegram_env):
    """The detail lands in the database, the journal, and Mission Control.
    A revoked token is the likeliest failure — i.e. the token would leak
    exactly when the operator is most likely to share the output."""
    with patch("src.notifier.requests.post") as post:
        post.side_effect = ConnectionError(
            "failed to reach https://api.telegram.org/bot111:FAKE-TOKEN-FOR-TESTS/sendMessage"
        )
        alert_watchdog.verify_alert_channel(source="morning")

    detail = alert_watchdog.read_health().last_detail or ""
    assert "FAKE-TOKEN-FOR-TESTS" not in detail
    assert "<redacted>" in detail


# ===========================================================================
# 7. The sessions really do run it — end to end through main()
# ===========================================================================

def _fake_config(db_path: Path):
    return SimpleNamespace(
        storage=SimpleNamespace(db_path=str(db_path)),
        trading=SimpleNamespace(universe=["SPY"]),
        alpaca=SimpleNamespace(paper=True),
        notifications=SimpleNamespace(mission_control_url=""),
    )


def _run_session(monkeypatch, tmp_path, mode="intra_check", result=None):
    """Drive `main.main()` with everything but the alert path mocked."""
    import main as main_mod

    db_path = tmp_path / "session" / "quant_agent.db"
    silent_ok = {
        "status": "ok",
        "run_id": "intra-1",
        "intraday_scan": {"status": "intraday_scan_disabled", "run_id": "intra-1"},
    }
    pipeline = MagicMock()
    pipeline.run_intra_check.return_value = result or silent_ok

    monkeypatch.setattr(main_mod, "load_config", lambda _p: _fake_config(db_path))
    monkeypatch.setattr(main_mod, "refresh_pricing", lambda: None)
    monkeypatch.setattr(main_mod, "TradingPipeline", lambda _c: pipeline)
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", mode])
    main_mod.main()
    return db_path


def test_a_real_session_verifies_and_records_the_alert_path(
    tmp_path, monkeypatch, telegram_env,
):
    """The whole design in one test: the thing that depends on the alarm is
    the thing that tests it, as part of its own ordinary run."""
    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport() + [_response()]
        db_path = _run_session(monkeypatch, tmp_path)

    health = alert_watchdog.read_health(db_path)
    assert health.status == "ok"
    conn = sqlite3.connect(db_path)
    try:
        sources = [
            r[0] for r in conn.execute(
                f"SELECT source FROM {alert_watchdog.TABLE}"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert sources == ["intra_check"]


def test_a_real_session_on_a_broken_channel_records_it_and_shouts(
    tmp_path, monkeypatch, telegram_env,
):
    """End to end, the load-bearing case: a session whose noise policy is
    silent, on a channel that is genuinely refusing sends, must still leave
    a durable BROKEN record and still attempt to say so."""
    with patch("src.notifier.requests.post") as post:
        post.return_value = _response(401, {"ok": False, "description": "Unauthorized"})
        db_path = _run_session(monkeypatch, tmp_path)
        sent = [
            call.kwargs.get("json", {}).get("text", "")
            for call in post.call_args_list
        ]

    health = alert_watchdog.read_health(db_path)
    assert health.status == "broken"
    assert health.last_stage == "api"
    assert "Unauthorized" in (health.last_detail or "")
    assert any("ALERT CHANNEL FAILED" in text for text in sent), (
        "the session did not even try to report the broken channel"
    )


def test_the_watchdog_never_replaces_the_sessions_own_exception(
    tmp_path, monkeypatch, telegram_env,
):
    """It runs inside `finally`. A raise there would hide the real fault
    behind a watchdog bug — the failure mode that makes a watchdog a
    liability instead of an asset."""
    import main as main_mod

    boom = RuntimeError("the actual session failure")
    pipeline = MagicMock()
    pipeline.run_intra_check.side_effect = boom

    monkeypatch.setattr(
        main_mod, "load_config", lambda _p: _fake_config(tmp_path / "x.db"),
    )
    monkeypatch.setattr(main_mod, "refresh_pricing", lambda: None)
    monkeypatch.setattr(main_mod, "TradingPipeline", lambda _c: pipeline)
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "intra_check"])
    monkeypatch.setattr(
        alert_watchdog, "verify_alert_channel",
        MagicMock(side_effect=RuntimeError("watchdog is itself broken")),
    )

    with patch("src.notifier.requests.post"):
        with pytest.raises(RuntimeError) as exc:
            main_mod.main()

    assert exc.value is boom, "the watchdog masked the session's own failure"


# ===========================================================================
# 8. Mission Control renders it — including the states that are NOT faults
# ===========================================================================

def test_the_health_endpoint_reports_a_broken_channel_as_degraded(
    db, telegram_env, monkeypatch,
):
    import src.api.routes_live as routes_live

    with patch("src.notifier.requests.post") as post:
        post.side_effect = ConnectionError("egress blocked")
        alert_watchdog.verify_alert_channel(source="morning")

    monkeypatch.setattr(
        routes_live, "check_broker_reachable", lambda: True,
    )
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)
    monkeypatch.setattr(
        "src.api.db_reads.get_alert_channel_health",
        lambda: alert_watchdog.read_health().to_dict(),
    )

    response = routes_live.get_health()
    assert response.alert_channel is not None
    assert response.alert_channel["status"] == "broken"
    assert response.status == "degraded", (
        "a desk that cannot raise an alarm hides every other fault on this "
        "board, so it must not render green"
    )


def test_the_health_endpoint_never_omits_the_alert_channel(monkeypatch):
    """A missing field renders as nothing, and nothing reads as fine."""
    import src.api.routes_live as routes_live

    monkeypatch.setattr(routes_live, "check_broker_reachable", lambda: True)
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)
    monkeypatch.setattr(
        "src.api.db_reads.get_alert_channel_health",
        MagicMock(side_effect=RuntimeError("db gone")),
    )

    response = routes_live.get_health()
    assert response.alert_channel is not None
    assert response.alert_channel["status"] == "unknown"


def test_the_health_endpoint_does_not_flip_red_on_a_fresh_database(
    db, monkeypatch,
):
    """`unknown` is a missing measurement, not a detected fault. A board
    that is red on every fresh deploy teaches the operator to ignore red —
    which is the same failure as not reporting at all, one step removed. It
    is still stated explicitly in the payload."""
    import src.api.routes_live as routes_live

    monkeypatch.setattr(routes_live, "check_broker_reachable", lambda: True)
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)
    monkeypatch.setattr(
        "src.api.db_reads.get_alert_channel_health",
        lambda: alert_watchdog.read_health().to_dict(),
    )

    response = routes_live.get_health()
    assert response.alert_channel["status"] == "unknown"
    assert response.alert_channel["error"]


def test_the_db_reads_helper_never_raises_without_config(monkeypatch):
    from src.api import db_reads

    monkeypatch.setattr(
        db_reads, "get_db_path", MagicMock(side_effect=RuntimeError("no config")),
    )
    payload = db_reads.get_alert_channel_health()
    assert payload["status"] == "unknown"


# ===========================================================================
# 9. The probe is reused, not reimplemented
# ===========================================================================

def test_the_watchdog_uses_the_notifiers_own_probe(db, telegram_env):
    """A second implementation of 'is the channel alive' is a self-test that
    can pass while the path it stands in for is broken. The message shape,
    the parse mode, the chat id and the transport all have to be the real
    ones."""
    notifier = TelegramNotifier()
    with patch.object(
        notifier, "probe", return_value=ProbeResult(True, "delivered"),
    ) as probe:
        alert_watchdog.verify_alert_channel(notifier, source="close")
    probe.assert_called_once_with()


# ===========================================================================
# 10. THE WATCHDOG MUST NEVER COST A TRADING DAY
# ===========================================================================
#
# This runs inside every session, on the trading path, against a third-party
# endpoint the desk does not control. The failure that would make the whole
# feature a net negative is not a missed detection — it is a session that
# ran late or died because api.telegram.org went slow.
#
# `TelegramNotifier.probe` never raises on transport (it catches and returns
# ProbeResult(ok=False, stage="transport")) and every request it makes is
# bounded by `HTTP_TIMEOUT_S`. These tests hold both properties down.

def test_every_probe_request_is_bounded_by_a_timeout(telegram_env):
    """The structural guarantee: no socket the watchdog opens can hang.

    Without a timeout `requests` waits forever, and a hung probe inside
    `main.py`'s finally block would wedge a trading session until systemd's
    own kill timer — which is minutes, not seconds.
    """
    assert TelegramNotifier.HTTP_TIMEOUT_S > 0
    assert TelegramNotifier.HTTP_TIMEOUT_S <= 15, (
        f"HTTP_TIMEOUT_S={TelegramNotifier.HTTP_TIMEOUT_S}s is too long to sit "
        "on the trading path"
    )

    with patch("src.notifier.requests.post") as post:
        post.side_effect = _healthy_transport()
        TelegramNotifier().probe()

    assert post.call_count == 2, "expected exactly sendMessage + deleteMessage"
    for call in post.call_args_list:
        timeout = call.kwargs.get("timeout")
        assert timeout is not None, "a probe request went out with no timeout"
        assert 0 < timeout <= 15, timeout


def test_a_hanging_telegram_endpoint_cannot_stall_or_fail_a_session(
    tmp_path, monkeypatch, telegram_env,
):
    """The load-bearing safety test: Telegram goes dark and the session is
    unaffected except for being told the alarm is down.

    Stands in for an endpoint that accepts the connection and never answers.
    `requests` gives up at its timeout and raises; the probe catches it,
    calls the channel broken, and the session finishes normally. A session
    that raised, hung, or exited non-zero here would mean the watchdog can
    cost a trading day, which is a worse defect than the one it fixes.
    """
    import time

    import requests as requests_mod

    calls: list[float] = []

    def hangs_then_times_out(*args, **kwargs):
        # A real hang ends in requests raising at `timeout`; the sleep keeps
        # the test honest about elapsed time without waiting 5s per call.
        assert kwargs.get("timeout"), "a request went out unbounded"
        calls.append(time.monotonic())
        time.sleep(0.05)
        raise requests_mod.exceptions.ReadTimeout("simulated hang")

    started = time.monotonic()
    with patch("src.notifier.requests.post", side_effect=hangs_then_times_out):
        # No pytest.raises: the session must complete, not survive an error.
        db_path = _run_session(monkeypatch, tmp_path, mode="intra_check")
    elapsed = time.monotonic() - started

    assert calls, "the watchdog never even tried the channel"
    # Bounded work, not an unbounded retry loop against a dead endpoint.
    assert len(calls) <= 4, f"{len(calls)} requests against a hanging endpoint"
    assert elapsed < 5, f"the session stalled for {elapsed:.1f}s on Telegram"

    # And the outage was still detected, recorded and attributed correctly.
    health = alert_watchdog.read_health(db_path)
    assert health.status == "broken"
    assert health.last_stage == "transport"


def test_a_slow_endpoint_delays_a_session_by_at_most_its_timeout(telegram_env):
    """Worst case is bounded arithmetic, not a hope.

    A probe is two sequential requests, each capped at HTTP_TIMEOUT_S, so
    the most a totally unresponsive Telegram can add to a session is
    2 x HTTP_TIMEOUT_S. At 5s that is 10s on a session budgeted in minutes.
    """
    worst_case = 2 * TelegramNotifier.HTTP_TIMEOUT_S
    assert worst_case <= 30, (
        f"worst-case watchdog delay is {worst_case}s — too much to add to a "
        "session on the trading path"
    )


def test_a_watchdog_that_hangs_cannot_block_the_sessions_own_alert(
    tmp_path, monkeypatch, telegram_env,
):
    """Ordering: the session's own message still goes out after a dead probe.

    The watchdog runs before the session pushes its result. If a failed or
    slow probe could swallow that push, a bad watchdog would silence the
    reporting it was added to protect.
    """
    import requests as requests_mod

    sent: list[str] = []
    state = {"probe_calls": 0}

    def transport(*args, **kwargs):
        # Call 1 is the probe's sendMessage — it hangs, so the probe never
        # reaches its deleteMessage. Everything after is the session's own
        # push, which must still get through.
        state["probe_calls"] += 1
        if state["probe_calls"] == 1:
            raise requests_mod.exceptions.ReadTimeout("probe hangs")
        sent.append(kwargs.get("json", {}).get("text", ""))
        return _response(200, {"ok": True, "result": {"message_id": 7}})

    with patch("src.notifier.requests.post", side_effect=transport):
        db_path = _run_session(monkeypatch, tmp_path, mode="intra_check")

    assert alert_watchdog.read_health(db_path).status == "broken"
    assert sent, "the session's own message never left after a hanging probe"
    assert any("ALERT CHANNEL FAILED" in text for text in sent)
