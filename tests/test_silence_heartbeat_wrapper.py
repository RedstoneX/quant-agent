"""A deliberately paused desk must not page every thirty minutes.

The silence watchdog fires on the ABSENCE of sessions. When the owner pauses
the desk, absence is the intended state — so the wrapper checks whether any
trading-mode timer is enabled before it checks anything else. An alarm that
cries wolf about a thing somebody chose on purpose gets muted, and a muted
alarm is the failure item 17c exists to prevent.
"""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_silence_heartbeat.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _env_with_fake_systemctl(tmp_path: Path, *, running: bool) -> dict:
    """PATH with a systemctl that answers `is-active` however we want, and a
    timeout shim that records the command instead of running it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # `is-active`, not `is-enabled`: the pause on this desk is expressed by
    # stopping the timers, which leaves them all still reading "enabled".
    _write_executable(
        bin_dir / "systemctl",
        "#!/bin/bash\n"
        '[[ \"$2\" == is-active ]] || exit 1\n'
        "exit {}\n".format(0 if running else 1),
    )
    _write_executable(
        tmp_path / "timeout",
        "#!/bin/bash\n"
        "# drop the timeout flags, then report rather than run\n"
        "shift 3\n"
        'echo \"WOULD RUN: $*\"\n',
    )
    return os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TIMEOUT_OVERRIDE": str(tmp_path / "timeout"),
        # The worktree this runs from has no .venv of its own; the wrapper
        # needs a real interpreter to read the mode names out of
        # src.trading_calendar, which is the point of the guard.
        "PYTHON_OVERRIDE": sys.executable,
    }


def test_a_paused_desk_is_not_reported_as_a_silent_one(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env_with_fake_systemctl(tmp_path, running=False),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "paused on purpose" in result.stdout
    # The silence CHECK is still suppressed — it would page every 30 min.
    assert "--threshold" not in result.stdout


def test_a_paused_desk_still_gets_the_reminder_path(tmp_path):
    """Regression for docs/WORK.md item 11. When the pause guard first
    shipped it exited 0 in silence, which made "paused and forgotten" the
    one desk-wide state with no alarm behind it at all. The guard must
    suppress the 30-minute silence alarm and hand off to the
    once-a-weekday reminder, not swallow the condition entirely."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env_with_fake_systemctl(tmp_path, running=False),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "WOULD RUN" in result.stdout
    assert "scripts/silence_heartbeat.py --desk-paused" in result.stdout


def test_a_running_desk_still_gets_checked(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env_with_fake_systemctl(tmp_path, running=True),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "scripts/silence_heartbeat.py" in result.stdout
    assert "paused on purpose" not in result.stdout


def test_the_pause_check_can_be_bypassed_by_hand(tmp_path):
    """`--paused-ok` is how a person runs the check against a paused desk on
    purpose. The flag is consumed here and never passed on."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "--paused-ok"],
        env=_env_with_fake_systemctl(tmp_path, running=False),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "scripts/silence_heartbeat.py" in result.stdout
    assert "--paused-ok" not in result.stdout
