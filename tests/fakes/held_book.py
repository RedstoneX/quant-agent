"""What the broker HOLDS, for a MagicMock broker that otherwise reports nothing.

The protected sell re-reads the position after clearing the stops and sizes
the exit to what is held (a resting stop may have filled in between); the
stop-cancel rollback re-reads it before restoring anything. A bare MagicMock
broker answers `get_positions()` with an empty iterable -- "nothing held" --
so a test that sells or covers must say what the broker holds. Offline.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def hold(broker, positions: dict[str, float]):
    """Make `broker.get_positions()` report `positions` ({symbol: signed qty}); returns `broker`."""
    broker.get_positions = MagicMock(
        return_value=[SimpleNamespace(symbol=s, qty=float(q)) for s, q in positions.items()]
    )
    return broker
