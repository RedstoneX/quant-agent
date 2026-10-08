"""Recorded FRED macro data for a rehearsal, taken from data the desk ALREADY recorded.

WHY THIS EXISTS
---------------
`ops.rehearsal.feed_recording` serves FRED from a capture, but capturing needs
a real FRED key and the only real key lives in production, which a rehearsal
box must never touch. A capture made here therefore records 0 of 15 series and
a morning or midday session cannot complete.

The desk has, however, already recorded real FRED observations: the pinned
fixture `ops/model_policy/fixtures/fred_series_2026-09-14.json.gz`, fetched
live on 2026-09-14 for the seat-exam work (manifest `fred_macro_2026-09-14.json`,
sha256-pinned, admissibility re-checked on every load). It holds raw
observations and series metadata for all 15 `CONFIGURED_SERIES`. This module
re-shapes that blob into the table layout `feed_recording` already replays, so
macro is served exactly like model answers: from a recording, through the
same patched client, with everything above the client running for real.

PRECEDENCE AND GAPS
-------------------
A series the feed recording itself holds — success OR recorded failure — wins;
this fixture only fills series the feed recording does not hold. The one
exception: a recorded failure that is FRED rejecting the capturing box's API
key is a fact about that box's credential, not about FRED or the series, so
it counts as not recorded (a capture on a box with only a placeholder key
records exactly this for all 15). A series in
neither is left absent, so `feed_recording` raises it as an unrecorded input.
Nothing is invented, interpolated, shifted or re-dated.

KNOWN LIMITS (read before trusting a replay's macro numbers)
------------------------------------------------------------
The observations are as of 2026-09-14 whatever date the rehearsal declares,
and each series holds only the window the provider asked for at capture time.
The provider's own freshness logic therefore sees whatever age that implies.
"""

from __future__ import annotations

import contextlib
import json

from ops.rehearsal import feed_recording

FIXTURE_MANIFEST = "fred_macro_2026-09-14.json"
FIXTURE_BLOB = "fred_series_2026-09-14.json.gz"


def tables_from_pinned_fixture() -> tuple[dict, dict]:
    """(fred_series, fred_series_info) in `feed_recording`'s layout."""
    from ops.model_policy import fixture_policy

    payload = json.loads(fixture_policy.load_blob(FIXTURE_MANIFEST, FIXTURE_BLOB))
    series: dict[str, dict] = {}
    for sid, obs in (payload.get("series") or {}).items():
        points = sorted((str(d)[:10], v) for d, v in (obs or {}).items())
        if not points:
            continue  # empty is unrecorded, never an empty-but-valid series
        series[str(sid).upper()] = {
            "ok": True,
            "index": [d for d, _ in points],
            "values": [None if v is None or v != v else float(v) for _, v in points],
        }
    info = {
        str(sid).upper(): {"ok": True, "info": {
            "observation_end": str(meta.get("observation_end")),
            "last_updated": str(meta.get("last_updated")),
        }}
        for sid, meta in (payload.get("series_info") or {}).items()
        if meta
    }
    return series, info


def merge_into(recording: dict | None) -> dict:
    """The feed recording with FRED gaps filled from the pinned fixture."""
    merged = dict(recording or {})
    series, info = tables_from_pinned_fixture()
    for key, pinned in (("fred_series", series), ("fred_series_info", info)):
        held = {
            sid: entry for sid, entry in (merged.get(key) or {}).items()
            if entry.get("ok") or "api_key" not in str(entry.get("error", ""))
        }
        merged[key] = {**pinned, **held}
    return merged


def load_feeds_with_macro(*args, **kwargs) -> dict:
    """`feed_recording.load`, plus recorded macro for every series it lacks."""
    return merge_into(feed_recording.load(*args, **kwargs))


@contextlib.contextmanager
def recorded_feeds_with_macro(record: list[str], recording: dict | None):
    """`feed_recording.recorded_feeds`, with FRED also served to a provider that already exists.

    `recorded_feeds` rebinds the NAME `src.data.macro.Fred`, which only helps a
    provider built afterwards. The pipeline builds its `MacroDataProvider` (and
    so its `Fred` instance) at construction, before the session starts, so that
    instance kept reaching the wire and the replay was voided on the first
    series. Patching the two methods on the class is the narrowest layer that
    covers instances built before AND after, the same layer `replay.py` picks
    for the model transports. Misses and recorded failures raise exactly as in
    `feed_recording`; nothing falls back to live.
    """
    from src.data.fred_series_client import FredSeriesClient

    offline = feed_recording._offline_fred(record, recording)[0]()
    saved = {n: getattr(FredSeriesClient, n) for n in ("get_series", "get_series_info")}
    with feed_recording.recorded_feeds(record, recording) as message:
        FredSeriesClient.get_series = lambda self, sid, **kw: offline.get_series(sid, **kw)
        FredSeriesClient.get_series_info = lambda self, sid, **kw: offline.get_series_info(sid, **kw)
        try:
            yield message + "; FRED methods also replaced on the class, so an already-built provider is covered"
        finally:
            for name, original in saved.items():
                setattr(FredSeriesClient, name, original)
