"""The scheduled price-cache refresh, and the alert that keeps it from
failing silently.

THE DEFECT (recorded as open defect (b)). `data/openrouter_pricing_cache.json`
gates every paid LLM call: past 24h freshness + 24h grace the cost circuit
fails closed and suspends paid analysis for the rest of the day. Nothing
refreshed that file on a schedule — the only callers were
`TradingPipeline.__init__` and `activate_paid_call_session`, i.e. a paid
session actually starting. So the cache aged exactly when no session was
running to notice it. Verified live on 2026-08-30: last refreshed by a real
session on 2026-08-28 ~13:30 UTC, stale past the freshness window over the
weekend, and only a manual refresh before Monday kept the desk trading.

Two things had to be true for the fix, and this file pins both:

  1. The scheduled thing must refresh the cache that gates the calls. Before
     2026-08-31 `scripts/refresh_pricing.py` refreshed only the LiteLLM
     cost-REPORTING cache — scheduling it as it stood would have closed
     nothing, which is the kind of near-miss worth a test rather than a
     comment.
  2. It must run on the weekend, and it must complain when it fails. A
     scheduled refresh that has been silently failing for a week reproduces
     the original defect exactly.

Nothing here touches the network, the production box, or the real cache
files.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = PROJECT_ROOT / "scripts" / "systemd"
SERVICE = SYSTEMD_DIR / "quant-agent-pricing-refresh.service"
TIMER = SYSTEMD_DIR / "quant-agent-pricing-refresh.timer"
WRAPPER = PROJECT_ROOT / "scripts" / "run_pricing_refresh.sh"

FRESH_HOURS = 24.0


def _parse_unit(path: Path) -> dict[str, list[str]]:
    """`SECTION.Key -> [values]`. Not configparser: systemd allows a key to
    repeat (the timer carries two `OnCalendar=` lines) and configparser would
    silently keep only the last, which is precisely the line a weekend-
    coverage test must not lose."""
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


# ---------------------------------------------------------------------------
# 1. The scheduled script refreshes the cache that actually gates paid calls
# ---------------------------------------------------------------------------

def test_refresh_all_refreshes_the_openrouter_cache_not_only_litellm(monkeypatch):
    """The near-miss: a timer wired to the old script would have refreshed
    the cost-reporting cache and left the desk-stopping one untouched."""
    import scripts.refresh_pricing as rp

    called: list[str] = []

    def _openrouter(force: bool = False):
        called.append(f"openrouter(force={force})")
        return True

    def _litellm(force: bool = False):
        called.append(f"litellm(force={force})")
        return True

    monkeypatch.setattr(rp, "refresh_openrouter_pricing", _openrouter)
    monkeypatch.setattr(rp, "refresh_pricing", _litellm)
    monkeypatch.setattr(rp, "openrouter_cache_age_hours", lambda: 0.1)

    outcome = rp.refresh_all(force=True)

    assert "openrouter(force=True)" in called
    assert "litellm(force=True)" in called
    # The second, network-free openrouter call is the completeness re-read of
    # the file just written — see RefreshOutcome's docstring.
    assert called.count("openrouter(force=False)") == 1
    assert outcome.desk_can_open


# ---------------------------------------------------------------------------
# 2. The verdict is read off the FILE, not off the return value
# ---------------------------------------------------------------------------

def _outcome(**kwargs):
    from scripts.refresh_pricing import RefreshOutcome

    base = dict(
        openrouter_call_ok=True,
        openrouter_age_hours=0.1,
        accepted_models_priced=True,
        litellm_call_ok=True,
        models_priced=16,
    )
    base.update(kwargs)
    return RefreshOutcome(**base)


@pytest.mark.parametrize(
    "kwargs,can_open,why",
    [
        (dict(), True, "fresh, complete, everything worked"),
        (
            dict(openrouter_call_ok=False),
            True,
            "a --force fetch failing against a still-current cache is not an "
            "outage; alerting on it with 24h of runway left trains operators "
            "to ignore the channel",
        ),
        (
            dict(openrouter_age_hours=None, accepted_models_priced=False),
            False,
            "no cache at all — the circuit has nothing to price with",
        ),
        (
            dict(openrouter_age_hours=30.0, accepted_models_priced=False),
            False,
            "stale: the grace window is now burning down to a hard suspension",
        ),
        (
            dict(accepted_models_priced=False),
            False,
            "current but missing an accepted model fails the circuit closed "
            "immediately, freshness notwithstanding",
        ),
        (
            dict(litellm_call_ok=False),
            True,
            "the LiteLLM cache is cost reporting only; it gates no call",
        ),
    ],
)
def test_desk_can_open_is_judged_on_the_cache_file(kwargs, can_open, why):
    assert _outcome(**kwargs).desk_can_open is can_open, why


def test_the_stale_alert_names_the_consequence_not_just_the_symptom():
    text = _outcome(
        openrouter_age_hours=30.0, accepted_models_priced=False,
    ).alert_text()
    assert "30.0h" in text
    assert "trade nothing" in text
    assert "refresh_pricing.py --force" in text


def test_the_incomplete_catalog_alert_is_a_different_message():
    """A fresh-but-incomplete catalog and a stale one need different
    instructions — one is a network problem, the other a config problem."""
    text = _outcome(accepted_models_priced=False).alert_text()
    assert "does NOT price every accepted model" in text
    assert "config/settings.yaml" in text


# ---------------------------------------------------------------------------
# 3. Failure is loud
# ---------------------------------------------------------------------------

def test_a_stale_cache_exits_nonzero_and_alerts(monkeypatch, capsys):
    import scripts.refresh_pricing as rp

    sent: list[str] = []
    monkeypatch.setattr(
        rp, "refresh_all",
        lambda force=False: _outcome(
            openrouter_age_hours=41.0, accepted_models_priced=False,
        ),
    )
    monkeypatch.setattr(rp, "send_alert", lambda message: sent.append(message) or True)

    assert rp.main(["--quiet"]) == 1
    assert len(sent) == 1
    assert "41.0h" in sent[0]


def test_a_healthy_refresh_is_silent_and_exits_zero(monkeypatch):
    import scripts.refresh_pricing as rp

    sent: list[str] = []
    monkeypatch.setattr(rp, "refresh_all", lambda force=False: _outcome())
    monkeypatch.setattr(rp, "send_alert", lambda message: sent.append(message) or True)

    assert rp.main(["--quiet"]) == 0
    assert sent == []


def test_no_telegram_still_exits_nonzero(monkeypatch):
    """Suppressing the push must not suppress the failure — the systemd unit
    reads the exit code."""
    import scripts.refresh_pricing as rp

    sent: list[str] = []
    monkeypatch.setattr(
        rp, "refresh_all",
        lambda force=False: _outcome(
            openrouter_age_hours=None, accepted_models_priced=False,
        ),
    )
    monkeypatch.setattr(rp, "send_alert", lambda message: sent.append(message) or True)

    assert rp.main(["--quiet", "--no-telegram"]) == 1
    assert sent == []


def test_a_litellm_only_failure_does_not_alert(monkeypatch):
    import scripts.refresh_pricing as rp

    sent: list[str] = []
    monkeypatch.setattr(
        rp, "refresh_all", lambda force=False: _outcome(litellm_call_ok=False),
    )
    monkeypatch.setattr(rp, "send_alert", lambda message: sent.append(message) or True)

    assert rp.main(["--quiet"]) == 0
    assert sent == []


def test_send_alert_uses_the_existing_notifier(monkeypatch):
    """No new sender and no new channel — the same `TelegramNotifier` the
    shutdown/hold alerts and check_deploy_drift.py already use."""
    import src.notifier as notifier_module
    from scripts.refresh_pricing import send_alert

    delivered: list[str] = []

    class _Notifier:
        enabled = True

        def send(self, message, *a, **k):
            delivered.append(message)
            return True

    monkeypatch.setattr(notifier_module, "TelegramNotifier", _Notifier)
    assert send_alert("hello") is True
    assert delivered == ["hello"]


def test_send_alert_reports_rather_than_crashes_when_unconfigured(monkeypatch):
    import src.notifier as notifier_module
    from scripts.refresh_pricing import send_alert

    class _Notifier:
        enabled = False

        def send(self, message, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("a disabled notifier must not be sent through")

    monkeypatch.setattr(notifier_module, "TelegramNotifier", _Notifier)
    assert send_alert("hello") is False


# ---------------------------------------------------------------------------
# 4. The units — installable, and firing on the days that bit
# ---------------------------------------------------------------------------

def test_the_units_are_shipped_as_a_pair():
    assert SERVICE.is_file()
    assert TIMER.is_file()


def test_the_timer_fires_every_day_including_weekends():
    """The defect happened over a weekend, with no session running to notice.
    A `Mon..Fri` spec — which is what the neighbouring drift-check timer
    correctly uses, because a deploy check only matters on a trading day —
    would miss it verbatim."""
    schedules = _parse_unit(TIMER)["Timer.OnCalendar"]
    assert schedules, "the timer schedules nothing"
    for spec in schedules:
        first = spec.split()[0]
        assert first == "*-*-*", (
            f"OnCalendar={spec!r} restricts the days it fires; the pricing "
            f"cache goes stale on days the market is closed"
        )


def test_no_firing_lands_inside_a_trading_session_window():
    """A refresh is a safe, atomic write, but scheduling it on top of a
    session start is asking for a confusing log. Checked against the
    authoritative window table rather than a comment."""
    from src.trading_calendar import SESSION_WINDOWS

    for spec in _parse_unit(TIMER)["Timer.OnCalendar"]:
        hh, mm = spec.split()[1].split(":")[:2]
        minute_of_day = int(hh) * 60 + int(mm)
        for mode, (lo, hi) in SESSION_WINDOWS.items():
            assert not (lo <= minute_of_day <= hi), (
                f"OnCalendar={spec!r} fires inside the {mode} window"
            )


def test_the_timer_catches_up_after_a_reboot():
    assert _parse_unit(TIMER)["Timer.Persistent"] == ["true"]
    assert _parse_unit(TIMER)["Install.WantedBy"] == ["timers.target"]


def test_the_service_runs_the_wrapper_that_exists_and_is_executable():
    exec_start = _parse_unit(SERVICE)["Service.ExecStart"]
    assert len(exec_start) == 1
    command = exec_start[0]
    assert command.split()[0].endswith("scripts/run_pricing_refresh.sh")
    assert WRAPPER.is_file()
    assert WRAPPER.stat().st_mode & stat.S_IXUSR, (
        "systemd refuses to start a non-executable ExecStart"
    )


def test_the_service_forces_the_fetch():
    """Without --force the script only fetches once the cache has ALREADY
    passed its freshness window, which pins the file permanently at the edge
    of staleness and keeps the reservation multiplier widened for nothing."""
    assert "--force" in _parse_unit(SERVICE)["Service.ExecStart"][0]


def test_the_service_deploy_path_matches_the_other_qamc_units():
    """One wrong prefix and the refresh writes a pristine cache somewhere no
    session ever reads it — silently, and with a zero exit code."""
    reference = _parse_unit(
        SYSTEMD_DIR / "quant-agent-drift-check.service"
    )["Service.WorkingDirectory"]
    assert _parse_unit(SERVICE)["Service.WorkingDirectory"] == reference
    assert _parse_unit(SERVICE)["Service.ExecStart"][0].startswith(reference[0])


def test_the_service_lets_a_failed_refresh_mark_the_unit_failed():
    """Exit 1 means the cache did not land. Unlike the drift check — where
    exit 1 is a finding — that is a genuine failure and must be visible in
    `systemctl --user status`, not swallowed by SuccessExitStatus."""
    assert _parse_unit(SERVICE)["Service.SuccessExitStatus"] == ["0"]


def test_the_wrapper_sources_env_so_the_alert_can_actually_send(monkeypatch):
    """An alert path with no credentials is the silent-failure defect one
    level up."""
    body = WRAPPER.read_text()
    assert "source \"${PROJECT_ROOT}/.env\"" in body
    assert "cd \"$PROJECT_ROOT\"" in body, (
        "the cache paths are relative; the wrong cwd writes the cache where "
        "no session reads it"
    )
