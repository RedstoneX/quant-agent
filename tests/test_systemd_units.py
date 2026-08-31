"""The schedule, under version control — and the check that keeps it there.

THE DEFECT. QAMC's schedule *is* its systemd user units. On 2026-08-31 the
production box ran eleven services and ten timers; the repository tracked
six services and four timers. The seven units that run the trading day and
serve Mission Control — api, morning, midday, intra_check, close, evening,
earnings_preprocess — existed nowhere but on one machine. Had the box been
lost they would have been rebuilt from memory.

The tracked copies were not trustworthy either. `quant-agent-daily.service`
declared `WorkingDirectory=/home/yebo/quant-agent` and ran
`/home/yebo/quant-agent/scripts/run_daily_export.sh` — a path inherited from
the upstream fork this project came from, which exists on no QAMC machine.
Installing the repository's own copy would have broken the daily export on
contact. Nothing compared the two, so nobody knew.

TWO HALVES, BOTH NEEDED. `check_deploy_drift.py` compares COMMITS, so it is
satisfied the moment the checkout sits on origin/main — while the units
systemd actually loaded are whatever was last `cp`'d into
~/.config/systemd/user. git never sees an install.

  - This file is the CI half. It cannot see the box; it gates the
    repository so the tracked units stay installable and stay honest. The
    `/home/yebo` defect is a test here, by name.
  - `scripts/check_unit_drift.py` is the box half. It cannot see a pull
    request; it compares installed against tracked on a daily timer and
    alerts. Its behaviour is tested at the bottom of this file.

Neither half subsumes the other. CI stops the repository regressing; the
timer stops the box diverging.

Nothing here touches the network, the production box, or systemd itself.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = PROJECT_ROOT / "scripts" / "systemd"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# The one account QAMC is deployed under. `/home/yebo` is the upstream
# fork's path and must never reappear in a unit.
DEPLOY_USER = "qamc"
DEPLOY_ROOT = f"/home/{DEPLOY_USER}/quant-agent"

SERVICES = sorted(SYSTEMD_DIR.glob("*.service"))
TIMERS = sorted(SYSTEMD_DIR.glob("*.timer"))
ALL_UNITS = sorted(SERVICES + TIMERS)

# A `.service` with no paired `.timer` must be a long-running daemon that
# enables itself through `[Install]`. Anything else is a unit nothing can
# ever start.
_HOME_PATH = re.compile(r"/home/([A-Za-z0-9_.-]+)")

# Credential shapes that must never appear inline in a tracked unit.
_SECRET_SHAPES = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "Telegram bot token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OpenAI-style secret key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
)
# `Environment=KEY=VALUE` whose KEY looks like a credential.
_CREDENTIAL_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|CHAT_?ID|CREDENTIAL)",
    re.IGNORECASE,
)


def parse_unit(path: Path) -> dict[str, list[str]]:
    """`SECTION.Key -> [values]`. Not configparser: systemd allows a key to
    repeat (two `OnCalendar=` lines is the normal way to say "twice a day")
    and configparser would silently keep only the last."""
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


# ===========================================================================
# 1. The repository must describe the machine it is deployed to
# ===========================================================================

def test_the_units_directory_is_not_empty():
    """A guard on every parametrization below: `glob` returning nothing
    would make each of them vacuously pass."""
    assert SERVICES, "no .service files found — the gates below prove nothing"
    assert TIMERS, "no .timer files found — the gates below prove nothing"


@pytest.mark.parametrize("unit", ALL_UNITS, ids=lambda p: p.name)
def test_no_unit_carries_a_foreign_home_path(unit: Path):
    """THE `/home/yebo` DEFECT, as a test.

    `quant-agent-daily.service` shipped with `/home/yebo/quant-agent` — the
    upstream fork's path — while the box ran `/home/qamc/quant-agent`.
    Installing the tracked copy would have broken the daily P&L export, and
    the repository had no way to notice.
    """
    foreign = sorted({
        user for user in _HOME_PATH.findall(unit.read_text())
        if user != DEPLOY_USER
    })
    assert not foreign, (
        f"{unit.name} references /home/{', /home/'.join(foreign)}; QAMC is "
        f"deployed at {DEPLOY_ROOT}. A unit carrying another account's path "
        f"is broken the moment it is installed."
    )


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_runs_from_the_deploy_root(service: Path):
    working_dir = parse_unit(service).get("Service.WorkingDirectory")
    assert working_dir, f"{service.name} declares no WorkingDirectory"
    assert working_dir[0] == DEPLOY_ROOT, (
        f"{service.name} has WorkingDirectory={working_dir[0]}, expected "
        f"{DEPLOY_ROOT}"
    )


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_execstart_exists_in_the_repo(service: Path):
    """A unit whose ExecStart names a repo script that no longer exists is a
    rename away from a silent, permanent failure. Matched by basename: units
    carry absolute deploy paths that exist neither in a checkout nor in CI.
    """
    exec_start = parse_unit(service).get("Service.ExecStart")
    assert exec_start, f"{service.name} declares no ExecStart"
    for token in exec_start[0].split():
        if token.endswith((".py", ".sh")):
            candidate = SCRIPTS_DIR / Path(token).name
            assert candidate.is_file(), (
                f"{service.name} runs {token}, but scripts/{candidate.name} "
                f"does not exist in the repository"
            )


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_every_timer_has_the_service_it_activates(timer: Path):
    service = SYSTEMD_DIR / f"{timer.stem}.service"
    assert service.is_file(), (
        f"{timer.name} would activate {service.name}, which is not tracked"
    )


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_every_timer_is_installable_and_scheduled(timer: Path):
    parsed = parse_unit(timer)
    assert parsed.get("Timer.OnCalendar"), (
        f"{timer.name} declares no OnCalendar — it would never fire"
    )
    assert parsed.get("Install.WantedBy"), (
        f"{timer.name} has no [Install] WantedBy, so `systemctl --user "
        f"enable` has nothing to link and the timer never starts"
    )


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_is_reachable(service: Path):
    """A service is started either by its paired timer or by `[Install]`.
    A service with neither is dead code that looks like infrastructure —
    which is worse than absent, because it reads as covered.
    """
    has_timer = (SYSTEMD_DIR / f"{service.stem}.timer").is_file()
    declares_install = bool(parse_unit(service).get("Install.WantedBy"))
    assert has_timer or declares_install, (
        f"{service.name} has no paired timer and no [Install] WantedBy — "
        f"nothing on the box would ever start it"
    )


@pytest.mark.parametrize("unit", ALL_UNITS, ids=lambda p: p.name)
def test_no_unit_contains_an_inline_secret(unit: Path):
    """Units are world-readable on the box and public in git. Credentials
    reach them through `EnvironmentFile=` or a wrapper that sources `.env`,
    never inline. This is the by-hand scrub of 2026-08-31, mechanised.
    """
    text = unit.read_text()
    for pattern, label in _SECRET_SHAPES:
        assert not pattern.search(text), (
            f"{unit.name} appears to contain a {label} inline. Route it "
            f"through EnvironmentFile= or an .env-sourcing wrapper."
        )
    parsed = parse_unit(unit)
    for section_key, values in parsed.items():
        if not section_key.endswith(".Environment"):
            continue
        for value in values:
            name = value.partition("=")[0]
            assert not _CREDENTIAL_KEY.search(name), (
                f"{unit.name} sets Environment={name}=... inline. A "
                f"credential-shaped variable belongs in .env, not in a "
                f"tracked unit."
            )


@pytest.mark.parametrize("unit", ALL_UNITS, ids=lambda p: p.name)
def test_every_unit_describes_itself(unit: Path):
    description = parse_unit(unit).get("Unit.Description")
    assert description and description[0], (
        f"{unit.name} has no Description; `systemctl --user list-timers` "
        f"would show it as a bare filename"
    )


# ===========================================================================
# 2. The production units the repository was missing
# ===========================================================================
#
# Capturing them once fixes nothing on its own — a later refactor could
# delete one and CI would stay green. These name what the box actually runs.

# The six ET-windowed trading sessions, self-gating through one wrapper.
SESSION_MODES = (
    "morning", "midday", "intra_check", "close", "evening",
    "earnings_preprocess",
)


@pytest.mark.parametrize("mode", SESSION_MODES)
def test_every_trading_session_has_a_tracked_service_and_timer(mode: str):
    """These seven units ran the trading day from a single machine's
    ~/.config until 2026-08-31."""
    service = SYSTEMD_DIR / f"quant-agent-{mode}.service"
    timer = SYSTEMD_DIR / f"quant-agent-{mode}.timer"
    assert service.is_file(), f"{service.name} is not tracked"
    assert timer.is_file(), f"{timer.name} is not tracked"


@pytest.mark.parametrize("mode", SESSION_MODES)
def test_every_session_service_self_gates_through_the_window_wrapper(mode: str):
    """The sessions do not each carry their own schedule. Every one fires on
    a 30-minute timer and `run_if_et_window.sh` decides whether this is that
    session's ET window — one place where the trading calendar lives.
    """
    service = SYSTEMD_DIR / f"quant-agent-{mode}.service"
    exec_start = parse_unit(service)["Service.ExecStart"][0]
    assert exec_start.endswith(f"run_if_et_window.sh {mode}"), (
        f"{service.name} runs {exec_start!r}; expected the shared window "
        f"wrapper invoked with the {mode!r} mode"
    )


def test_the_mission_control_api_is_tracked_and_self_starting():
    """The API has no timer: it is a long-running daemon, enabled through
    `[Install] WantedBy=default.target` and restarted on failure."""
    service = SYSTEMD_DIR / "quant-agent-api.service"
    assert service.is_file(), "quant-agent-api.service is not tracked"
    parsed = parse_unit(service)
    assert parsed["Service.Type"] == ["simple"]
    assert parsed["Service.Restart"] == ["always"]
    assert parsed["Install.WantedBy"] == ["default.target"]


def test_the_daily_export_no_longer_points_at_the_upstream_fork():
    """Regression pin for the exact defect. Kept separate from the
    parametrized `/home/` gate so a failure names the unit that had it."""
    text = (SYSTEMD_DIR / "quant-agent-daily.service").read_text()
    assert "/home/yebo" not in text
    assert f"ExecStart={DEPLOY_ROOT}/scripts/run_daily_export.sh" in text


# ===========================================================================
# 3. The box half — scripts/check_unit_drift.py
# ===========================================================================
#
# Every case below builds two throwaway directories and compares them. The
# real /home/qamc paths are never read.

UNIT_BODY = """\
[Unit]
Description=example

[Service]
Type=oneshot
WorkingDirectory=/home/qamc/quant-agent
ExecStart=/home/qamc/quant-agent/scripts/run_daily_export.sh
"""

TIMER_BODY = """\
[Unit]
Description=example timer

[Timer]
OnCalendar=*-*-* 08:50 America/New_York

[Install]
WantedBy=timers.target
"""


def _make_box(tmp_path: Path, repo: dict[str, str], installed: dict[str, str],
              enabled: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """Build a fake checkout and a fake ~/.config/systemd/user."""
    repo_root = tmp_path / "checkout"
    repo_units = repo_root / "scripts" / "systemd"
    repo_units.mkdir(parents=True)
    for name, body in repo.items():
        (repo_units / name).write_text(body)

    units_dir = tmp_path / "systemd-user"
    units_dir.mkdir()
    for name, body in installed.items():
        (units_dir / name).write_text(body)
    for name in enabled:
        wants = units_dir / "timers.target.wants"
        wants.mkdir(exist_ok=True)
        (wants / name).symlink_to(units_dir / name)
    return repo_root, units_dir


def _report(tmp_path, repo, installed, enabled=()):
    from scripts.check_unit_drift import build_report

    repo_root, units_dir = _make_box(tmp_path, repo, installed, enabled)
    return build_report(str(repo_root), str(units_dir))


def test_identical_units_are_in_sync(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    assert report.has_drift is False
    assert report.untracked == []
    assert report.modified == []
    assert report.undeployed == []
    assert report.not_enabled == []


def test_a_unit_hand_added_on_the_box_is_reported_as_untracked(tmp_path):
    """THE CASE THAT MATTERS. A unit created directly in
    ~/.config/systemd/user is invisible to every commit-based check this
    desk runs: the checkout can sit exactly on origin/main while systemd
    runs something nobody has ever reviewed."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY, "rogue.service": UNIT_BODY},
    )
    from scripts.check_unit_drift import format_alert

    assert report.untracked == ["rogue.service"]
    assert report.has_drift is True
    assert "rogue.service" in format_alert(report)


def test_a_unit_edited_in_place_on_the_box_is_reported_as_modified(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY},
        {"a.service": UNIT_BODY.replace("oneshot", "simple")},
    )
    assert report.modified == ["a.service"]
    assert report.has_drift is True


def test_a_comment_only_change_still_counts_as_drift(tmp_path):
    """Byte-for-byte, deliberately. Deploys here are a literal `cp`, and a
    box copy whose rationale comments no longer match the repository is a
    copy nobody can reason about."""
    report = _report(
        tmp_path,
        {"a.service": "# why this exists\n" + UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.modified == ["a.service"]


def test_a_tracked_unit_never_copied_to_the_box_is_reported(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "b.service": UNIT_BODY},
        {"a.service": UNIT_BODY},
    )
    assert report.undeployed == ["b.service"]
    assert report.has_drift is True


def test_an_installed_but_unenabled_timer_is_reported(tmp_path):
    """The quietest failure of the four: the unit is present, correct, and
    identical to the repository — and never fires, because nothing linked it
    into timers.target.wants."""
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        {"a.service": UNIT_BODY, "a.timer": TIMER_BODY},
        enabled=(),
    )
    assert report.not_enabled == [("a.timer", "timers.target")]
    assert report.has_drift is True


def test_enablement_is_not_reported_for_units_that_are_not_installed(tmp_path):
    """An undeployed unit is already one finding. Reporting it a second time
    as 'not enabled' is the same fact twice, which is how an operator learns
    to skim."""
    report = _report(
        tmp_path, {"a.timer": TIMER_BODY}, {},
    )
    assert report.undeployed == ["a.timer"]
    assert report.not_enabled == []


def test_a_oneshot_service_with_no_install_section_is_not_expected_enabled(
    tmp_path,
):
    """The six session services are started by their timers and declare no
    `[Install]`. Demanding a `.wants` link for them would alarm forever."""
    report = _report(
        tmp_path, {"a.service": UNIT_BODY}, {"a.service": UNIT_BODY},
    )
    assert report.not_enabled == []
    assert report.has_drift is False


def test_a_dangling_wants_symlink_reads_as_not_enabled(tmp_path):
    from scripts.check_unit_drift import build_report

    repo_root, units_dir = _make_box(
        tmp_path, {"a.timer": TIMER_BODY}, {"a.timer": TIMER_BODY},
    )
    wants = units_dir / "timers.target.wants"
    wants.mkdir()
    (wants / "a.timer").symlink_to(units_dir / "gone.timer")
    report = build_report(str(repo_root), str(units_dir))
    assert report.not_enabled == [("a.timer", "timers.target")]


def test_wants_directories_are_not_mistaken_for_units(tmp_path):
    """`timers.target.wants` and the symlinks inside it live in the same
    directory as the units. Counting a symlink as an installed unit would
    report permanent phantom drift."""
    report = _report(
        tmp_path,
        {"a.timer": TIMER_BODY},
        {"a.timer": TIMER_BODY},
        enabled=("a.timer",),
    )
    assert report.installed_units == ["a.timer"]
    assert report.has_drift is False


def test_non_unit_files_are_ignored(tmp_path):
    report = _report(
        tmp_path,
        {"a.service": UNIT_BODY, "README.md": "notes"},
        {"a.service": UNIT_BODY, "a.service.bak": UNIT_BODY},
    )
    assert report.repo_units == ["a.service"]
    assert report.installed_units == ["a.service"]
    assert report.has_drift is False


# --- exit codes and the alert path -----------------------------------------

def test_missing_systemd_directory_is_an_operator_problem_not_drift(tmp_path):
    from scripts.check_unit_drift import build_report, main

    repo_root, _ = _make_box(tmp_path, {"a.service": UNIT_BODY}, {})
    report = build_report(str(repo_root), str(tmp_path / "nope"))
    assert report.checked is False
    assert report.has_drift is False
    assert main([
        "--repo-path", str(repo_root),
        "--units-path", str(tmp_path / "nope"),
        "--no-telegram",
    ]) == 3


def test_missing_repo_unit_directory_is_an_operator_problem(tmp_path):
    from scripts.check_unit_drift import build_report

    report = build_report(str(tmp_path / "nope"), str(tmp_path))
    assert report.checked is False
    assert "repo unit directory not found" in report.error


def test_main_exits_zero_when_in_sync(tmp_path, capsys):
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path, {"a.service": UNIT_BODY}, {"a.service": UNIT_BODY},
    )
    code = main([
        "--repo-path", str(repo_root), "--units-path", str(units_dir),
        "--no-telegram",
    ])
    assert code == 0
    assert "in sync" in capsys.readouterr().out


def test_main_exits_one_and_names_every_bucket_on_drift(tmp_path, capsys):
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path,
        {"a.service": UNIT_BODY, "gone.service": UNIT_BODY},
        {"a.service": UNIT_BODY.replace("oneshot", "simple"),
         "rogue.service": UNIT_BODY},
    )
    code = main([
        "--repo-path", str(repo_root), "--units-path", str(units_dir),
        "--no-telegram",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "rogue.service" in out
    assert "a.service" in out
    assert "gone.service" in out
    # The operator must be told what to do about it.
    assert "cp scripts/systemd/*" in out


def test_the_alert_is_pushed_through_the_existing_notifier(tmp_path):
    """Same `TelegramNotifier` as check_deploy_drift.py and the cost
    circuit. No new sender, no new credential path."""
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path, {}, {"rogue.service": UNIT_BODY},
    )
    notifier = MagicMock()
    notifier.enabled = True
    with patch("src.notifier.TelegramNotifier", return_value=notifier):
        code = main([
            "--repo-path", str(repo_root), "--units-path", str(units_dir),
        ])
    assert code == 1
    notifier.send.assert_called_once()
    assert "rogue.service" in notifier.send.call_args[0][0]


def test_no_alert_is_pushed_when_in_sync(tmp_path):
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path, {"a.service": UNIT_BODY}, {"a.service": UNIT_BODY},
    )
    notifier = MagicMock()
    notifier.enabled = True
    with patch("src.notifier.TelegramNotifier", return_value=notifier):
        assert main([
            "--repo-path", str(repo_root), "--units-path", str(units_dir),
        ]) == 0
    notifier.send.assert_not_called()


def test_a_disabled_notifier_degrades_to_printing(tmp_path, capsys):
    """It must still exit 1. The finding is real whether or not the desk can
    currently shout about it."""
    from scripts.check_unit_drift import main

    repo_root, units_dir = _make_box(
        tmp_path, {}, {"rogue.service": UNIT_BODY},
    )
    notifier = MagicMock()
    notifier.enabled = False
    with patch("src.notifier.TelegramNotifier", return_value=notifier):
        code = main([
            "--repo-path", str(repo_root), "--units-path", str(units_dir),
        ])
    assert code == 1
    notifier.send.assert_not_called()
    assert "Telegram not configured" in capsys.readouterr().err


# --- the unit that schedules the check -------------------------------------

def test_the_unit_drift_check_is_itself_scheduled_on_the_box():
    """A check nobody runs is a check that does not exist."""
    service = SYSTEMD_DIR / "quant-agent-unit-drift.service"
    timer = SYSTEMD_DIR / "quant-agent-unit-drift.timer"
    assert service.is_file() and timer.is_file()
    parsed = parse_unit(service)
    # Exit 1 is a finding, not a crash — same contract as the deploy-drift
    # unit. Exit 3 (missing directory) must still mark the unit failed.
    assert parsed["Service.SuccessExitStatus"] == ["0 1"]
    assert parsed["Service.ExecStart"] == [
        f"{DEPLOY_ROOT}/scripts/run_unit_drift_check.sh"
    ]


def test_the_unit_drift_check_runs_every_day_not_only_weekdays():
    """Units get hand-added at weekends too, and this desk's recorded blind
    spot is precisely the weekend (the pricing cache went stale over one).
    The check costs no network and no model call."""
    on_calendar = parse_unit(
        SYSTEMD_DIR / "quant-agent-unit-drift.timer"
    )["Timer.OnCalendar"]
    assert on_calendar == ["*-*-* 08:50 America/New_York"]
    assert not any("Mon" in entry for entry in on_calendar)


def test_the_unit_drift_wrapper_sources_env_and_is_executable():
    """Redundant with the parametrized gate in test_alert_heartbeat.py, and
    kept anyway: that gate proves the rule holds across all units, this
    names the file when it is this one that broke."""
    wrapper = SCRIPTS_DIR / "run_unit_drift_check.sh"
    assert wrapper.is_file()
    assert 'source "${PROJECT_ROOT}/.env"' in wrapper.read_text()
    assert wrapper.stat().st_mode & stat.S_IXUSR
