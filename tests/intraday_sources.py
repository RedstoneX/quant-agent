"""Where the intraday bodies live after the parts split (item 210 step 8).

Source-reading tests look here instead of at the shims on `IntradayMixin`.
"""

import inspect

from src.intraday.candidates import IntradayCandidates
from src.pipeline_intraday import IntradayScanBody

#: Parts come FIRST so a by-name search meets a body, not a shim.
PART_MODULES = (
    "pipeline.py",
    "intraday/safety.py",
    "intraday/session.py",
    "intraday/candidates.py",
    "intraday/gating.py",
    "pipeline_intraday.py",
)


def scan_source(*names: str) -> str:
    """Concatenated source of the named intraday bodies, wherever they live."""
    out = []
    for name in names:
        owner = IntradayCandidates if hasattr(IntradayCandidates, name) else IntradayScanBody
        out.append(inspect.getsource(getattr(owner, name)))
    return "".join(out)
