"""Tests for scripts/check_deploy_drift.py.

Uses real local git repos (an "origin" bare repo + a "deployed" clone
checked out detached, matching how /home/qamc/quant-agent actually
looks) rather than mocking git — the whole point of this script is
shelling out to git correctly, so that's what needs covering.

Covers: in-sync, behind-by-N (with subject lines + exit code), a dirty
config/settings.yaml that must NOT be reported as drift, and a fetch
failure that must degrade quietly (exit 0, no alert) rather than
alerting falsely.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "check_deploy_drift.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_deploy_drift_under_test", SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses needs this registered pre-exec
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _git(args, cwd):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


def _make_origin(tmp_path) -> Path:
    """A non-bare 'GitHub' repo with an initial commit on main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main"], origin)
    _git(["config", "user.email", "test@example.com"], origin)
    _git(["config", "user.name", "Test"], origin)
    (origin / "README.md").write_text("hello\n")
    _git(["add", "README.md"], origin)
    _git(["commit", "-q", "-m", "initial commit"], origin)
    return origin


def _make_deployed(tmp_path, origin: Path) -> Path:
    """A clone of `origin`, detached at main HEAD — like the real box."""
    deployed = tmp_path / "deployed"
    _git(["clone", "-q", str(origin), str(deployed)], tmp_path)
    _git(["config", "user.email", "test@example.com"], deployed)
    _git(["config", "user.name", "Test"], deployed)
    head = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(["checkout", "-q", "--detach", head], deployed)
    return deployed


def _commit(origin: Path, message: str, filename: str = "file.txt") -> None:
    (origin / filename).write_text(message)
    _git(["add", filename], origin)
    _git(["commit", "-q", "-m", message], origin)


def _fake_notifier():
    """Patch TelegramNotifier so no real HTTP happens; return the mock instance."""
    instance = MagicMock()
    instance.enabled = True
    instance.send.return_value = True
    patcher = patch("src.notifier.TelegramNotifier", return_value=instance)
    return patcher, instance


# ---------------------------------------------------------------------------
# in sync
# ---------------------------------------------------------------------------

def test_in_sync_is_quiet_and_exits_zero(tmp_path, capsys):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)

    patcher, notifier = _fake_notifier()
    with patcher:
        code = mod.main(["--deployed-path", str(deployed), "--no-fetch"])

    assert code == 0
    notifier.send.assert_not_called()
    out = capsys.readouterr().out
    assert "in sync" in out


def test_build_report_in_sync_has_zero_behind(tmp_path):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)

    report = mod.build_report(str(deployed), "origin/main", do_fetch=False)

    assert report.checked
    assert report.behind_count == 0
    assert not report.is_behind
    assert report.missing_commits == []


# ---------------------------------------------------------------------------
# behind by N
# ---------------------------------------------------------------------------

def test_behind_by_n_reports_count_subjects_and_nonzero_exit(tmp_path, capsys):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)

    _commit(origin, "fix: PR 111 hotfix")
    _commit(origin, "chore: bump pricing table")

    patcher, notifier = _fake_notifier()
    with patcher:
        code = mod.main(["--deployed-path", str(deployed)])

    assert code == 1
    notifier.send.assert_called_once()
    alert_text = notifier.send.call_args[0][0]
    assert "2 commits behind" in alert_text
    assert "fix: PR 111 hotfix" in alert_text
    assert "chore: bump pricing table" in alert_text
    # oldest-first ordering
    assert alert_text.index("fix: PR 111 hotfix") < alert_text.index(
        "chore: bump pricing table"
    )

    out = capsys.readouterr().out
    assert "2 commits behind" in out


def test_build_report_behind_by_one_singular_commit_word(tmp_path):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)
    _commit(origin, "fix: single missing commit")

    report = mod.build_report(str(deployed), "origin/main", do_fetch=True)

    assert report.behind_count == 1
    assert report.missing_commits[0][1] == "fix: single missing commit"


# ---------------------------------------------------------------------------
# dirty config/settings.yaml must not read as drift
# ---------------------------------------------------------------------------

def test_dirty_settings_yaml_but_in_sync_is_not_drift(tmp_path, capsys):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)

    config_dir = deployed / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("paper: true\n")
    _git(["add", "config/settings.yaml"], deployed)
    _git(["commit", "-q", "-m", "seed tracked settings.yaml"], deployed)
    # Now dirty it locally, same as the real box's intentional delta.
    (config_dir / "settings.yaml").write_text("paper: true\nlocal_override: 1\n")

    patcher, notifier = _fake_notifier()
    with patcher:
        code = mod.main(["--deployed-path", str(deployed), "--no-fetch"])

    assert code == 0
    notifier.send.assert_not_called()
    out, err = capsys.readouterr()
    assert "in sync" in out
    # config/settings.yaml must never appear as a flagged/unexpected file
    assert "config/settings.yaml" not in err


def test_unexpected_dirty_file_is_noted_but_not_alerted(tmp_path, capsys):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)
    (deployed / "README.md").write_text("locally edited\n")

    report = mod.build_report(str(deployed), "origin/main", do_fetch=False)

    assert report.unexpected_dirty_files == ["README.md"]
    assert not report.is_behind  # dirtiness never becomes drift


# ---------------------------------------------------------------------------
# fetch failure degrades quietly
# ---------------------------------------------------------------------------

def test_fetch_failure_exits_zero_and_does_not_alert(tmp_path, capsys):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)
    # Point "origin" at a path that doesn't exist so fetch fails, simulating
    # a network/DNS outage without touching real network.
    _git(["remote", "set-url", "origin", str(tmp_path / "does-not-exist")], deployed)

    patcher, notifier = _fake_notifier()
    with patcher:
        code = mod.main(["--deployed-path", str(deployed)])  # fetch enabled

    assert code == 0
    notifier.send.assert_not_called()
    err = capsys.readouterr().err
    assert "fetch failed" in err


def test_fetch_remote_reports_failure_tuple(tmp_path):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)
    _git(["remote", "set-url", "origin", "https://127.0.0.1:1/definitely-not-a-remote.git"], deployed)

    ok, error = mod.fetch_remote(str(deployed), "origin/main")

    assert ok is False
    assert error


def test_head_unreadable_path_returns_none_and_exit_three(tmp_path, capsys):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    code = mod.main(["--deployed-path", str(not_a_repo), "--no-fetch"])

    assert code == 3
    err = capsys.readouterr().err
    assert "could not read HEAD" in err


# ---------------------------------------------------------------------------
# --no-telegram
# ---------------------------------------------------------------------------

def test_no_telegram_flag_skips_send_even_when_behind(tmp_path, capsys):
    origin = _make_origin(tmp_path)
    deployed = _make_deployed(tmp_path, origin)
    _commit(origin, "fix: something merged")

    patcher, notifier = _fake_notifier()
    with patcher:
        code = mod.main(
            ["--deployed-path", str(deployed), "--no-telegram"]
        )

    assert code == 1
    notifier.send.assert_not_called()
    out = capsys.readouterr().out
    assert "fix: something merged" in out
