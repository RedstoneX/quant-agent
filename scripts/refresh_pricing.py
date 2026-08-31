#!/usr/bin/env python3
"""Refresh the two on-disk LLM price lists, and shout if the important one
did not land.

WHY THIS RUNS ON A TIMER
------------------------
`data/openrouter_pricing_cache.json` is not telemetry. It is an input to the
mandatory pre-call spending breaker: `refresh_openrouter_pricing()` must be
able to price every accepted OpenRouter model or the circuit fails closed and
suspends paid analysis for the rest of the day. That fail-closed behaviour is
correct and is NOT what this script relaxes.

What was wrong was who refreshed the file. Until this script was scheduled,
the only callers were `TradingPipeline.__init__` and
`activate_paid_call_session` — i.e. the file was only ever refreshed by a
paid session actually starting. So the cache aged exactly when nothing was
watching: over a weekend it passed the 24h freshness window with no session
to notice, and by Monday it was near the 24h + `openrouter_pricing_grace_
period_hours` limit past which the desk opens and trades nothing. Verified
on the live box on 2026-08-30: last refreshed by a real session on 2026-08-28
~13:30 UTC, and only a manual refresh before Monday kept the desk trading.

A timer that runs every day, weekends included, is the fix: the price list
stops going unknown. Nothing about what happens to unknown prices changes.

WHAT IT REFRESHES
-----------------
1. `data/openrouter_pricing_cache.json` — OpenRouter's own catalog. This is
   the load-bearing one: it gates paid calls. Failure here is alertable.
2. `data/pricing_cache.json` — the LiteLLM dataset, used for cost reporting
   on non-OpenRouter-routed models. Nice to have current; it does not gate a
   call, so a failure here is reported and never alerted on.

Note for anyone reading git history: before 2026-08-31 this script refreshed
only (2). Scheduling it as it then stood would not have closed the defect
above, because it never touched the file that stops the desk.

USAGE
-----
    python scripts/refresh_pricing.py            # respects the 24h windows
    python scripts/refresh_pricing.py --force    # always fetches
    python scripts/refresh_pricing.py --no-telegram   # print only

Paths are relative to the working directory, exactly as the trading process
resolves them, so run it from the project root (the systemd unit sets
`WorkingDirectory`).

EXIT CODES
    0  the OpenRouter cache is current (under the freshness window) afterwards
    1  it is missing or already stale afterwards — the desk is now burning
       grace, and an operator alert has been sent
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# Make `src.*` importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from src.cost_table import (  # noqa: E402
    OPENROUTER_CACHE_FRESH_HOURS,
    PRICING,
    litellm_cache_path,
    openrouter_cache_age_hours,
    openrouter_cache_path,
    refresh_openrouter_pricing,
    refresh_pricing,
)


@dataclass
class RefreshOutcome:
    """What the run achieved, judged on the FILE rather than the call.

    `openrouter_call_ok` is what `refresh_openrouter_pricing()` returned.
    That alone is the wrong thing to judge on, in both directions:

    - `--force` with the catalog unreachable returns False even though a
      perfectly current cache is sitting on disk. Alerting on that is a false
      alarm with a full day of runway still in hand, and a channel that cries
      wolf is a channel operators stop reading.
    - A True return can come from a cache accepted through the grace window,
      which means the file IS stale and the clock to the hard suspension is
      already running.

    So the verdict is built from two facts about the file the cost circuit
    will actually open at the next session: how old it is, and whether it
    prices every accepted model. `accepted_models_priced` is a second,
    network-free `refresh_openrouter_pricing(force=False)` against the
    now-current cache — a fresh-but-incomplete catalog fails the circuit
    closed just as hard as a missing one, and would otherwise look fine here.
    """

    openrouter_call_ok: bool
    openrouter_age_hours: float | None
    accepted_models_priced: bool
    litellm_call_ok: bool
    models_priced: int

    @property
    def openrouter_is_current(self) -> bool:
        age = self.openrouter_age_hours
        return age is not None and age < OPENROUTER_CACHE_FRESH_HOURS

    @property
    def desk_can_open(self) -> bool:
        """True when the next paid session will clear the pricing preflight."""
        return self.openrouter_is_current and self.accepted_models_priced

    def alert_text(self) -> str:
        if self.openrouter_age_hours is None:
            state = "there is NO OpenRouter pricing cache on disk"
            consequence = (
                "The cost circuit has nothing to price paid calls with and "
                "will fail closed at the next session — the desk would open "
                "and trade nothing."
            )
        elif not self.openrouter_is_current:
            state = (
                f"the OpenRouter pricing cache is {self.openrouter_age_hours:.1f}h "
                f"old (freshness window is {OPENROUTER_CACHE_FRESH_HOURS:.0f}h)"
            )
            consequence = (
                "The cost circuit is now running on its grace window. When "
                "that runs out it fails closed and suspends every paid call — "
                "the desk would open and trade nothing."
            )
        else:
            state = (
                f"the OpenRouter pricing cache is current "
                f"({self.openrouter_age_hours:.1f}h old) but does NOT price "
                f"every accepted model"
            )
            consequence = (
                "A catalog missing a model this desk is configured to route "
                "fails the circuit closed immediately, freshness "
                "notwithstanding. Check whether config/settings.yaml routes a "
                "model OpenRouter no longer lists."
            )
        return (
            "⚠️ QAMC pricing refresh did not land\n\n"
            f"After a refresh attempt, {state}.\n\n"
            f"{consequence}\n\n"
            "Check outbound access to openrouter.ai from the box, then run "
            "`scripts/refresh_pricing.py --force` by hand."
        )

    def summary(self) -> str:
        if self.openrouter_age_hours is None:
            age = "absent"
        else:
            age = f"{self.openrouter_age_hours:.1f}h old"
        return (
            f"openrouter cache {age} "
            f"({'current' if self.openrouter_is_current else 'STALE'}, "
            f"accepted models "
            f"{'priced' if self.accepted_models_priced else 'NOT priced'}), "
            f"refresh call {'ok' if self.openrouter_call_ok else 'failed'}; "
            f"litellm refresh {'ok' if self.litellm_call_ok else 'failed'}; "
            f"{self.models_priced} models priced"
        )


def refresh_all(force: bool = False) -> RefreshOutcome:
    """Refresh both caches and report the resulting state of each."""
    openrouter_ok = refresh_openrouter_pricing(force=force)
    age_hours = openrouter_cache_age_hours()
    is_current = age_hours is not None and age_hours < OPENROUTER_CACHE_FRESH_HOURS
    # Re-read the written cache from disk to confirm it prices every accepted
    # model. Only when it is current: against a stale cache this same call
    # would go back to the network, and a stale cache is already an alert.
    priced = bool(refresh_openrouter_pricing(force=False)) if is_current else False
    litellm_ok = refresh_pricing(force=force)
    return RefreshOutcome(
        openrouter_call_ok=bool(openrouter_ok),
        openrouter_age_hours=age_hours,
        accepted_models_priced=priced,
        litellm_call_ok=bool(litellm_ok),
        models_priced=len(PRICING),
    )


def send_alert(message: str) -> bool:
    """Push one operator alert. Returns whether it was actually delivered.

    Uses the same `TelegramNotifier` as the shutdown/hold alerts and
    `scripts/check_deploy_drift.py` — no new sender, no new channel.
    """
    from src.notifier import TelegramNotifier

    notifier = TelegramNotifier()
    if not notifier.enabled:
        print(
            "refresh_pricing: Telegram not configured; the alert above was "
            "printed only",
            file=sys.stderr,
        )
        return False
    return bool(notifier.send(message))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the OpenRouter and LiteLLM price caches.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the 24h cache freshness checks and always fetch",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Print findings but don't push an operator alert",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Print the one-line summary only, not the full price table",
    )
    args = parser.parse_args(argv)

    if not args.quiet:
        print(f"BEFORE refresh — PRICING has {len(PRICING)} models:")
        for k, v in sorted(PRICING.items()):
            print(f"  {k:25s} input=${v['input']:.2f}/M  output=${v['output']:.2f}/M")

    outcome = refresh_all(force=args.force)

    if not args.quiet:
        print(f"\nAFTER refresh — PRICING has {len(PRICING)} models:")
        for k, v in sorted(PRICING.items()):
            print(f"  {k:25s} input=${v['input']:.2f}/M  output=${v['output']:.2f}/M")

    print(f"\nrefresh_pricing: {outcome.summary()}")
    print(f"  openrouter cache: {openrouter_cache_path()}")
    print(f"  litellm cache:    {litellm_cache_path()}")

    if not outcome.litellm_call_ok:
        # Reporting-only. Never alerts: it does not gate a paid call, and an
        # alert operators learn to ignore is worse than no alert.
        print(
            "refresh_pricing: note — the LiteLLM dataset did not refresh. "
            "Cost REPORTING for non-OpenRouter models may be stale; no paid "
            "call is blocked by this.",
            file=sys.stderr,
        )

    if outcome.desk_can_open:
        return 0

    message = outcome.alert_text()
    print(message, file=sys.stderr)
    if not args.no_telegram:
        send_alert(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
