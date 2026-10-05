"""The check on the fill socket's open path, built on both lower layers.

Dependency direction, one way: `src.session_identity` (names the identity and
compares pairs) <- `src.credentials` (resolves this identity's pair) <- here.
"""

from __future__ import annotations

from src.credentials import session_broker_credentials
from src.session_identity import assert_identity_whole


def checked_socket_identity(broker: object) -> str:
    """Refuse, at the moment of opening, a socket that is not this session's.

    The one call site is the `trade_updates` stream's `start`. It is on the
    open path itself rather than in a wrapper or a sandbox-only arm, so there
    is nothing for a future caller to forget: the socket cannot be opened
    without passing through it.
    """
    rest_key, rest_secret = session_broker_credentials()
    return assert_identity_whole(
        rest_key=rest_key,
        rest_secret=rest_secret,
        socket_key=getattr(broker, "api_key", None),
        socket_secret=getattr(broker, "secret_key", None),
    )
