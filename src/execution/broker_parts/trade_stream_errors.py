"""HTTP-status and Retry-After readers for the trade_updates stream, split out of trade_stream.py."""

from __future__ import annotations

import re


def _stream_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status on a websocket handshake error. Never raises."""
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int):
                return val
    match = re.search(r"\bHTTP\s*429\b|\bstatus(?:\s+code)?\s*[:=]?\s*429\b", str(exc), re.IGNORECASE)
    if match:
        return 429
    return None


def _stream_retry_after_seconds(exc: BaseException) -> float | None:
    """Retry-After from a handshake 429, when the server sent one.

    Numeric seconds only (same restriction as `_retry_after_hint_seconds`
    in src/agents/base.py). The HTTP-date form is not worth parsing here:
    the fill wait already has its own wall-clock ceiling.
    """
    sources = [exc, getattr(exc, "response", None)]
    for src in sources:
        if src is None:
            continue
        headers = getattr(src, "headers", None)
        if headers is None:
            continue
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            continue
        try:
            hint = float(raw)
        except (TypeError, ValueError):
            continue
        if hint > 0:
            return hint
    match = re.search(
        r'retry[_-]after["\']?\s*[:=]\s*"?(\d+(?:\.\d+)?)',
        str(exc),
        re.IGNORECASE,
    )
    if match:
        hint = float(match.group(1))
        if hint > 0:
            return hint
    return None
