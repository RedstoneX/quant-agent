"""Standalone launcher: `python -m src.api` (or `python -m src.api.server`
via uvicorn's own CLI, e.g. `uvicorn src.api.server:app`).

Deliberately separate from `main.py` — the trading entry point has zero
awareness this file exists. Binds to loopback by default: DECISIONS.md #29 /
docs/architecture/SYSTEM_ARCHITECTURE.md both say initial remote access
should prefer Tailscale/private networking over exposing a public bind, so
the safe default here is `127.0.0.1`, not `0.0.0.0`. Override via env vars
if a private-network bind is genuinely wanted.
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("QUANT_AGENT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("QUANT_AGENT_API_PORT", "8800"))
    uvicorn.run("src.api.server:app", host=host, port=port)


if __name__ == "__main__":
    main()
