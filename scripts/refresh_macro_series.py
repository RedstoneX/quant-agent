#!/usr/bin/env python3
"""Fetch every FRED series into the on-disk cache, ahead of the open.

WHY THIS RUNS ON A TIMER — board item 119
-----------------------------------------
Measured in the production log (`/home/qamc/quant-agent/quant_agent.log`):

* The morning session's macro stage starts at **09:30:49 ET** — 49 seconds
  after the opening bell (13:30:48.283 / 13:30:49.653 / 13:30:49.386 UTC on
  2026-09-18, 09-21, 09-22).
* On 2026-09-22 that fetch began 13:30:49.4 UTC and hit its 90-second ceiling
  at 13:32:19.39 UTC, consuming the budget exactly. Eight of fifteen series
  were skipped with **no attempt at all**; coverage was 7 of 15.
* The 90-second ceiling was fully exhausted in SEVEN distinct runs in the
  retained log — and only ONE of those was the morning. Three were midday
  (17:02 UTC = 13:02 ET) and three were evening (00:02 UTC = 20:02 ET). The
  board framed item 119 as open starvation; the log says the fetch starves at
  every session, because 30 requests do not fit a 90-second ceiling at any
  hour. The open is only where it costs the most.
* Coverage over twelve recorded runs: complete on four, partial on eight
  (5, 6, 7, 7, 8, 11, 11, 11 of 15).

None of this data needs to be fetched at 09:30:49. CPI and PCE are monthly,
the unemployment rate is monthly, initial claims are weekly, and the daily
rate series come from a release that posts at 16:15 ET. The desk was reading
slow-moving statistics under intraday deadline pressure for no reason, and
that pressure is the whole defect.

So this job does the fetching, one series at a time, with no session waiting
on it, and the sessions read what it left behind. It fires twice a weekday —
08:45 ET (after the 08:30 BLS release slot, before the 09:30:49 morning read)
and 18:30 ET (after the 16:15 H.15 post, before the 20:00 evening session) —
so a cached entry is never asked to survive a publication boundary. See
`scripts/systemd/quant-agent-macro-prefetch.timer` for both derivations.

NOT ROUTED THROUGH run_if_et_window.sh, on purpose. That wrapper is for the
six ET-windowed trading sessions: it takes the cross-session lock, honours a
once-per-day marker, and imposes a 1,200-second outer timeout. This job holds
no trading state, must not block or be blocked by a session, and is deliberately
slow. Same reasoning, and the same shape, as `scripts/refresh_pricing.py`.

WHAT IT WRITES
--------------
`data/macro/series_cache/<SERIES>.json`, one file per series, each written
tmp-file-then-`os.replace` so a session reading at the same instant sees the
old file or the new one and never a half-written one. `data/` is gitignored,
so a refresh can never dirty the checkout.

EXIT STATUS
-----------
0 when every configured series landed. 1 when any did not — the cache is then
incomplete and the next session falls back to fetching the missing ones live,
which is exactly today's behaviour, so this is a degradation worth seeing in
`systemctl --user status` rather than an outage.

USAGE
-----
    python scripts/refresh_macro_series.py
    python scripts/refresh_macro_series.py --quiet
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.data.macro import MacroDataProvider  # noqa: E402

logger = logging.getLogger("refresh_macro_series")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Log warnings and above only. The unit passes this.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(Path(args.config))
    macro_cfg = config.macro
    provider = MacroDataProvider(
        api_key=config.api_keys.fred,
        request_timeout_s=macro_cfg.request_timeout_s,
        max_retries=macro_cfg.max_retries,
        retry_backoff_base_s=macro_cfg.retry_backoff_base_s,
        retry_backoff_max_s=macro_cfg.retry_backoff_max_s,
        retry_backoff_jitter_s=macro_cfg.retry_backoff_jitter_s,
        breaker_after_failed_series=macro_cfg.breaker_after_failed_series,
        total_fetch_deadline_s=macro_cfg.total_fetch_deadline_s,
    )

    coverage = provider.prefetch_series_cache()
    if coverage is None:
        logger.error("FRED prefetch recorded no coverage — cache not refreshed")
        return 1
    print(coverage.describe())
    if not coverage.complete:
        logger.warning(
            "FRED prefetch incomplete — the next session will fetch the "
            "missing series live, exactly as it did before this job existed"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
