"""Reconnect and handshake time bounds for the `trade_updates` socket, lifted verbatim from trade_stream.py."""

from __future__ import annotations

# Fallback reconnect bounds, used only when the installed TradingStream
# does not expose `_reconnect_min_backoff` / `_reconnect_max_backoff`.
# They are alpaca-py's own values for this exact storm (TradingStream.__init__
# on versions that shipped `reconnect_delay`), not a desk-invented constant.
_ALPACA_STREAM_RECONNECT_MIN_S = 1.0
_ALPACA_STREAM_RECONNECT_MAX_S = 30.0

# Handshake ceiling for trade_updates auth. Reuses alpaca-py's own reconnect
# max so we do not invent a second clock. Auth must not consume a fill /
# funding timeout (measured 2026-09-16: ~4 min of `_auth` retries after
# Risk approved, before the first BUY hit the tape).
_ALPACA_STREAM_AUTH_DEADLINE_S = _ALPACA_STREAM_RECONNECT_MAX_S
