"""Live fetcher for the Nasdaq listing documents (once per run, public data).

Isolated in its own module so the outbound site is one file; the parse is
covered by hermetic fixtures in `nasdaq_listing`. Pass as
`load_listing(fetch=fetch_json)`.
"""

from __future__ import annotations

import requests


def fetch_json(url: str, headers: dict) -> dict:
    # 30 s: generous for a ~MB document fetched twice a day at most; inline, not a module constant.
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()
