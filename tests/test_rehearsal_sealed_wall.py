"""Board item 202: the rehearsal wall is sealed on every route off the box.

The wall used to cover `socket.connect`, `create_connection` and curl_cffi's
module-level `get`/`post`/`request`/`Session.request`. Measured 2026-10-02
against TEST-NET-1 (a literal address, so the attempt reaches the wall rather
than failing at DNS): UDP `sendto`/`sendmsg`, curl_cffi's `AsyncSession` and
raw `Curl.perform`, and a `curl` subprocess all left the process with an EMPTY
journal, and `getaddrinfo` resolved off-box names unjournalled. A replay that
reaches any of them is a fresh live session wearing a recording's label.
"""

import socket
import subprocess

import pytest

from ops.rehearsal.feed_recording import recorded_feeds
from ops.rehearsal.isolation import NetworkBlocked, no_network

OFF_BOX = "192.0.2.1"  # RFC 5737 TEST-NET-1


def _udp_sendto():
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", (OFF_BOX, 53))


def _udp_sendmsg():
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, (OFF_BOX, 53))


def _resolve():
    socket.getaddrinfo("provider.example.invalid", 443)


def _curl_subprocess():
    subprocess.run(["curl", "-m", "1", f"http://{OFF_BOX}"], capture_output=True)


def _curl_cffi_perform():
    from curl_cffi import Curl

    handle = Curl()
    handle.setopt(10002, f"http://{OFF_BOX}".encode())
    handle.setopt(13, 1)
    handle.perform()


def _curl_cffi_async():
    import asyncio

    from curl_cffi.requests import AsyncSession

    async def go():
        async with AsyncSession() as session:
            await session.get(f"http://{OFF_BOX}", timeout=1)

    asyncio.run(go())


ROUTES = {
    "udp sendto": _udp_sendto,
    "udp sendmsg": _udp_sendmsg,
    "name resolution": _resolve,
    "curl subprocess": _curl_subprocess,
    "curl_cffi Curl.perform": _curl_cffi_perform,
    "curl_cffi AsyncSession": _curl_cffi_async,
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_route_off_the_box_fails_loudly_and_is_journalled(route):
    pytest.importorskip("curl_cffi")
    journal = []
    with no_network(journal):
        with pytest.raises(NetworkBlocked) as raised:
            ROUTES[route]()
    assert journal, f"{route} left the process without the wall journalling it"
    assert "outbound connection" in str(raised.value)


def test_a_replay_with_the_network_unavailable_still_completes():
    # Loopback is the one thing the wall allows, and the recorded feeds are
    # served in-process: an offline replay needs nothing else.
    journal, missing = [], []
    with no_network(journal), recorded_feeds(missing, {"fred_series": {}, "http": {}}):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        probe.sendto(b"x", probe.getsockname())
        assert probe.recvfrom(8)[0] == b"x"
        assert socket.getaddrinfo("localhost", 80)
    assert journal == []


def test_an_unrecorded_call_fails_instead_of_fetching():
    from urllib.request import urlopen

    journal, missing = [], []
    with no_network(journal), recorded_feeds(missing, {"fred_series": {}, "http": {}}):
        with pytest.raises(Exception):
            import src.data.news as news

            news.urlopen("https://feeds.example.invalid/unrecorded.xml", timeout=1)
    assert journal == [], f"an unrecorded call reached the network: {journal}"
    assert missing, "an unrecorded call was not named as a missing recorded input"
