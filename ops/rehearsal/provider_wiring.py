"""Install either a real captured session's inputs or legacy sampled feeds.

The captured path leaves the pipeline's provider objects intact and replaces
their actual transport calls. The older sampled path must rebind the market
object already handed to morning stages, then close separate sector/feed
routes; both paths run inside the caller's network wall.
"""

from __future__ import annotations


def install_provider_inputs(stack, pipeline, *, payload, market_recording,
                            unavailable: list[str], checks: list[str],
                            notes: list[str]):
    """Return a strict session ledger, or None for sampled-recording replay."""
    if payload is not None:
        from ops.rehearsal.session_inputs import session_inputs

        ledger = stack.enter_context(session_inputs(pipeline, payload))
        checks.append("session providers replay captured call outcomes only")
        return ledger

    from ops.rehearsal.broker import blocked_market_data, recorded_sector_lookup
    from ops.rehearsal.market_recording import load as load_recording
    from ops.rehearsal.market_recording import recorded_market_data

    recording = load_recording(market_recording) if market_recording else load_recording()
    live_market = pipeline.market
    if recording:
        served = recorded_market_data(unavailable, recording)
        checks.append(
            "market data is served from the recording captured "
            f"{recording.get('captured_utc')} "
            f"({len(recording.get('bars') or {})} symbols), not downloaded"
        )
    else:
        served = blocked_market_data(unavailable)
        notes.append(
            "no recorded market data on this box, so every technical read is "
            "empty — capture one with `python -m ops.rehearsal.market_recording "
            "SYM ...` (board item 202)"
        )

    # TradingPipeline.__init__ passes the same market object into its stages.
    # Rebinding pipeline.market alone left morning research on the live wire.
    pipeline.market = served
    rebound = []
    for name, obj in list(vars(pipeline).items()):
        if obj is live_market or not hasattr(obj, "market"):
            continue
        if getattr(obj, "market", None) is live_market:
            obj.market = served
            rebound.append(name)
    still_live = [
        name for name, obj in vars(pipeline).items()
        if obj is not live_market and getattr(obj, "market", None) is live_market
    ]
    if still_live:
        raise AssertionError(
            "a rehearsal cannot start with the LIVE market-data provider "
            f"still reachable through {sorted(still_live)} — it would "
            "fetch prices from the network instead of the recording "
            "(board item 202)"
        )
    checks.append(
        "every holder of the market-data provider was rebound to the "
        f"rehearsal's, not just the pipeline: {sorted(rebound) or 'none'}"
    )

    # broker._get_sector builds its own yfinance client, independent of the
    # pipeline.market object. FRED and news/reference reads also need their
    # own recorded transports; a missing call must raise, never substitute.
    from ops.rehearsal.sector_recording import merge_into as with_sectors

    checks.append(stack.enter_context(
        recorded_sector_lookup(unavailable, with_sectors(recording))))
    from ops.rehearsal.macro_recording import load_feeds_with_macro
    from ops.rehearsal.macro_recording import recorded_feeds_with_macro as recorded_feeds

    feeds = load_feeds_with_macro()
    if not feeds:
        notes.append(
            "no recorded FRED/news feeds on this box, so every macro and "
            "news read raises as a missing recorded input — capture one "
            "with `python -m ops.rehearsal.feed_recording --series ...` "
            "(board item 202)"
        )
    checks.append(stack.enter_context(recorded_feeds(unavailable, feeds)))
    return None
