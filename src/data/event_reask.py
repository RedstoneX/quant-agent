"""Prefetch pass that asks again for the release schedules it missed.

Split out of `MacroEventCalendarProvider.prefetch_release_schedules` (board
item 187) so the provider module stays under its file-size baseline.
"""

from __future__ import annotations


def prefetch_with_reasks(provider):
    """Run the prefetch pass, then re-ask the releases it missed.

    Sets the provider's prefetch mode for the duration and returns the merged
    coverage (also stored on `provider.last_coverage`).
    """
    provider._prefetch_mode = True
    configured = provider.releases
    try:
        provider.get_upcoming_events()
        merged = provider.last_coverage
        # Board item 187 / owner ruling 2026-10-02: a miss is asked again
        # before it is accepted. Each further pass re-asks ONLY the
        # releases still missing, each with a fresh fair share of the
        # same `total_fetch_deadline_s` (off the trading path, so no
        # session pays for it). The loop ends the moment a pass recovers
        # nothing, so no retry count is invented: the data decides.
        while merged is not None and merged.failed:
            missing = {f.release_id for f in merged.failed}
            provider.releases = tuple(r for r in configured if r.release_id in missing)
            provider.get_upcoming_events()
            again = provider.last_coverage
            if again is None or again.succeeded == 0:
                break
            merged = type(merged)(
                configured=len(configured),
                succeeded=merged.succeeded + again.succeeded,
                failed=again.failed,
                next_beyond_horizon=sorted(
                    merged.next_beyond_horizon + again.next_beyond_horizon,
                    key=lambda e: (e.event_date, e.label),
                ),
                from_cache=merged.from_cache,
            )
    finally:
        provider.releases = configured
        provider._prefetch_mode = False
    provider.last_coverage = merged
    return provider.last_coverage
