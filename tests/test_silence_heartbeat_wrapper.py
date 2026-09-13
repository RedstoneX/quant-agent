"""A deliberately paused desk must not page every thirty minutes.

The silence watchdog fires on the ABSENCE of sessions. When the owner pauses
the desk, absence is the intended state — so the wrapper checks whether any
trading-mode timer is enabled before it checks anything else. An alarm that
cries wolf about a thing somebody chose on purpose gets muted, and a muted
alarm is the failure item 17c exists to prevent.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_silence_heartbeat.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _env_with_fake_systemctl(tmp_path: Path, *, enabled: bool) -> dict:
    """PATH with a systemctl that answers `is-enabled` however we want, and a
    timeout shim that records the command instead of running it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(
        bin_dir / "systemctl",
        "#!/bin/bash\nexit {}\n".format(0 if enabled else 1),
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
    }


def test_a_paused_desk_is_not_reported_as_a_silent_one(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env_with_fake_systemctl(tmp_path, enabled=False),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "paused on purpose" in result.stdout
    assert "WOULD RUN" not in result.stdout   # never reached the watchdog


def test_a_running_desk_still_gets_checked(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env_with_fake_systemctl(tmp_path, enabled=True),
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
        env=_env_with_fake_systemctl(tmp_path, enabled=False),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "scripts/silence_heartbeat.py" in result.stdout
    assert "--paused-ok" not in result.stdout
