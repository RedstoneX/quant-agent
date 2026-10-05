"""Share-quantity formatting (lifted from pipeline_sizing)."""
from __future__ import annotations

import math


def _fmt_shares(qty: float) -> str:
    """Render a share count for a human without a spurious `.0` on a whole
    number or a wall of trailing zeros on a fractional one."""
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return str(qty)
    if value.is_integer():
        return str(int(value))
    return f"{value:.9f}".rstrip("0").rstrip(".")
