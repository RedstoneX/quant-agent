"""A replay of a recorded session must reach NO network provider at all.

Board item 202. The rehearsal harness has claimed to be offline since it was
written, and twice that claim turned out to be false in a way nothing caught:
`yfinance` ships its own libcurl transport that never calls
`socket.socket.connect`, and the one stage that mattered kept its own
reference to the live provider after the swap. Both times the run went green.

The lesson is that "offline" cannot be a property anybody asserts in a
docstring. It has to be MEASURED by the run itself and it has to be FATAL:

  * `no_network()` journals every outbound attempt, which is what survives the
    broad `except` clauses every HTTP client in this dependency set uses to
    retry or degrade. A blocked call used to end as an empty result.
  * `assert_hermetic()` turns a non-empty journal into a failed run, with no
    opt-out of any kind.
  * A missing recorded input is fatal too, because a replay that fills a gap
    with an invented value gives a confident wrong answer.

`test_every_http_transport_this_repo_can_reach_for_is_journalled` is the part
that cannot rot: it exercises each HTTP client installed in this environment
and fails if the wall did not SEE the attempt. A new dependency with a C
transport breaks it on the day it is added, rather than years later in a
rehearsal nobody doubted.
"""

from __future__ import annotations

import importlib
import socket

import pytest

from ops.rehearsal.isolation import (
    HermeticBreach,
    MissingRecordedInput,
    assert_hermetic,
    no_network,
)

# TEST-NET-1 (RFC 5737): reserved for documentation, routed nowhere, so
# nothing here can reach anything even if the wall were removed. A literal
# address and not a hostname on purpose — a `.invalid` name fails at DNS
# resolution inside urllib3, BEFORE the connect the wall intercepts, which
# would make this test pass without the wall doing anything. The assertion
# under test is not "the call failed" (it would fail either way) but "the
# wall SAW it". Name resolution itself is left alone: it carries no provider
# data and blocking it breaks unrelated tooling, exactly as loopback is.
BLOCKED_HOST = "192.0.2.1"
BLOCKED_URL = f"http://{BLOCKED_HOST}:81/bars"

# Captured at IMPORT time, before `tests/conftest.py`'s own autouse outbound
# guard replaces them for the duration of each test. That guard is a second,
# independent wall and it works — but it would answer the call before the
# REHEARSAL wall ever saw it, and the rehearsal wall is what is under test
# here. Put the real callables back for the duration of one exercise.
import requests as _requests  # noqa: E402

_REAL_REQUESTS = {
    "get": _requests.get,
    "post": _requests.post,
    "request": _requests.request,
}
_REAL_SESSION_REQUEST = _requests.Session.request


def test_a_clean_run_reports_that_it_was_offline_and_complete():
    note = assert_hermetic([], [])
    assert "no outbound connection was attempted" in note
    assert "came from the recording" in note


def test_an_outbound_attempt_voids_the_replay_even_when_the_caller_swallows_it():
    # The caller swallowing NetworkBlocked is the normal case, not an edge
    # case: yfinance, requests and the Alpaca SDK all catch broadly and
    # retry. The journal is the only evidence left, so it must be fatal.
    journal = []
    with no_network(journal):
        try:
            socket.create_connection((BLOCKED_HOST, 81), timeout=1)
        except Exception:
            pass  # exactly what a provider SDK does
    assert journal, "the wall did not journal a blocked connection"

    with pytest.raises(HermeticBreach) as excinfo:
        assert_hermetic(journal, [])
    message = str(excinfo.value)
    assert "NOT hermetic" in message
    assert BLOCKED_HOST in message


def test_allow_degraded_never_relaxes_the_network_wall():
    with pytest.raises(HermeticBreach):
        assert_hermetic(["socket.connect to data.example:443"], [], allow_degraded=True)


def test_a_missing_recorded_input_stops_the_replay_instead_of_being_filled_in():
    with pytest.raises(MissingRecordedInput) as excinfo:
        assert_hermetic([], ["recorded daily bars for SPY"])
    message = str(excinfo.value)
    assert "recorded daily bars for SPY" in message
    assert "never invents a value" in message


def test_an_explicitly_degraded_run_says_so_and_still_invents_nothing():
    note = assert_hermetic([], ["recorded daily bars for SPY"], allow_degraded=True)
    assert "accepted as degraded" in note
    assert "nothing was invented" in note


# --------------------------------------------------------------- rot guard

# Every HTTP client this repo could plausibly reach for. A transport that is
# not installed is skipped; one that IS installed must be journalled. The
# list is deliberately wider than what `requirements.txt` holds today so a
# new dependency is covered the moment it appears.
TRANSPORTS = {
    "urllib.request": lambda m: m.urlopen(BLOCKED_URL, timeout=1),
    "http.client": lambda m: m.HTTPConnection(BLOCKED_HOST, 81, timeout=1).request("GET", "/"),
    "requests": lambda m: m.get(BLOCKED_URL, timeout=1),
    "curl_cffi.requests": lambda m: m.get(BLOCKED_URL, timeout=1),
    "httpx": lambda m: m.get(BLOCKED_URL, timeout=1),
    "aiohttp": None,  # async; covered by the socket layer it ultimately uses
    "pycurl": None,  # not installed here; listed so its arrival is noticed
}


@pytest.mark.parametrize("module_name", sorted(TRANSPORTS))
def test_every_http_transport_this_repo_can_reach_for_is_journalled(
    module_name,
    monkeypatch,
):
    exercise = TRANSPORTS[module_name]
    if module_name == "requests":
        for attr, real in _REAL_REQUESTS.items():
            monkeypatch.setattr(_requests, attr, real)
        monkeypatch.setattr(_requests.Session, "request", _REAL_SESSION_REQUEST)
    try:
        module = importlib.import_module(module_name)
    except Exception:
        pytest.skip(f"{module_name} is not installed in this environment")
    if exercise is None:
        pytest.skip(
            f"{module_name} is installed but has no synchronous one-liner here; "
            "it reaches the network through a transport covered above"
        )

    journal = []
    with no_network(journal):
        try:
            exercise(module)
        except Exception:
            pass
    assert journal, (
        f"{module_name} left this process WITHOUT the rehearsal wall seeing it. "
        "A rehearsal using it would download live data through a wall that "
        "reports itself intact — exactly the defect board item 202 was filed "
        "for. Block this transport in ops/rehearsal/isolation.no_network and "
        "in tests/conftest.py, which has the identical wall."
    )


def test_the_runner_enforces_hermeticity_rather_than_only_reporting_it():
    # The journal existed before this item and was printed in the report as
    # narrative while the run still returned a verdict. Pin the enforcement
    # so it cannot quietly go back to being advisory.
    import inspect

    from ops.rehearsal import runner

    source = inspect.getsource(runner.run_rehearsal)
    assert "assert_hermetic(" in source, (
        "run_rehearsal no longer asserts hermeticity; a replay that reached a "
        "live provider would return a verdict again (board item 202)"
    )
    assert "raise hermetic_breach" in source, "run_rehearsal collected the breach but no longer raises it"


def test_the_cli_turns_a_breach_into_a_void_run_not_a_pass(monkeypatch, tmp_path):
    from ops.rehearsal import run as run_module

    class _Sandbox:
        @staticmethod
        def prepare(**kwargs):
            return object()

    def _boom(*args, **kwargs):
        raise HermeticBreach("this replay was NOT hermetic: 1 outbound connection attempt(s)")

    monkeypatch.setattr("ops.rehearsal.isolation.Sandbox", _Sandbox)
    monkeypatch.setattr("ops.rehearsal.runner.run_rehearsal", _boom)

    code = run_module.main(
        [
            "--source-db",
            str(tmp_path / "nonexistent.db"),
            "--sandbox",
            str(tmp_path / "sbx"),
        ]
    )
    assert code == 2, "a non-hermetic replay must not exit 0"
