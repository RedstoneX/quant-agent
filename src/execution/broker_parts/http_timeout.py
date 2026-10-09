"""Default HTTP timeout for Alpaca SDK clients, lifted verbatim from
src/execution/broker_parts/market_data.py (re-exported there and by
src.execution.broker)."""

from __future__ import annotations

from src.infra_retry_policy import _BROKER_HTTP_TIMEOUT  # noqa: F401 (re-export)


def _install_http_timeout(client, timeout: float | None = None) -> None:
    """Inject a default timeout on an Alpaca SDK client's underlying requests.Session.

    The SDK (alpaca-py 0.43.2) uses a requests.Session with no default timeout; each
    call goes through RESTClient._one_request which just forwards opts. This patches
    session.request to set timeout=30s if the caller didn't specify one.
    """
    if timeout is None:  # the policy's ledgered default, read at call time
        timeout = _BROKER_HTTP_TIMEOUT
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_quant_timeout_patched", False):
        return
    original_request = session.request

    def _request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = _request_with_timeout
    session._quant_timeout_patched = True
