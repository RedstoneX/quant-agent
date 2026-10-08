"""Socket-level network guard and the offline-provider fixtures that keep it green.

Imported into tests/conftest.py by name; this file is where they LIVE so that
conftest.py stays small.
"""
import os
import socket
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# The network is CLOSED for every test, at the socket layer.
#
# The library-level wall above (`requests`, `curl_cffi`) only blocks the
# transports somebody thought of. Measured 2026-10-01 with a socket-level
# journal over the full suite: 17 tests still reached the wire through
# `urllib.request.urlopen` -- fredapi (api.stlouisfed.org), the Fed's own
# calendar (www.federalreserve.gov) and ten RSS/reference hosts -- and every
# one of them PASSED, because the code under test degrades a fetch failure
# by design. That is the exact shape of a red CI check that has nothing to
# do with the diff: the test is green or red depending on whether a provider
# answered. (tests/test_shorts_stage3.py went red on Yahoo's 401 the same way
# on 2026-09-30.)
#
# So this guard is installed where every transport converges -- the same
# three socket calls `ops/rehearsal/isolation.no_network` guards -- and it
# FAILS THE TEST AT TEARDOWN whether or not the code under test swallowed the
# error. Loopback is allowed (tests/test_api_isolation.py binds a local
# port). Nothing is allow-listed: the 17 were fixed at their seam, so a new
# test that reaches the wire fails on arrival, naming itself and the host.
# Stub the provider where the pipeline BUILDS it (`src.pipeline.<Provider>`,
# see `offline_calendars` below) or the fetch method the provider already
# exposes for tests (`NewsDataProvider._fetch_feed`,
# `MacroEventCalendarProvider._fetch_release_dates`).
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    return isinstance(host, str) and host in ("127.0.0.1", "::1", "localhost", "")


@pytest.fixture(autouse=True)
def _no_sockets_leave_the_box(monkeypatch, request):
    attempts: list[str] = []
    nodeid = request.node.nodeid

    def _blocked(address, what: str):
        target = (
            f"{address[0]}:{address[1]}"
            if isinstance(address, tuple) and len(address) > 1
            else str(address)
        )
        line = f"{what} to {target}"
        if line not in attempts:
            attempts.append(line)
        journal = os.environ.get("QAMC_NETWORK_JOURNAL")
        if journal:
            try:
                with open(journal, "a", encoding="utf-8") as fh:
                    fh.write(f"{nodeid}\t{target}\n")
            except OSError:
                pass  # journalling must never change what the suite does
        raise OSError(
            f"the test suite is offline: {nodeid} tried {line}. "
            "A unit test must not reach the network; stub the provider at the "
            "seam the pipeline builds it from (see tests/conftest.py)."
        )

    def guarded_connect(self, address):
        if _is_loopback(address):
            return _REAL_SOCKET_CONNECT(self, address)
        _blocked(address, "socket.connect")

    def guarded_connect_ex(self, address):
        if _is_loopback(address):
            return _REAL_SOCKET_CONNECT_EX(self, address)
        _blocked(address, "socket.connect_ex")

    def guarded_create_connection(address, *args, **kwargs):
        if _is_loopback(address):
            return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
        _blocked(address, "socket.create_connection")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield
    if attempts:
        pytest.fail(
            f"{nodeid} reached for the network ({len(attempts)} distinct "
            f"target(s)): " + "; ".join(attempts) + ". The attempt was blocked, "
            "but a test whose result depends on a provider answering is not a "
            "unit test. Stub the provider at its seam.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def offline_calendars(monkeypatch):
    """Replace the two forward-calendar feeds where the pipeline BUILDS them.

    `TradingPipeline.__init__` constructs `MacroEventCalendarProvider` (FRED
    release dates) and `FOMCCalendarProvider` (federalreserve.gov) itself, so
    a test that patches `src.pipeline.MacroDataProvider` and
    `src.pipeline.NewsDataProvider` but forgets these two still goes to the
    wire -- 14 tests in four files did (measured 2026-10-01). Autouse, like
    the sector default above: no test anywhere reads the pipeline-built
    calendars, and the provider classes themselves are tested directly from
    src.data.event_calendar, which this never touches. The research stage
    sees an empty schedule with no coverage, which is how it already reads a
    feed that was never fetched.
    """
    from src import pipeline as _pipeline

    def _calendar_stub(*args, **kwargs):
        stub = MagicMock(name="offline_calendar")
        stub.get_upcoming_events.return_value = []
        stub.get_meetings.return_value = []
        stub.last_coverage = None
        return stub

    monkeypatch.setattr(_pipeline, "MacroEventCalendarProvider", _calendar_stub)
    monkeypatch.setattr(_pipeline, "FOMCCalendarProvider", _calendar_stub)
