"""The rehearsal's network wall, split out of isolation.py (board item 202).

Every route a replay could use to leave the process is closed here: socket
connect, UDP send, name resolution, libcurl (sync and async), and the spawn
of a network-only executable. An attempt is journalled and raises
`NetworkBlocked`, which names what was tried.
"""

import os
import socket
from contextlib import contextmanager


class NetworkBlocked(OSError):
    """A rehearsal tried to open an off-box socket."""



_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_SENDTO = socket.socket.sendto
_REAL_SENDMSG = socket.socket.sendmsg
_REAL_GETADDRINFO = socket.getaddrinfo

# Executables whose whole purpose is to open a connection. A subprocess is a
# separate process: no patch in this one can see its sockets, so the only
# place to stop it is the spawn. This list is NAMED, not exhaustive — it is
# the one route the wall cannot close by construction, and is said so here.
_NETWORK_EXECUTABLES = frozenset({
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync",
    "ftp", "telnet", "socat",
})


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    return host in ("127.0.0.1", "::1", "localhost", "")


def _is_ip_literal(host) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False


@contextmanager
def no_network(record: list[str] | None = None):
    """Block every off-box socket connection for the duration of the block.

    Installed at the socket layer deliberately: patching `requests.get`, or an
    SDK's transport, or an environment variable only blocks the paths you
    thought of. Anthropic, OpenAI, OpenRouter, Alpaca, yfinance, FRED and
    feedparser all reach the network eventually through this one call.

    Anything blocked is appended to `record`, so the report can say which
    component tried and the operator can judge whether the resulting
    degradation invalidates the rehearsal.
    """
    blocked = record if record is not None else []

    def _blocked(address, what: str):
        target = f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) > 1 else str(address)
        message = f"{what} to {target}"
        if message not in blocked:
            blocked.append(message)
        raise NetworkBlocked(
            f"rehearsal blocked an outbound connection ({message}); a rehearsal "
            f"is offline by construction — no provider call, no market data "
            f"fetch and no broker request may leave this process"
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

    def guarded_sendto(self, data, *args):
        # sendto(data, address) or sendto(data, flags, address): address last.
        address = args[-1] if args else None
        if address is None or _is_loopback(address):
            return _REAL_SENDTO(self, data, *args)
        _blocked(address, "socket.sendto")

    def guarded_sendmsg(self, buffers, *args):
        address = args[2] if len(args) > 2 else None
        if address is None or _is_loopback(address):
            return _REAL_SENDMSG(self, buffers, *args)
        _blocked(address, "socket.sendmsg")

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        # Resolving an off-box NAME is an outbound DNS query. IP literals
        # resolve nothing off-box; the connect that follows is what is blocked.
        if isinstance(host, bytes):
            host = host.decode("ascii", "ignore")
        if host is None or host == "" or host == "localhost" or _is_ip_literal(host):
            return _REAL_GETADDRINFO(host, port, *args, **kwargs)
        _blocked((host, port), "socket.getaddrinfo")

    import subprocess as _subprocess

    _real_popen_init = _subprocess.Popen.__init__

    def guarded_popen_init(self, args, *pargs, **pkwargs):
        argv = args if isinstance(args, (list, tuple)) else [args]
        first = str(argv[0]).split()[0] if argv else ""
        if os.path.basename(first) in _NETWORK_EXECUTABLES:
            _blocked((os.path.basename(first), 0), "subprocess")
        return _real_popen_init(self, args, *pargs, **pkwargs)

    # The socket layer is NOT the only way out, and the docstring above was
    # wrong about yfinance for as long as this wall has existed (board item
    # 202). yfinance ships its own transport on `curl_cffi`, which is libcurl
    # in C: it never calls `socket.socket.connect`, so every rehearsal went on
    # downloading live prices through a wall that reported itself intact
    # (~196s of fetching, measured 2026-09-30). The test suite's own outbound
    # guard had the identical hole and was closed the same day in
    # tests/conftest.py; this is that fix, in the same shape.
    restore: list = []

    def _blocked_call(*args, **kwargs):
        _blocked(("curl_cffi", 0), "http request")

    try:
        import curl_cffi.requests as _curl_requests
    except Exception:  # pragma: no cover - absent in a minimal env
        _curl_requests = None
    if _curl_requests is not None:
        for _attr in ("get", "post", "request"):
            if hasattr(_curl_requests, _attr):
                restore.append((_curl_requests, _attr, getattr(_curl_requests, _attr)))
                setattr(_curl_requests, _attr, _blocked_call)
        _curl_session = getattr(_curl_requests, "Session", None)
        if _curl_session is not None:
            restore.append((_curl_session, "request", _curl_session.request))
            _curl_session.request = _blocked_call

    # libcurl entry points BELOW the Session API: `AsyncSession` and a raw
    # `Curl` never pass through `Session.request`, which is all that was
    # patched. `Curl.perform` is the synchronous handle every sync path ends
    # in, and `AsyncCurl.add_handle` is where an async transfer starts.
    if _curl_requests is not None:
        try:
            from curl_cffi.curl import Curl as _Curl

            restore.append((_Curl, "perform", _Curl.perform))
            _Curl.perform = _blocked_call
        except Exception:  # pragma: no cover
            pass
        try:
            from curl_cffi.aio import AsyncCurl as _AsyncCurl

            restore.append((_AsyncCurl, "add_handle", _AsyncCurl.add_handle))
            _AsyncCurl.add_handle = _blocked_call
        except Exception:  # pragma: no cover
            pass

    restore.append((_subprocess.Popen, "__init__", _real_popen_init))
    _subprocess.Popen.__init__ = guarded_popen_init
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.sendto = guarded_sendto
    socket.socket.sendmsg = guarded_sendmsg
    socket.getaddrinfo = guarded_getaddrinfo
    socket.create_connection = guarded_create_connection
    try:
        yield blocked
    finally:
        socket.socket.connect = _REAL_SOCKET_CONNECT
        socket.socket.connect_ex = _REAL_SOCKET_CONNECT_EX
        socket.create_connection = _REAL_CREATE_CONNECTION
        socket.socket.sendto = _REAL_SENDTO
        socket.socket.sendmsg = _REAL_SENDMSG
        socket.getaddrinfo = _REAL_GETADDRINFO
        for _owner, _attr, _original in reversed(restore):
            setattr(_owner, _attr, _original)
