"""Credential delivery via systemd credentials, and the placeholder detector.

EVERY VALUE IN THIS FILE IS AN OBVIOUS DUMMY. Nothing here is, resembles, or is
derived from a real credential. The "realistic-shaped" values below are random
alphanumeric strings chosen only to be free of the stand-in markers the detector
looks for — they authenticate with nothing.
"""

import logging
from datetime import date
from pathlib import Path

import pytest

from src.credentials import (
    CREDENTIALS_DIRECTORY_ENV,
    CredentialDeliveryError,
    describe_delivery,
    load_systemd_credentials,
    looks_like_placeholder,
    placeholder_reason,
    report_startup_credentials,
)

# Dummy values. Not credentials. Shaped only so the detector has no stand-in
# marker to catch — that is the whole point of the "realistic" pair.
DUMMY_KEY_FROM_SYSTEMD = "AKFAKE7QZ2X9RUNITTEST"
DUMMY_SECRET_FROM_SYSTEMD = "s3cFAKEtestONLYvalue0000nothingreal11"
DUMMY_KEY_FROM_ENV = "AKENV7QZ2X9RUNITTESTV"
DUMMY_SECRET_FROM_ENV = "envFAKEtestONLYvalue0000nothingreal22"


def _write_credentials(directory: Path, **values: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (directory / name).write_text(value)
    return directory


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

def test_credentials_directory_present_and_readable(tmp_path, monkeypatch):
    """The happy path: systemd delivered both credentials as files."""
    directory = _write_credentials(
        tmp_path / "creds",
        alpaca_api_key=DUMMY_KEY_FROM_SYSTEMD,
        alpaca_secret_key=DUMMY_SECRET_FROM_SYSTEMD,
    )
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    resolved = load_systemd_credentials()

    assert resolved == {
        "ALPACA_API_KEY": DUMMY_KEY_FROM_SYSTEMD,
        "ALPACA_SECRET_KEY": DUMMY_SECRET_FROM_SYSTEMD,
    }


def test_trailing_newline_is_stripped(tmp_path, monkeypatch):
    """A file written by an ordinary editor ends in a newline; a header value must not.

    This is the difference between working and a puzzling 401, so it is pinned.
    """
    directory = _write_credentials(
        tmp_path / "creds",
        alpaca_api_key=f"{DUMMY_KEY_FROM_SYSTEMD}\n",
    )
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    assert load_systemd_credentials()["ALPACA_API_KEY"] == DUMMY_KEY_FROM_SYSTEMD


def test_credentials_directory_absent_falls_back_to_environment(monkeypatch):
    """No CREDENTIALS_DIRECTORY means nobody wired credentials — not a failure."""
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)

    assert load_systemd_credentials() == {}


def test_credentials_directory_blank_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, "   ")

    assert load_systemd_credentials() == {}


def test_unrelated_credentials_directory_falls_back(tmp_path, monkeypatch):
    """A unit may load credentials this desk knows nothing about. Not a failure."""
    directory = _write_credentials(tmp_path / "creds", something_else="whatever")
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    assert load_systemd_credentials() == {}


def test_empty_credential_refuses_to_start(tmp_path, monkeypatch):
    """Present but empty must be fatal — never a silent fall back to the placeholder."""
    directory = _write_credentials(tmp_path / "creds", alpaca_api_key="")
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    with pytest.raises(CredentialDeliveryError) as excinfo:
        load_systemd_credentials()

    message = str(excinfo.value)
    assert "alpaca_api_key" in message
    assert "empty" in message
    # Plain English, in the style of the existing required-key failure: it must
    # say what happened and why the desk stopped, to someone who is not a developer.
    assert "refusing to start" in message


def test_whitespace_only_credential_refuses_to_start(tmp_path, monkeypatch):
    directory = _write_credentials(tmp_path / "creds", alpaca_api_key="   \n  ")
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    with pytest.raises(CredentialDeliveryError):
        load_systemd_credentials()


def test_unreadable_credential_refuses_to_start(tmp_path, monkeypatch):
    directory = _write_credentials(tmp_path / "creds", alpaca_api_key=DUMMY_KEY_FROM_SYSTEMD)
    target = directory / "alpaca_api_key"
    target.chmod(0o000)
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    try:
        if target.read_text():  # running as root ignores the mode — skip rather than lie
            pytest.skip("cannot make a file unreadable as this user")
    except OSError:
        pass

    with pytest.raises(CredentialDeliveryError) as excinfo:
        load_systemd_credentials()
    assert "refusing to start" in str(excinfo.value)
    target.chmod(0o600)


def test_advertised_directory_that_does_not_exist_refuses_to_start(tmp_path, monkeypatch):
    """systemd said it delivered credentials and the directory is not there."""
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(tmp_path / "nope"))

    with pytest.raises(CredentialDeliveryError) as excinfo:
        load_systemd_credentials()
    assert "refusing to start" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Both delivery paths must produce an identical ApiKeysConfig
# ---------------------------------------------------------------------------

_SETTINGS_TEMPLATE = """
api_keys:
  anthropic: "${ANTHROPIC_API_KEY}"
  fred: "${FRED_API_KEY}"
  alpaca_key: "${ALPACA_API_KEY}"
  alpaca_secret: "${ALPACA_SECRET_KEY}"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/quant_agent.db"
"""


def _load(tmp_path):
    from src.config import load_config

    tmp_path.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(_SETTINGS_TEMPLATE)
    return load_config(config_file)


def test_api_keys_identical_between_delivery_paths(tmp_path, monkeypatch):
    """The whole point of the design: downstream cannot tell the two paths apart."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("FRED_API_KEY", "dummy-fred")

    # Path 1 — environment only.
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", DUMMY_KEY_FROM_SYSTEMD)
    monkeypatch.setenv("ALPACA_SECRET_KEY", DUMMY_SECRET_FROM_SYSTEMD)
    via_environment = _load(tmp_path / "env").api_keys

    # Path 2 — systemd credentials, with the environment holding the *placeholder*
    # the live box actually has, so this also proves precedence.
    directory = _write_credentials(
        tmp_path / "creds",
        alpaca_api_key=DUMMY_KEY_FROM_SYSTEMD,
        alpaca_secret_key=DUMMY_SECRET_FROM_SYSTEMD,
    )
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))
    monkeypatch.setenv("ALPACA_API_KEY", "placeholder-alpaca-key-xxx")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "placeholder-alpaca-secret-x")
    via_systemd = _load(tmp_path / "sysd").api_keys

    assert via_systemd == via_environment
    assert via_systemd.alpaca_key == DUMMY_KEY_FROM_SYSTEMD
    assert via_systemd.alpaca_secret == DUMMY_SECRET_FROM_SYSTEMD


def test_systemd_credentials_do_not_disturb_other_interpolations(tmp_path, monkeypatch):
    """Only the mapped names are overridden; every other ${VAR} resolves as before."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-untouched")
    monkeypatch.setenv("FRED_API_KEY", "dummy-fred-untouched")
    monkeypatch.setenv("ALPACA_API_KEY", "placeholder-alpaca-key-xxx")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "placeholder-alpaca-secret-x")
    directory = _write_credentials(
        tmp_path / "creds", alpaca_api_key=DUMMY_KEY_FROM_SYSTEMD
    )
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_ENV, str(directory))

    api_keys = _load(tmp_path / "cfg").api_keys

    assert api_keys.anthropic == "dummy-anthropic-untouched"
    assert api_keys.fred == "dummy-fred-untouched"
    assert api_keys.alpaca_key == DUMMY_KEY_FROM_SYSTEMD
    # Not delivered by systemd, so this one still comes from the environment.
    assert api_keys.alpaca_secret == "placeholder-alpaca-secret-x"


def test_substitute_env_vars_unchanged_without_overrides(monkeypatch):
    """The existing behaviour is explicitly pinned, since every other key relies on it."""
    from src.config import _substitute_env_vars

    monkeypatch.setenv("SOME_VAR", "some-value")
    assert _substitute_env_vars("${SOME_VAR}") == "some-value"
    monkeypatch.delenv("SOME_VAR", raising=False)
    assert _substitute_env_vars("${SOME_VAR}") == ""


# ---------------------------------------------------------------------------
# The placeholder detector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "placeholder-alpaca-key",
        "PLACEHOLDER-CREDENTIAL-NOT-DELIVERED",  # the units' own fallback
        "changeme",
        "your-key-here",
        "<paste-your-key-here>",
        "dummy-key-value",
        "example-secret",
        "TODO",
        "xxxxxxxxxxxxxxxxxxxx",
        "aaaaaaaaaaaaaaaaaaaa",  # one character repeated
        "AKFAKE7QZ2X9R UNITTEST",  # a space
        "AKFAKE7QZ2X9R\tUNITTEST",
        "",
    ],
)
def test_placeholder_detector_fires(value):
    assert looks_like_placeholder(value), value
    assert placeholder_reason(value)


def test_reason_never_quotes_the_value_back():
    """The reason is logged, so it must not become a way to print the credential."""
    value = "placeholder-alpaca-key-9TZQ7R2X"

    reason = placeholder_reason(value)

    assert reason
    assert value not in reason
    assert "9TZQ7R2X" not in reason


@pytest.mark.parametrize(
    "value",
    [
        DUMMY_KEY_FROM_SYSTEMD,
        DUMMY_SECRET_FROM_SYSTEMD,
        DUMMY_KEY_FROM_ENV,
        DUMMY_SECRET_FROM_ENV,
        "PK7R2QZ9XMLV3TBN8WCD",
        "aB3dE6gH9jK2mN5pQ8rS1tU4vW7xY0zA3bC6dE9f",
    ],
)
def test_placeholder_detector_does_not_fire_on_realistic_shapes(value):
    assert not looks_like_placeholder(value), placeholder_reason(value)


def test_detector_uses_no_length_or_prefix_rule():
    """Alpaca publishes no key format, so the detector must not invent one.

    A short value and a long value that carry no stand-in marker must both pass;
    otherwise this check would start rejecting real keys the day Alpaca changes
    its issuing format.
    """
    assert not looks_like_placeholder("aB3dE6")
    assert not looks_like_placeholder("aB3dE6gH9jK2mN5pQ8rS1tU4vW7xY0zA3bC6dE9fG2hJ5k")


# ---------------------------------------------------------------------------
# Startup reporting
# ---------------------------------------------------------------------------

class _ApiKeys:
    def __init__(self, alpaca_key, alpaca_secret):
        self.alpaca_key = alpaca_key
        self.alpaca_secret = alpaca_secret


def test_describe_delivery_never_reveals_a_value():
    api_keys = _ApiKeys(DUMMY_KEY_FROM_SYSTEMD, DUMMY_SECRET_FROM_SYSTEMD)

    facts, problems = describe_delivery({"ALPACA_API_KEY": DUMMY_KEY_FROM_SYSTEMD}, api_keys)

    joined = " ".join(facts + problems)
    assert DUMMY_KEY_FROM_SYSTEMD not in joined
    assert DUMMY_SECRET_FROM_SYSTEMD not in joined
    assert "systemd credential" in facts[0]
    assert "environment (.env)" in facts[1]
    assert str(len(DUMMY_KEY_FROM_SYSTEMD)) in facts[0]
    assert problems == []


def test_report_startup_credentials_shouts_about_a_placeholder(monkeypatch, caplog, tmp_path):
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    api_keys = _ApiKeys("placeholder-alpaca-key", DUMMY_SECRET_FROM_SYSTEMD)
    logger = logging.getLogger("credential-test")

    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))

    with caplog.at_level(logging.INFO, logger="credential-test"):
        problems = report_startup_credentials(
            api_keys, logger=logger, state_path=tmp_path / "marker.json",
        )

    assert len(problems) == 1
    assert "alpaca_key" in problems[0]
    assert "PLACEHOLDER CREDENTIAL" in caplog.text
    # ONE message, not one per problem.
    assert len(sent) == 1
    assert "placeholder-alpaca-key" not in caplog.text
    assert "placeholder-alpaca-key" not in sent[0]


def test_report_startup_credentials_is_quiet_when_both_look_real(monkeypatch, caplog, tmp_path):
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    api_keys = _ApiKeys(DUMMY_KEY_FROM_SYSTEMD, DUMMY_SECRET_FROM_SYSTEMD)
    logger = logging.getLogger("credential-test-quiet")

    with caplog.at_level(logging.INFO, logger="credential-test-quiet"):
        problems = report_startup_credentials(
            api_keys, logger=logger, state_path=tmp_path / "marker.json",
        )

    assert problems == []
    assert "PLACEHOLDER CREDENTIAL" not in caplog.text


def test_alert_failure_never_blocks_startup(monkeypatch, tmp_path):
    """A broken notification path must not stop the desk — it is the wrong reason to."""
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)

    def _explode(text, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("src.notifier.send_owner_alert", _explode)
    api_keys = _ApiKeys("placeholder-alpaca-key", "placeholder-alpaca-secret")

    problems = report_startup_credentials(
        api_keys,
        logger=logging.getLogger("credential-test-fail"),
        state_path=tmp_path / "marker.json",
    )

    assert len(problems) == 2


# ---------------------------------------------------------------------------
# alert volume — the reason this is rationed at all
# ---------------------------------------------------------------------------

def test_both_placeholders_produce_one_message_not_two(monkeypatch, tmp_path):
    """Two placeholder credentials must not mean two Telegram messages.

    `main.py --mode <session>` is the entrypoint for all six session units, so
    one message per problem per start is roughly a dozen a day about a condition
    that is the intended state. That is how a channel becomes unreadable — which
    is the very reason the eight-day placeholder went unnoticed.
    """
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))

    problems = report_startup_credentials(
        _ApiKeys("placeholder-alpaca-key", "placeholder-alpaca-secret"),
        logger=logging.getLogger("credential-test-volume"),
        state_path=tmp_path / "marker.json",
    )

    assert len(problems) == 2
    assert len(sent) == 1
    assert "alpaca_key" in sent[0] and "alpaca_secret" in sent[0]


def test_repeat_starts_on_the_same_day_log_but_do_not_push(monkeypatch, tmp_path, caplog):
    """Six session starts a day are six log lines and ONE push."""
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))
    marker = tmp_path / "marker.json"
    logger = logging.getLogger("credential-test-repeat")

    with caplog.at_level(logging.INFO, logger="credential-test-repeat"):
        for _ in range(6):
            report_startup_credentials(
                _ApiKeys("placeholder-alpaca-key", DUMMY_SECRET_FROM_SYSTEMD),
                logger=logger,
                state_path=marker,
            )

    assert len(sent) == 1
    assert caplog.text.count("PLACEHOLDER CREDENTIAL") == 6


def test_the_next_day_pushes_again(monkeypatch, tmp_path):
    """The condition is re-stated daily while it is still true, not silenced forever."""
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))
    marker = tmp_path / "marker.json"
    api_keys = _ApiKeys("placeholder-alpaca-key", DUMMY_SECRET_FROM_SYSTEMD)
    logger = logging.getLogger("credential-test-nextday")

    monkeypatch.setattr("src.credentials._today", lambda: date(2026, 9, 18))
    report_startup_credentials(api_keys, logger=logger, state_path=marker)
    report_startup_credentials(api_keys, logger=logger, state_path=marker)
    assert len(sent) == 1

    monkeypatch.setattr("src.credentials._today", lambda: date(2026, 9, 19))
    report_startup_credentials(api_keys, logger=logger, state_path=marker)
    assert len(sent) == 2


def test_a_failed_push_is_not_recorded_as_sent(monkeypatch, tmp_path):
    """A notification that never left must not silence tomorrow's — or today's next."""
    monkeypatch.delenv(CREDENTIALS_DIRECTORY_ENV, raising=False)
    attempts: list[str] = []

    def _explode(text, **kwargs):
        attempts.append(text)
        raise RuntimeError("telegram down")

    monkeypatch.setattr("src.notifier.send_owner_alert", _explode)
    marker = tmp_path / "marker.json"
    api_keys = _ApiKeys("placeholder-alpaca-key", DUMMY_SECRET_FROM_SYSTEMD)
    logger = logging.getLogger("credential-test-failpush")

    report_startup_credentials(api_keys, logger=logger, state_path=marker)
    report_startup_credentials(api_keys, logger=logger, state_path=marker)

    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# the word list is matched on word boundaries, not as raw substrings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "AKFZ8XTODOI3XQ",     # contains "todo" by chance
        "PKINSERT9QZ2X4LM",   # contains "insert" by chance
        "AKXXXXQ7ZM2V9LD4",   # contains "xxxx" by chance
        "PK7ZQ3M2V9LD4WX1",
    ],
)
def test_placeholder_words_do_not_fire_inside_an_opaque_key(value):
    """A real key is an opaque run of characters; a stand-in word can appear in one.

    Firing on a raw substring made the detector shout "placeholder" at a working
    credential. It cannot block anything, but an unmeasured false-positive word
    list in an alert path is how alerts stop being read.
    """
    assert placeholder_reason(value) is None, value


@pytest.mark.parametrize(
    "value",
    [
        "TODO",
        "todo-replace-this",
        "ALPACA_KEY_INSERT_HERE",
        "XXXX",
        "placeholder-alpaca-key",
        "CHANGE_ME",
        "YOUR-KEY-HERE",
    ],
)
def test_placeholder_words_still_fire_as_whole_words(value):
    assert placeholder_reason(value), value
