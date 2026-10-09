"""Item 187: the prefetch asks again for the release schedules it missed."""

from datetime import timedelta as _timedelta

from src.data.event_calendar import (
    MacroEventCalendarProvider as _Provider,
    MacroRelease as _Release,
)
from src.trading_calendar import et_today as _today

# --- item 187: the prefetch asks again for what it missed -------------------

_TWO = (_Release(10, "CPI", "inflation print"), _Release(11, "NFP", "jobs print"))


def test_the_prefetch_re_asks_only_for_the_releases_it_missed_and_stops_when_nothing_improves(tmp_path):
    dates = [_today() + _timedelta(days=3)]
    p = _Provider(api_key="k", releases=_TWO, schedule_cache_path=str(tmp_path / "rs.json"))
    asked = []

    def fake(release, *a, **k):
        asked.append(release.release_id)
        if release.release_id == 11 and asked.count(11) < 2:
            return [], "fetch_deadline_exceeded"
        return dates, None

    p._fetch_release_dates = fake
    coverage = p.prefetch_release_schedules()
    assert asked == [10, 11, 11], "second ask must cover only the missed release"
    assert coverage.complete and coverage.configured == 2 and coverage.succeeded == 2
    assert p.releases == _TWO, "the configured set must be restored"

    # a release that never answers: asked again once, then given up as a NAMED failure
    q = _Provider(api_key="k", releases=_TWO, schedule_cache_path=str(tmp_path / "rs2.json"))
    asked2 = []
    q._fetch_release_dates = lambda r, *a, **k: (
        asked2.append(r.release_id),
        ([], "boom") if r.release_id == 11 else (dates, None),
    )[1]
    cov = q.prefetch_release_schedules()
    assert asked2 == [10, 11, 11]
    assert [f.label for f in cov.failed] == ["NFP"] and cov.configured == 2 and cov.succeeded == 1
