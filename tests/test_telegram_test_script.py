"""Regression tests for scripts/telegram_test.py.

The script exists so an operator can prove Telegram delivery WITHOUT
running a trading session. The invariants worth pinning are therefore
safety and diagnosis ones, not formatting ones:

  - it never prints a credential value (it is meant to be pasteable);
  - it distinguishes "muted" from "not configured" the way the notifier
    actually parses the kill switch, not by truthiness;
  - it resolves credentials the way the scheduled wrappers do, so a green
    result here means a green result at 09:30 ET;
  - a Telegram-side failure stays non-fatal;
  - it imports no trading/broker/LLM code, so there is no path from this
    diagnostic to an order.

Loaded via importlib because `scripts/` is a tooling directory, not a
package (same convention as tests/test_export_alpaca_trades.py).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "telegram_test.py"

# Distinctive sentinels: if either ever reaches stdout the script has
# leaked a secret into whatever the operator pastes into a chat.
FAKE_TOKEN = "1234567:SENTINEL-TOKEN-MUST-NOT-BE-PRINTED"
FAKE_CHAT_ID = "-100SENTINELCHATID"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "telegram_test_under_test", SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch):
    """The script with its env-file loader neutralised.

    The script derives PROJECT_ROOT from __file__, so left alone it would
    read the developer's real environment file during the suite. Stub it
    out; the loader itself is covered separately against a tmp_path root.
    """
    module = _load_module()
    monkeypatch.setattr(module, "_load_env_file", lambda: (False, set()))
    for name in module._TELEGRAM_VARS:
        monkeypatch.delenv(name, raising=False)
    return module


def _run(mod, monkeypatch, argv=()):
    monkeypatch.setattr(sys, "argv", ["telegram_test.py", *argv])
    return mod.main()


def _no_post(*args, **kwargs):
    raise AssertionError("script must not contact Telegram in this state")


class _Resp:
    def raise_for_status(self):
        return None


def _ok_response(*args, **kwargs):
    return _Resp()


# === credential-state reporting ===


def test_no_credentials_reports_not_configured_and_sends_nothing(
    mod, monkeypatch, capsys,
):
    monkeypatch.setattr(requests, "post", _no_post)
    assert _run(mod, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN : NOT SET" in out
    assert "TELEGRAM_CHAT_ID   : NOT SET" in out
    assert "NOT CONFIGURED" in out


def test_partial_credentials_still_report_not_configured(
    mod, monkeypatch, capsys,
):
    """Token without chat_id is the classic half-finished setup. It must
    read as NOT CONFIGURED, not as a green check."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(requests, "post", _no_post)
    assert _run(mod, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN : SET" in out
    assert "TELEGRAM_CHAT_ID   : NOT SET" in out
    assert "NOT CONFIGURED" in out


def test_report_names_the_source_of_each_value(mod, monkeypatch, capsys):
    """"NOT SET, but I definitely set it" is the common support loop.
    Saying where each value came from ends it."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(
        mod, "_load_env_file", lambda: (True, {"TELEGRAM_BOT_TOKEN"}),
    )
    monkeypatch.setattr(requests, "post", _no_post)
    _run(mod, monkeypatch)
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN : SET (from .env)" in out


def test_kill_switch_reports_muted_and_sends_nothing(mod, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    monkeypatch.setenv("TELEGRAM_DISABLED", "1")
    monkeypatch.setattr(requests, "post", _no_post)
    assert _run(mod, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "MUTED" in out
    assert "NOT CONFIGURED" not in out


def test_inert_kill_switch_value_is_not_reported_as_muted(
    mod, monkeypatch, capsys,
):
    """The notifier accepts exactly 1/true/yes, so TELEGRAM_DISABLED=0
    leaves pushes ON. A truthiness check here would blame the kill switch
    for a missing-credentials problem and send the operator to the wrong
    variable."""
    monkeypatch.setenv("TELEGRAM_DISABLED", "0")
    monkeypatch.setattr(requests, "post", _no_post)
    assert _run(mod, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out
    assert "MUTED" not in out


def test_inert_kill_switch_value_still_sends_with_credentials(
    mod, monkeypatch, capsys,
):
    """Same predicate from the other side: 0 must not mute a configured
    notifier, or the tool would disagree with the sessions it predicts."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    monkeypatch.setenv("TELEGRAM_DISABLED", "0")
    monkeypatch.setattr(requests, "post", _ok_response)
    assert _run(mod, monkeypatch) == 0
    assert "DELIVERED" in capsys.readouterr().out


def test_dry_run_with_credentials_confirms_without_sending(
    mod, monkeypatch, capsys,
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    monkeypatch.setattr(requests, "post", _no_post)
    assert _run(mod, monkeypatch, ["--dry-run"]) == 0
    assert "--dry-run, nothing sent" in capsys.readouterr().out


# === secret discipline ===


def test_never_prints_credential_values(mod, monkeypatch, capsys):
    """The whole point of SET / NOT SET is that the output is safe to
    paste. Cover the send path too — the message body must not embed the
    chat_id or token either."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    sent = []

    def _capture(url, **kwargs):
        sent.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr(requests, "post", _capture)
    assert _run(mod, monkeypatch) == 0
    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out
    assert FAKE_CHAT_ID not in out
    body = sent[0][1]["json"]
    assert FAKE_TOKEN not in body["text"]
    assert FAKE_CHAT_ID not in body["text"]


def test_failure_path_output_carries_no_token(mod, monkeypatch, capsys, caplog):
    """The failure path is the one an operator actually pastes. requests
    puts the request URL — which contains the token — into HTTPError, so
    both stdout and the log line must come out redacted."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)

    class _Failing:
        def raise_for_status(self):
            raise requests.HTTPError(
                "401 Client Error: Unauthorized for url: "
                f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
            )

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Failing())
    with caplog.at_level("WARNING"):
        assert _run(mod, monkeypatch) == 1
    assert FAKE_TOKEN not in capsys.readouterr().out
    assert FAKE_TOKEN not in caplog.text
    assert "<redacted>" in caplog.text


# === delivery / failure ===


def test_send_posts_once_to_send_message_and_exits_zero(mod, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    calls = []

    def _capture(url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(requests, "post", _capture)
    assert _run(mod, monkeypatch) == 0
    assert len(calls) == 1, "exactly one Telegram call — this is a probe, not a burst"
    assert calls[0].endswith("/sendMessage")
    assert "DELIVERED" in capsys.readouterr().out


def test_telegram_failure_is_non_fatal_and_reported(mod, monkeypatch, capsys):
    """A dead Telegram must produce an explained exit 1, never a traceback
    — the notifier swallows by design and the script must not undo that."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)

    def _boom(*args, **kwargs):
        raise requests.ConnectionError("telegram unreachable")

    monkeypatch.setattr(requests, "post", _boom)
    assert _run(mod, monkeypatch) == 1
    assert "SEND FAILED" in capsys.readouterr().out


# === no trading path ===


_IMPORT_PROBE = """
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("telegram_test_under_test", {script!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# Repoint the env-file lookup at an empty dir: the probe must never read
# the developer's real credentials, and sys.path was already extended
# with the true project root at module import.
m.PROJECT_ROOT = pathlib.Path({sandbox!r})
sys.argv = ["telegram_test.py", "--dry-run"]
rc = m.main()
bad = sorted(n for n in sys.modules if n.startswith({forbidden!r}))
print("RC=" + str(rc))
print("BAD=" + json.dumps(bad))
"""


def test_script_pulls_in_no_trading_or_broker_code(tmp_path):
    """Telegram is status output only. The diagnostic must not be able to
    reach the pipeline, the broker, market data, storage or an LLM client.

    Run out-of-process: in-process this would only ever measure what the
    rest of the suite already imported.
    """
    import subprocess

    forbidden = (
        "src.pipeline", "src.execution", "src.agents", "src.risk",
        "src.data", "src.storage", "src.portfolio_constructor", "alpaca",
    )
    code = _IMPORT_PROBE.format(
        script=str(SCRIPT), sandbox=str(tmp_path), forbidden=forbidden,
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM")}
    env |= {
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TELEGRAM_CHAT_ID": FAKE_CHAT_ID,
        "TELEGRAM_DISABLED": "",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "RC=0" in proc.stdout, proc.stdout
    assert "BAD=[]" in proc.stdout, (
        f"telegram_test.py must not import trading code: {proc.stdout}"
    )
    assert FAKE_TOKEN not in proc.stdout
    assert FAKE_CHAT_ID not in proc.stdout


# === env-file loader ===


def test_env_file_wins_over_inherited_environment(monkeypatch, tmp_path):
    """The wrappers do `set -a; source .env; set +a`, so a scheduled
    session sees the FILE's value even when a stale one is exported in
    some shell. An env-wins diagnostic would report DELIVERED off a shell
    token while every scheduled session kept failing on the file's."""
    module = _load_module()
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    (tmp_path / module._ENV_FILENAME).write_text(
        "# comment\n"
        "\n"
        f"export TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n"
        f'TELEGRAM_CHAT_ID="{FAKE_CHAT_ID}"\n'
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stale-shell-value")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    present, applied = module._load_env_file()
    assert present is True
    assert applied == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert os.environ["TELEGRAM_BOT_TOKEN"] == FAKE_TOKEN
    assert os.environ["TELEGRAM_CHAT_ID"] == FAKE_CHAT_ID


def test_env_loader_ignores_non_telegram_keys(monkeypatch, tmp_path):
    """A delivery probe has no business pulling broker or LLM credentials
    into its process."""
    module = _load_module()
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    (tmp_path / module._ENV_FILENAME).write_text(
        "ALPACA_API_KEY=must-not-be-loaded\n"
        "export OPENROUTER_API_KEY=must-not-be-loaded\n"
        f"TELEGRAM_CHAT_ID={FAKE_CHAT_ID}\n"
    )
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    present, applied = module._load_env_file()
    assert present is True
    assert applied == {"TELEGRAM_CHAT_ID"}
    assert "ALPACA_API_KEY" not in os.environ
    assert "OPENROUTER_API_KEY" not in os.environ


def test_env_loader_absent_file_is_not_an_error(monkeypatch, tmp_path):
    module = _load_module()
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    assert module._load_env_file() == (False, set())
