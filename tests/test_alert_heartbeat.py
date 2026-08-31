"""The alert channel, and the proof that it still works.

TWO DEFECTS, ONE FILE.

(a) THE DRIFT ALARM COULD NOT SPEAK. `quant-agent-drift-check.service`
    invoked the venv Python straight on `scripts/check_deploy_drift.py`
    with no `EnvironmentFile` and no `.env` sourcing, unlike the session
    units which go through an env-sourcing wrapper. Verified on the live
    box on 2026-08-31: the qamc systemd user environment contains zero
    `TELEGRAM_*` variables, and `.env` — which does carry them — was never
    read by that unit. `TelegramNotifier` disables itself when the
    credentials are absent, so a drift alert would have been printed to
    the journal and delivered to nobody. Every run since the unit was
    installed had reported "in sync", so the one path that mattered had
    never been exercised. This is the alarm whose entire job is to say
    that a merge never reached production.

(b) SILENCE MEANT TWO THINGS. Nothing ever checked that a Telegram message
    can still be sent, so "no alert arrived" meant both "nothing is wrong"
    and "the alarm is broken", with no evidence anywhere able to separate
    them. A credential that is present but revoked, a chat id that is
    wrong, an egress rule that drops api.telegram.org — every one of those
    passes a variable check and fails a send.

Nothing here touches the network, the production box, or a real Telegram
chat. Every transport is mocked at `src.notifier.requests.post`; no live
message is ever sent.
"""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = PROJECT_ROOT / "scripts" / "systemd"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

DRIFT_SERVICE = SYSTEMD_DIR / "quant-agent-drift-check.service"
DRIFT_WRAPPER = SCRIPTS_DIR / "run_drift_check.sh"
HEARTBEAT_SERVICE = SYSTEMD_DIR / "quant-agent-alert-heartbeat.service"
HEARTBEAT_TIMER = SYSTEMD_DIR / "quant-agent-alert-heartbeat.timer"
HEARTBEAT_WRAPPER = SCRIPTS_DIR / "run_alert_heartbeat.sh"

TELEGRAM_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_DISABLED")


def _parse_unit(path: Path) -> dict[str, list[str]]:
    """`SECTION.Key -> [values]`. Not configparser: systemd allows a key to
    repeat and configparser would silently keep only the last."""
    parsed: dict[str, list[str]] = {}
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed.setdefault(f"{section}.{key.strip()}", []).append(value.strip())
    return parsed


@pytest.fixture
def no_telegram_env(monkeypatch):
    """The environment a systemd unit gets on the box: no TELEGRAM_* at all."""
    for name in TELEGRAM_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def wrapper_env(monkeypatch):
    """The environment after the wrapper has sourced `.env`."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:FAKE-TOKEN-FOR-TESTS")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)


def _response(status: int = 200, payload: dict | None = None) -> MagicMock:
    """A stand-in for a `requests` Response at the transport boundary."""
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {
        "ok": True, "result": {"message_id": 4242},
    }
    if status >= 400:
        response.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return response


# ===========================================================================
# 1. The drift alarm could not send — and now can
# ===========================================================================

def _behind_report():
    from scripts.check_deploy_drift import DriftReport

    return DriftReport(
        deployed_path="/home/qamc/quant-agent",
        head_sha="a" * 40,
        remote_sha="b" * 40,
        fetch_ok=True,
        behind_count=2,
        missing_commits=[("a1" * 20, "fix the thing"), ("b2" * 20, "fix the other")],
    )


def test_drift_alert_reaches_nobody_without_the_wrappers_environment(
    no_telegram_env, monkeypatch, capsys,
):
    """BEFORE: the unit ran Python directly, so the process had no
    credentials and the notifier disabled itself. The alert went to the
    journal and stopped there."""
    import scripts.check_deploy_drift as drift
    from src.notifier import TelegramNotifier

    assert TelegramNotifier().enabled is False, (
        "with no TELEGRAM_* in the environment the notifier must disable "
        "itself — this is what made the drift alarm mute"
    )

    monkeypatch.setattr(drift, "build_report", lambda *a, **k: _behind_report())

    with patch("src.notifier.requests.post") as mock_post:
        exit_code = drift.main(["--no-fetch"])

    assert exit_code == 1, "drift is still a finding"
    assert mock_post.call_count == 0, (
        "nothing was transmitted: the alarm that exists to report an "
        "undeployed merge reached nobody"
    )
    assert "Telegram not configured" in capsys.readouterr().err


def test_drift_alert_transmits_under_the_wrappers_environment(
    wrapper_env, monkeypatch,
):
    """AFTER: the wrapper sources `.env`, the notifier enables itself, and
    the same code path puts the alert on the wire. Transport mocked at the
    boundary — no live message is sent."""
    import scripts.check_deploy_drift as drift
    from src.notifier import TelegramNotifier

    assert TelegramNotifier().enabled is True

    monkeypatch.setattr(drift, "build_report", lambda *a, **k: _behind_report())

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response()
        exit_code = drift.main(["--no-fetch"])

    assert exit_code == 1
    assert mock_post.call_count == 1, "exactly one alert, actually transmitted"
    url = mock_post.call_args.args[0]
    body = mock_post.call_args.kwargs["json"]
    assert url.startswith("https://api.telegram.org/bot")
    assert url.endswith("/sendMessage")
    assert "QAMC deploy drift" in body["text"]
    assert "2 commits behind" in body["text"]


def test_the_drift_unit_goes_through_the_env_sourcing_wrapper():
    """The fix, pinned at the unit. Matching the existing wrapper pattern
    rather than inventing a third mechanism was the requirement."""
    exec_start = _parse_unit(DRIFT_SERVICE)["Service.ExecStart"]
    assert len(exec_start) == 1
    command = exec_start[0]
    assert command.split()[0].endswith("scripts/run_drift_check.sh"), (
        f"ExecStart={command!r} does not go through the wrapper; a unit that "
        f"invokes Python directly gets no TELEGRAM_* and cannot alert"
    )
    assert 'source "${PROJECT_ROOT}/.env"' in DRIFT_WRAPPER.read_text()


def test_the_drift_finding_still_does_not_mark_the_unit_failed():
    """Unchanged by this fix: exit 1 means the box is behind, which is a
    finding, not a crash."""
    assert _parse_unit(DRIFT_SERVICE)["Service.SuccessExitStatus"] == ["0 1"]


# ===========================================================================
# 2. The audit, mechanised — no unit that can alert may miss its .env
# ===========================================================================
#
# Rex's rule: everything mechanically enforced holds, everything relying on
# remembering a rule slips. The drift unit was written months after the two
# wrapper-based units and simply forgot. This test makes the next one fail
# in CI instead.

def _invoked_repo_file(exec_start: str) -> Path | None:
    """The repo script a unit's ExecStart runs, mapped by basename.

    Basename, not full path: units carry absolute deploy paths that do not
    exist in a checkout or in CI.
    """
    for token in exec_start.split():
        if token.endswith(".py") or token.endswith(".sh"):
            candidate = SCRIPTS_DIR / Path(token).name
            if candidate.is_file():
                return candidate
    return None


def _can_alert(script: Path) -> bool:
    return "TelegramNotifier" in script.read_text()


@pytest.mark.parametrize(
    "service", sorted(SYSTEMD_DIR.glob("*.service")), ids=lambda p: p.name,
)
def test_every_unit_that_can_alert_sources_env(service: Path):
    """A unit whose script can raise a Telegram alert MUST go through a
    wrapper that sources `.env`. Otherwise its alert is printed to the
    journal and delivered to nobody — defect (a), verbatim."""
    exec_start = _parse_unit(service).get("Service.ExecStart")
    assert exec_start, f"{service.name} declares no ExecStart"
    script = _invoked_repo_file(exec_start[0])
    if script is None:
        pytest.skip(f"{service.name} runs nothing from scripts/")

    if script.suffix == ".py":
        # A Python script invoked straight from a unit gets whatever the
        # systemd user environment holds, which on the qamc box is no
        # TELEGRAM_* at all. That is fine only if the script cannot alert.
        # status_board.py is the current example — read-only, and its
        # finding surfaces through the unit's exit status, not Telegram.
        assert not _can_alert(script), (
            f"{service.name} invokes {script.name} directly, but that script "
            f"can raise a Telegram alert. Without .env sourcing the notifier "
            f"disables itself and the alert reaches nobody. Route it through "
            f"a wrapper like scripts/run_pricing_refresh.sh."
        )
        return

    # A wrapper is a unit's entry point into the project. It must hand the
    # process the credentials, whether or not the wrapper itself alerts —
    # what it execs almost always can.
    body = script.read_text()
    assert 'source "${PROJECT_ROOT}/.env"' in body, (
        f"{script.name} is the entry point for {service.name} but does not "
        f"source .env; anything it runs will have no credentials"
    )
    assert script.stat().st_mode & stat.S_IXUSR, (
        f"systemd refuses to start a non-executable ExecStart ({script.name})"
    )


# ===========================================================================
# 3. The probe exercises the real send path — not a variable check
# ===========================================================================

def test_probe_reports_missing_credentials_as_its_own_stage(no_telegram_env):
    from src.notifier import TelegramNotifier

    result = TelegramNotifier().probe()
    assert result.ok is False
    assert result.stage == "credentials"
    assert "reach nobody" in result.detail


def test_probe_catches_a_transport_that_raises(wrapper_env):
    """An egress rule, a DNS failure, a TLS interception, a timeout — the
    request never completes. A variable check sails straight past this."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = ConnectionError("connection refused")
        result = TelegramNotifier().probe()

    assert result.ok is False
    assert result.stage == "transport"
    assert "connection refused" in result.detail


def test_probe_catches_a_revoked_token(wrapper_env):
    """The token is present — a variable check passes. Telegram says no."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response(
            401, {"ok": False, "error_code": 401, "description": "Unauthorized"},
        )
        result = TelegramNotifier().probe()

    assert result.ok is False
    assert result.stage == "api"
    assert "Unauthorized" in result.detail


def test_probe_catches_a_wrong_chat_id(wrapper_env):
    """The chat id is present and syntactically fine. The chat is not."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response(
            400, {"ok": False, "description": "Bad Request: chat not found"},
        )
        result = TelegramNotifier().probe()

    assert result.ok is False
    assert result.stage == "api"
    assert "chat not found" in result.detail


def test_probe_catches_a_blocked_bot(wrapper_env):
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response(
            403, {"ok": False, "description": "Forbidden: bot was blocked by the user"},
        )
        result = TelegramNotifier().probe()

    assert result.ok is False
    assert result.stage == "api"


def test_probe_catches_a_200_carrying_ok_false(wrapper_env):
    """`send()` judges on `raise_for_status()` alone, so a 200 with
    `ok: false` would look like success. The probe reads the body."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response(200, {"ok": False, "description": "nope"})
        result = TelegramNotifier().probe()

    assert result.ok is False
    assert result.stage == "api"
    assert "nope" in result.detail


def test_probe_catches_a_body_that_is_not_json(wrapper_env):
    """A captive portal or a proxy error page answers 200 with HTML."""
    from src.notifier import TelegramNotifier

    response = MagicMock()
    response.status_code = 200
    response.json.side_effect = ValueError("not json")
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = response
        result = TelegramNotifier().probe()

    assert result.ok is False
    assert result.stage == "api"


def test_probe_never_leaks_the_bot_token_into_the_detail(wrapper_env):
    """The detail goes into the journal and into the weekly digest. The
    token must not travel with it — same reasoning as `_redact`."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = ConnectionError(
            "failed to reach https://api.telegram.org/bot111:FAKE-TOKEN-FOR-TESTS/sendMessage"
        )
        result = TelegramNotifier().probe()

    assert "111:FAKE-TOKEN-FOR-TESTS" not in result.detail
    assert "<redacted>" in result.detail


def test_a_successful_probe_sends_and_then_deletes_itself(wrapper_env):
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = [
            _response(200, {"ok": True, "result": {"message_id": 4242}}),
            _response(200, {"ok": True, "result": True}),
        ]
        result = TelegramNotifier().probe()

    assert result.ok is True
    assert result.stage == "delivered"
    assert result.residue is False
    assert mock_post.call_count == 2

    send_url = mock_post.call_args_list[0].args[0]
    delete_url = mock_post.call_args_list[1].args[0]
    assert send_url.endswith("/sendMessage")
    assert delete_url.endswith("/deleteMessage")
    assert mock_post.call_args_list[1].kwargs["json"]["message_id"] == 4242


def test_a_probe_that_cannot_delete_still_proves_the_channel(wrapper_env):
    """The send is what the alarm depends on. A failed delete leaves one
    self-describing message in the chat and is reported as residue, not as
    a broken channel."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = [
            _response(200, {"ok": True, "result": {"message_id": 7}}),
            _response(400, {"ok": False, "description": "message can't be deleted"}),
        ]
        result = TelegramNotifier().probe()

    assert result.ok is True
    assert result.residue is True
    assert "delete refused" in result.detail


def test_the_probe_does_not_buzz_the_operators_phone(wrapper_env):
    """Daily. If it notified, the operator would mute the channel inside a
    fortnight and the alarm would be worse off than before."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response()
        TelegramNotifier().probe()

    assert mock_post.call_args_list[0].kwargs["json"]["disable_notification"] is True


def test_the_probe_transmits_the_same_message_shape_as_a_real_alert(wrapper_env):
    """A probe that built its own payload would prove that *some* request
    reaches Telegram while leaving the real message shape untested — a
    self-test that passes while the thing it stands in for is broken."""
    from src.notifier import TelegramNotifier

    notifier = TelegramNotifier(mission_control_url="https://example.invalid/mc")

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response()
        notifier.probe()
        probe_body = mock_post.call_args_list[0].kwargs["json"]

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response()
        notifier.send("a real alert")
        send_body = mock_post.call_args.kwargs["json"]

    for key in ("chat_id", "parse_mode", "disable_web_page_preview"):
        assert probe_body[key] == send_body[key], (
            f"the probe and a real alert disagree on {key!r}; the probe is "
            f"then not exercising the path the alarm uses"
        )
    assert probe_body["parse_mode"] == "HTML"
    # ...but never carrying the tap-through link a real alert carries.
    assert "example.invalid" not in probe_body["text"]


def test_the_probe_transmits_nothing_during_a_rehearsal(wrapper_env, monkeypatch):
    """A rehearsal replays a real session offline. It must not put traffic
    on the operator's chat, and its non-send must not be recorded as an
    outage — 'we did not check' and 'we checked and it is broken' are the
    two things this whole design exists to keep apart."""
    import src.notifier as notifier_module

    monkeypatch.setattr(notifier_module, "_REHEARSAL_MODE", True)

    with patch("src.notifier.requests.post") as mock_post:
        result = notifier_module.TelegramNotifier().probe()

    assert mock_post.call_count == 0
    assert result.stage == "rehearsal"


def test_send_is_unchanged_by_the_payload_refactor(wrapper_env):
    """`send()` now builds its body through `_build_payload`. Same wire
    format, same single POST, same swallow-everything contract."""
    from src.notifier import TelegramNotifier

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response()
        assert TelegramNotifier().send("hello <b>world</b> & co") is True
        body = mock_post.call_args.kwargs["json"]

    assert body["text"] == "hello &lt;b&gt;world&lt;/b&gt; &amp; co"
    assert body["parse_mode"] == "HTML"
    assert "disable_notification" not in body

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = ConnectionError("down")
        assert TelegramNotifier().send("hello") is False


# ===========================================================================
# 4. The heartbeat script: quiet on success, loud and recorded on failure
# ===========================================================================

@pytest.fixture
def state_path(tmp_path, monkeypatch):
    import scripts.alert_heartbeat as hb

    path = tmp_path / "alerting" / "heartbeat.json"
    monkeypatch.setattr(hb, "STATE_PATH", path)
    return path


def test_a_healthy_probe_sends_the_operator_nothing_and_exits_zero(
    wrapper_env, state_path,
):
    import scripts.alert_heartbeat as hb

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = [
            _response(200, {"ok": True, "result": {"message_id": 1}}),
            _response(200, {"ok": True, "result": True}),
        ]
        assert hb.main([]) == 0

    # Two calls: the probe send and its own delete. Nothing else — in
    # particular no operator-visible message.
    assert mock_post.call_count == 2
    state = hb.load_state(state_path)
    assert state["last_ok"]
    assert state["consecutive_failures"] == 0
    assert state["history"][-1]["ok"] is True


def test_a_broken_channel_exits_nonzero_and_is_recorded(wrapper_env, state_path):
    """The load-bearing behaviour: when the channel is down, the record on
    the box says so and the unit fails. Those are the only two places the
    answer can appear, because the channel that would carry it is the thing
    that is broken."""
    import scripts.alert_heartbeat as hb

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response(
            401, {"ok": False, "description": "Unauthorized"},
        )
        assert hb.main([]) == 1

    state = hb.load_state(state_path)
    assert state["consecutive_failures"] == 1
    assert state["last_failure"]
    entry = state["history"][-1]
    assert entry["ok"] is False
    assert entry["stage"] == "api"
    assert "Unauthorized" in entry["detail"]


def test_a_failed_probe_still_attempts_a_best_effort_alert(wrapper_env, state_path):
    """Usually futile — but a probe can fail at a stage an ordinary send
    survives, and then this is the fastest warning available. One request."""
    import scripts.alert_heartbeat as hb

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value = _response(
            403, {"ok": False, "description": "Forbidden"},
        )
        assert hb.main([]) == 1

    texts = [c.kwargs["json"].get("text", "") for c in mock_post.call_args_list]
    assert any("FAILED its self-test" in t for t in texts)


def test_a_credential_less_heartbeat_fails_rather_than_reporting_health(
    no_telegram_env, state_path,
):
    """The detector must not be able to pass by being misconfigured."""
    import scripts.alert_heartbeat as hb

    with patch("src.notifier.requests.post") as mock_post:
        assert hb.main([]) == 1

    assert mock_post.call_count == 0
    assert hb.load_state(state_path)["history"][-1]["stage"] == "credentials"


def test_a_rehearsal_probe_is_not_recorded(wrapper_env, state_path, monkeypatch):
    import src.notifier as notifier_module
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(notifier_module, "_REHEARSAL_MODE", True)
    assert hb.main([]) == 0
    assert hb.load_state(state_path)["history"] == []


def test_the_record_survives_a_corrupt_state_file(wrapper_env, state_path):
    """A record that cannot be read must not stop the probe: the probe is
    what matters, the record is only how it is remembered."""
    import scripts.alert_heartbeat as hb

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json at all")

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = [
            _response(200, {"ok": True, "result": {"message_id": 1}}),
            _response(200, {"ok": True, "result": True}),
        ]
        assert hb.main([]) == 0

    assert hb.load_state(state_path)["last_ok"]


def test_the_record_does_not_grow_without_bound(state_path):
    import scripts.alert_heartbeat as hb

    state = hb.load_state(state_path)
    for _ in range(hb.HISTORY_LIMIT + 25):
        state = hb.record(state, kind="probe", ok=True, stage="delivered")
    assert len(state["history"]) == hb.HISTORY_LIMIT


# ===========================================================================
# 5. The weekly digest is RETIRED — and must stay retired
# ===========================================================================
#
# It sent the operator one "still alive" message every Sunday, and its
# ABSENCE was supposed to be how he learned the channel had died. Two things
# were wrong with that. A routine confirmation is a message an operator
# learns to swipe away, so its absence is the last thing he notices. And a
# channel that can be dead for seven days before anyone looks is not
# monitored — the desk trades every one of those days.
#
# What replaced it: the sessions themselves prove the channel several times
# a day and write the verdict somewhere that does not depend on Telegram
# working (tests/test_alert_watchdog.py). The operator now hears nothing at
# all until something is actually wrong.

def test_the_weekly_digest_mode_is_gone():
    """Not deprecated, not hidden behind a flag — gone. A CLI that still
    accepts `--digest` is a CLI someone re-adds a timer for."""
    import scripts.alert_heartbeat as hb

    assert not hasattr(hb, "run_digest")
    assert not hasattr(hb, "digest_text")
    with pytest.raises(SystemExit) as exc:
        hb.main(["--digest"])
    assert exc.value.code == 2, "argparse should reject the retired flag"


def test_no_unit_schedules_a_routine_operator_message():
    """The units are the part that would actually put a weekly message on
    his phone, so assert against the shipped units, not the CLI."""
    units = sorted(SYSTEMD_DIR.glob("quant-agent-alert-*"))
    assert units, "the heartbeat units disappeared entirely"
    for unit in units:
        assert "digest" not in unit.name, f"{unit.name} still ships"
        assert "--digest" not in unit.read_text(), f"{unit.name} still invokes it"


def test_a_healthy_probe_is_still_silent_to_the_operator(wrapper_env, state_path):
    """The whole noise budget for a working channel is zero messages."""
    import scripts.alert_heartbeat as hb

    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = [
            _response(200, {"ok": True, "result": {"message_id": 1}}),
            _response(200, {"ok": True, "result": True}),
        ]
        assert hb.main([]) == 0

    # Exactly the probe send and its own delete. Nothing the operator sees.
    assert mock_post.call_count == 2


def test_status_prints_the_record_and_transmits_nothing(wrapper_env, state_path):
    import scripts.alert_heartbeat as hb

    with patch("src.notifier.requests.post") as mock_post:
        assert hb.main(["--status"]) == 0
    assert mock_post.call_count == 0


def test_the_out_of_band_switch_is_dormant_and_off_by_default(monkeypatch):
    """DORMANT, not primary. The likely failure — the channel breaking while
    the box runs — is covered locally by the sessions with no outside
    dependency. This hook only addresses the box itself dying, which nothing
    on the box can report, and turning it on means accepting a third-party
    service. That is the owner's call, so the default is off."""
    import scripts.alert_heartbeat as hb

    monkeypatch.delenv("ALERT_HEARTBEAT_HEALTHCHECK_URL", raising=False)
    with patch("requests.get") as mock_get:
        hb.ping_healthcheck()
    assert mock_get.call_count == 0

    monkeypatch.setenv("ALERT_HEARTBEAT_HEALTHCHECK_URL", "https://hc.invalid/abc")
    with patch("requests.get") as mock_get:
        hb.ping_healthcheck("/fail")
    assert mock_get.call_args.args[0] == "https://hc.invalid/abc/fail"


def test_an_unconfigured_switch_never_reads_as_a_working_one(monkeypatch):
    """"Not configured" rendered as blank reads as "fine". Wherever health
    is printed it has to say so in words."""
    import scripts.alert_heartbeat as hb

    monkeypatch.delenv("ALERT_HEARTBEAT_HEALTHCHECK_URL", raising=False)
    assert "NOT CONFIGURED" in hb.deadman_state()
    _, text = hb.run_status()
    assert "NOT CONFIGURED" in text

    monkeypatch.setenv("ALERT_HEARTBEAT_HEALTHCHECK_URL", "https://hc.invalid/abc")
    assert "NOT CONFIGURED" not in hb.deadman_state()


def test_a_hanging_monitoring_endpoint_cannot_stall_or_fail_the_probe(
    wrapper_env, state_path, monkeypatch,
):
    """A dormant hook that can wedge the job it monitors is worse than no
    hook. Bounded by a timeout, and every failure swallowed."""
    import time as _time

    import requests as _requests

    import scripts.alert_heartbeat as hb

    monkeypatch.setenv("ALERT_HEARTBEAT_HEALTHCHECK_URL", "https://hc.invalid/abc")
    seen: dict[str, object] = {}

    def _hangs(url, timeout=None, **kwargs):
        # Stands in for an endpoint that never answers: `requests` gives up
        # at `timeout` and raises. If no timeout were passed this would hang
        # the trading box's systemd unit until its own kill timer.
        seen["timeout"] = timeout
        _time.sleep(0.2)
        raise _requests.exceptions.ReadTimeout("simulated hang")

    started = _time.monotonic()
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = [
            _response(200, {"ok": True, "result": {"message_id": 1}}),
            _response(200, {"ok": True, "result": True}),
        ]
        with patch("requests.get", side_effect=_hangs):
            assert hb.main([]) == 0, "a hanging monitor must not fail the job"
    elapsed = _time.monotonic() - started

    assert seen["timeout"] is not None, "the ping must be bounded by a timeout"
    assert seen["timeout"] <= 10, seen["timeout"]
    assert elapsed < 5, f"the probe stalled for {elapsed:.1f}s on the monitor"
    # And the real verdict is unaffected by the monitor's failure.
    assert hb.load_state(state_path)["last_ok"]


def test_the_out_of_band_switch_is_separate_from_the_sessions_one(monkeypatch):
    """run_if_et_window.sh already documents why sharing one dead-man's
    check across many jobs pins it green and defeats its purpose."""
    import scripts.alert_heartbeat as hb

    monkeypatch.delenv("ALERT_HEARTBEAT_HEALTHCHECK_URL", raising=False)
    monkeypatch.setenv("HEALTHCHECKS_URL", "https://hc.invalid/sessions")
    with patch("requests.get") as mock_get:
        hb.ping_healthcheck()
    assert mock_get.call_count == 0


# ===========================================================================
# 6. The units
# ===========================================================================

def test_the_heartbeat_units_are_shipped_as_a_pair():
    for path in (HEARTBEAT_SERVICE, HEARTBEAT_TIMER):
        assert path.is_file(), f"{path.name} is missing"


def test_the_probe_timer_fires_every_day_including_weekends():
    """THE reason this timer still exists. The sessions are the primary
    watchdog and they only run Mon-Fri; without a check that also runs on
    Saturday and Sunday, `alert_watchdog.STALE_AFTER_HOURS` could not be an
    alarm at all — it would fire every weekend and be switched off."""
    schedules = _parse_unit(HEARTBEAT_TIMER)["Timer.OnCalendar"]
    assert schedules
    for spec in schedules:
        assert spec.split()[0] == "*-*-*", (
            f"OnCalendar={spec!r} restricts the days the channel is proved; "
            f"the weekend floor is the only reason this unit exists"
        )


def test_the_daily_floor_is_never_looser_than_the_staleness_threshold():
    """The number in the unit and the number in the code are one decision.
    If the timer ever went to every-other-day, `stale` would fire on a
    perfectly healthy desk and the operator would learn to ignore red."""
    from src.alert_watchdog import STALE_AFTER_HOURS

    schedules = _parse_unit(HEARTBEAT_TIMER)["Timer.OnCalendar"]
    assert len(schedules) == 1
    # `*-*-* HH:MM` = once every 24h. Anything sparser breaks the contract.
    assert schedules[0].split()[0] == "*-*-*"
    assert STALE_AFTER_HOURS > 24.0, (
        "the staleness threshold must leave room for one daily firing plus "
        "timer slack, or a healthy desk reports itself stale"
    )
    assert STALE_AFTER_HOURS < 48.0, (
        "a threshold that tolerates two missed days is not a threshold"
    )


def test_the_probe_fires_before_the_first_scheduled_job_of_the_day():
    """06:15 ET, ahead of the 06:30 pricing refresh: on a Monday the channel
    is proved before the week's first session could need to shout over it."""
    spec = _parse_unit(HEARTBEAT_TIMER)["Timer.OnCalendar"][0]
    hh, mm = spec.split()[1].split(":")[:2]
    probe_minute = int(hh) * 60 + int(mm)

    pricing = _parse_unit(SYSTEMD_DIR / "quant-agent-pricing-refresh.timer")
    earliest = min(
        int(s.split()[1].split(":")[0]) * 60 + int(s.split()[1].split(":")[1])
        for s in pricing["Timer.OnCalendar"]
    )
    assert probe_minute < earliest, (
        f"the probe at {spec} fires after the first alertable job of the day"
    )


def test_the_daily_probe_never_fires_inside_a_trading_session_window():
    """Checked against the authoritative window table, not a comment. The
    per-session checks obviously run during sessions — that is their whole
    job — but this standalone unit must stay outside them so a `failed`
    heartbeat unit is never mistaken for a failed trading run."""
    from src.trading_calendar import SESSION_WINDOWS

    for spec in _parse_unit(HEARTBEAT_TIMER)["Timer.OnCalendar"]:
        hh, mm = spec.split()[1].split(":")[:2]
        minute_of_day = int(hh) * 60 + int(mm)
        for mode, (lo, hi) in SESSION_WINDOWS.items():
            assert not (lo <= minute_of_day <= hi), (
                f"{HEARTBEAT_TIMER.name}: OnCalendar={spec!r} fires inside "
                f"the {mode} window"
            )


def test_the_heartbeat_timer_catches_up_after_a_reboot():
    """A missed check and a dead channel look identical in the record, and
    the staleness signal depends on them not being."""
    parsed = _parse_unit(HEARTBEAT_TIMER)
    assert parsed["Timer.Persistent"] == ["true"]
    assert parsed["Install.WantedBy"] == ["timers.target"]


def test_the_heartbeat_unit_runs_the_env_sourcing_wrapper():
    probe = _parse_unit(HEARTBEAT_SERVICE)["Service.ExecStart"][0]
    assert probe.split()[0].endswith("scripts/run_alert_heartbeat.sh")
    assert 'source "${PROJECT_ROOT}/.env"' in HEARTBEAT_WRAPPER.read_text()


def test_a_dead_alert_channel_marks_its_unit_failed():
    """Exit 1 means the desk cannot reach the operator. With the channel
    down, `systemctl --user status` is one of the only places that fact can
    still appear — so it must not be swallowed by SuccessExitStatus."""
    assert _parse_unit(HEARTBEAT_SERVICE)["Service.SuccessExitStatus"] == ["0"]


def test_the_heartbeat_unit_deploy_path_matches_the_other_qamc_units():
    reference = _parse_unit(DRIFT_SERVICE)["Service.WorkingDirectory"]
    parsed = _parse_unit(HEARTBEAT_SERVICE)
    assert parsed["Service.WorkingDirectory"] == reference
    assert parsed["Service.ExecStart"][0].startswith(reference[0])


@pytest.mark.parametrize("wrapper", [DRIFT_WRAPPER, HEARTBEAT_WRAPPER],
                         ids=lambda p: p.name)
def test_the_new_wrappers_are_executable(wrapper: Path):
    """A wrapper committed without its executable bit fails on the box with
    203/EXEC and nothing else."""
    assert wrapper.stat().st_mode & stat.S_IXUSR, wrapper.name
