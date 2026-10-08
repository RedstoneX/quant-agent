"""Default HTTP timeout for Alpaca SDK clients, lifted verbatim from
src/execution/broker_parts/market_data.py (re-exported there and by
src.execution.broker)."""
from __future__ import annotations

# Default HTTP timeout for ALL Alpaca SDK calls (connect, read).
# Without this, a stalled TCP connection to the broker can hang the process
# for hours under launchd — observed 2026-04-17 when the evening job sat for
# 13+ hours at the very first broker call.
_BROKER_HTTP_TIMEOUT = 30.0


def _install_http_timeout(client, timeout: float = _BROKER_HTTP_TIMEOUT) -> None:
    """Inject a default timeout on an Alpaca SDK client's underlying requests.Session.

    The SDK (alpaca-py 0.43.2) uses a requests.Session with no default timeout; each
    call goes through RESTClient._one_request which just forwards opts. This patches
    session.request to set timeout=30s if the caller didn't specify one.
    """
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_quant_timeout_patched", False):
        return
    original_request = session.request

    def _request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = _request_with_timeout
    session._quant_timeout_patched = True
