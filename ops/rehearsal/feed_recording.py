"""Recorded FRED series and news feeds, so a rehearsal replays them offline.

Board item 202, the last two unrecorded inputs. The settling run of 2026-10-01
ended on `HermeticBreach: 11 outbound connection attempt(s) were blocked` and
every one of those eleven was either `api.stlouisfed.org` (FRED, 15 series) or
one of the ten news/reference hosts the news provider reads. Bars and sectors
were already recorded; these were not, so a replay still reached live
providers and was correctly voided.

This follows the SAME pattern as `ops/rehearsal/market_recording.py` and
`ops/rehearsal/broker.recorded_sector_lookup` rather than inventing a second
mechanism: capture ONCE, deliberately online, as an operator command; then
patch the name in the module that BUILDS the client, so everything above it
— retries, budgets, coverage accounting, the honest degradation paths — stays
real and no importer can route around the patch.

Two transports, because this dependency set has two:

  * `src.data.macro.Fred` — `fredapi`, which is a bare `urlopen` of its own.
  * `urlopen` as imported into `src.data.news`, `src.data.event_calendar` and
    `src.data.earnings` (the FRED release-dates call, the Fed/SEC pages and
    the ~20 RSS feeds all go through it).

FAILURES ARE RECORDED TOO, deliberately. Every FRED failure in the retained
log is `fetch_deadline_exceeded`, so a recording that only ever holds
successes cannot replay the thing being studied. A recorded failure is
re-raised at the same place the live one was raised, and the retry, coverage
and degradation logic above it then behaves exactly as it did live.

NOTHING IS SUBSTITUTED. A series or a URL the recording does not hold is
named in the `unavailable` list — which `assert_hermetic` turns into a loud
`MissingRecordedInput` — and the call raises rather than returning a default
or a computed stand-in. An unknown stays unknown; a filled-in value would
poison the evidence the recording exists to provide.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_RECORDING = Path(__file__).resolve().parent / "recordings" / "feeds.json.gz"

# Query parameters stripped before a URL becomes a recording key. An API key
# is a credential, not part of the request's identity: recording one would
# both leak it to disk and make the recording unreplayable under any other
# key.
_SECRET_PARAMS = {"api_key", "apikey", "token"}


class RecordedFeedFailure(RuntimeError):
    """A failure the recording captured, re-raised on replay."""


def url_key(url: str) -> str:
    """The recording key for a URL: the URL with credentials stripped."""
    parts = urlsplit(str(url))
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _SECRET_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def load(path: Path | str = DEFAULT_RECORDING) -> dict | None:
    """The recording, or None when none has been captured on this box."""
    path = Path(path)
    if not path.exists():
        return None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return json.load(handle)


def _failure(exc: BaseException) -> dict:
    return {"ok": False, "error_type": type(exc).__name__, "error": str(exc) or type(exc).__name__}


def is_error_shaped(entry) -> bool:
    """True for a recorded entry that is a failure, not an answer."""
    if not isinstance(entry, dict):
        return False
    status = entry.get("status")
    return (
        entry.get("ok") is False
        or "error" in entry
        or "error_type" in entry
        or (isinstance(status, int) and status >= 400)
    )


def _admit(table: dict, key: str, entry: dict, refused: list[str], kind: str) -> None:
    """Record an answer; an error-shaped entry is reported and NOT recorded."""
    if is_error_shaped(entry):
        refused.append(f"{kind} {key}: {entry.get('error_type')}: {str(entry.get('error'))[:120]}")
        return
    table[key] = entry


def _raise_recorded(entry: dict, what: str):
    raise RecordedFeedFailure(
        f"recorded failure for {what}: {entry.get('error_type')}: {entry.get('error')}"
    )


def capture(
    series_ids=(),
    urls=(),
    path: Path | str = DEFAULT_RECORDING,
    api_key: str | None = None,
    merge: bool = False,
) -> dict:
    """Fetch FRED series and feed URLs ONCE and write them to disk. Online by design.

    Never called from a rehearsal — `run_rehearsal` only ever reads. A failed
    fetch is REFUSED: it is reported (returned under `refused` and printed by
    the CLI) and nothing is recorded for that input, so a replay finds the
    input absent and says so. A recording holds answers or nothing.
    """
    from urllib.request import Request, urlopen

    fred_series: dict[str, dict] = {}
    fred_info: dict[str, dict] = {}
    http: dict[str, dict] = {}
    refused: list[str] = []

    ids = sorted({str(s).strip().upper() for s in series_ids if str(s).strip()})
    if ids:
        import os

        from fredapi import Fred

        client = Fred(api_key=api_key or os.getenv("FRED_API_KEY") or "")
        for series_id in ids:
            try:
                series = client.get_series(series_id)
                _admit(fred_series, series_id, {
                    "ok": True,
                    "index": [str(i)[:10] for i in series.index],
                    "values": [None if v != v else float(v) for v in series.values],
                }, refused, "fred_series")
            except Exception as exc:  # noqa: BLE001 — reported, never recorded
                _admit(fred_series, series_id, _failure(exc), refused, "fred_series")
            try:
                raw = client.get_series_info(series_id)
                getter = getattr(raw, "get", None)
                _admit(fred_info, series_id, {
                    "ok": True,
                    "info": {
                        "observation_end": str(getter("observation_end")) if getter else None,
                        "last_updated": str(getter("last_updated")) if getter else None,
                    },
                }, refused, "fred_series_info")
            except Exception as exc:  # noqa: BLE001 — reported, never recorded
                _admit(fred_info, series_id, _failure(exc), refused, "fred_series_info")

    from src.data.news import SEC_USER_AGENT, USER_AGENT, _is_sec_gov

    for url in sorted({str(u).strip() for u in urls if str(u).strip()}):
        key = url_key(url)
        try:
            # sec.gov refuses the generic agent with 403; the repo's own SEC
            # agent is what the live fetch paths send.
            agent = SEC_USER_AGENT if _is_sec_gov(url) else USER_AGENT
            request = Request(url, headers={"User-Agent": agent})
            with urlopen(request, timeout=20) as response:  # noqa: S310 — operator command
                body = response.read()
            status = getattr(response, "status", None)
            _admit(http, key, {
                "ok": True,
                "body_b64": base64.b64encode(body).decode("ascii"),
                **({"status": status} if isinstance(status, int) else {}),
            }, refused, "http")
        except Exception as exc:  # noqa: BLE001 — reported, never recorded
            _admit(http, key, _failure(exc), refused, "http")

    if merge:
        # Add to what is already recorded instead of rewriting it: re-fetching
        # the eleven feeds and 15 series live would replace evidence, not add.
        held = load(path) or {}

        def _answers(table):
            return {k: v for k, v in (table or {}).items() if not is_error_shaped(v)}

        fred_series = {**_answers(held.get("fred_series")), **fred_series}
        fred_info = {**_answers(held.get("fred_series_info")), **fred_info}
        http = {**_answers(held.get("http")), **http}

    recording = {
        "captured_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": "fredapi.Fred + urllib.request.urlopen, captured by ops.rehearsal.feed_recording",
        "fred_series": fred_series,
        "fred_series_info": fred_info,
        "http": http,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt") as handle:
        json.dump(recording, handle)
    return {**recording, "refused": refused}


def _offline_fred(record: list[str], recording: dict | None):
    import pandas as pd

    series_table = dict((recording or {}).get("fred_series") or {})
    info_table = dict((recording or {}).get("fred_series_info") or {})

    def _miss(kind: str, series_id: str):
        note = (
            f"FRED {kind} for {series_id} (not in the feed recording; capture one "
            "with `python -m ops.rehearsal.feed_recording --series ...`)"
        )
        if note not in record:
            record.append(note)
        raise RecordedFeedFailure(
            f"no recorded FRED {kind} for {series_id} — a replay never invents one"
        )

    class _OfflineFred:
        def __init__(self, api_key=None, **_kwargs):
            self.api_key = api_key

        def get_series(self, series_id, **_kwargs):
            key = str(series_id).upper()
            entry = series_table.get(key)
            if entry is None:
                _miss("series", key)
            if not entry.get("ok"):
                _raise_recorded(entry, f"FRED series {key}")
            index = pd.to_datetime(list(entry.get("index") or []))
            return pd.Series(list(entry.get("values") or []), index=index)

        def get_series_info(self, series_id, **_kwargs):
            key = str(series_id).upper()
            entry = info_table.get(key)
            if entry is None:
                _miss("series metadata", key)
            if not entry.get("ok"):
                _raise_recorded(entry, f"FRED metadata {key}")
            return pd.Series(dict(entry.get("info") or {}))

    return _OfflineFred, len(series_table)


class _RecordedResponse:
    """What `urlopen` returns, narrowed to what this repo actually reads."""

    def __init__(self, url: str, body: bytes):
        self._url = url
        self._body = body
        self.status = 200
        self.headers = {}

    def read(self, *_args) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _offline_urlopen(record: list[str], recording: dict | None):
    http = dict((recording or {}).get("http") or {})

    def _urlopen(request, *_args, **_kwargs):
        url = getattr(request, "full_url", None) or getattr(request, "get_full_url", lambda: None)()
        if url is None:
            url = str(request)
        key = url_key(url)
        entry = http.get(key)
        if entry is None:
            note = (
                f"feed response for {key} (not in the feed recording; capture one "
                "with `python -m ops.rehearsal.feed_recording --url ...`)"
            )
            if note not in record:
                record.append(note)
            raise RecordedFeedFailure(
                f"no recorded response for {key} — a replay never fetches it and "
                "never substitutes a default"
            )
        if not entry.get("ok"):
            _raise_recorded(entry, key)
        return _RecordedResponse(url, base64.b64decode(entry.get("body_b64") or ""))

    return _urlopen, len(http)


# The modules that import `urlopen` into their own namespace and call it on a
# session path. Patching the name where it is BOUND is the same choice
# `recorded_sector_lookup` makes for `yf`: everything above the transport
# stays real. The event calendar has no binding of its own: its providers
# call `urlopen` through `src.data.news`, so patching news covers them.
_URLOPEN_MODULES = (
    "src.data.news",
    "src.data.earnings",
)


@contextlib.contextmanager
def recorded_feeds(record: list[str], recording: dict | None):
    """Serve FRED and the news feeds from the recording, never from the network."""
    import importlib

    fred_class, series_count = _offline_fred(record, recording)
    urlopen_stub, url_count = _offline_urlopen(record, recording)

    patched: list[tuple[object, str, object]] = []
    macro = importlib.import_module("src.data.macro")
    patched.append((macro, "Fred", macro.Fred))
    macro.Fred = fred_class
    for name in _URLOPEN_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "urlopen"):
            patched.append((module, "urlopen", module.urlopen))
            module.urlopen = urlopen_stub
    try:
        yield (
            f"FRED ({series_count} recorded series) and the news/reference feeds "
            f"({url_count} recorded URLs) are served from the recording and can "
            "no longer reach the network"
        )
    finally:
        for module, attribute, original in reversed(patched):
            setattr(module, attribute, original)


def _default_urls() -> list[str]:
    """Every RSS/reference feed the news provider reads, from its own table."""
    from src.data.news import RSS_FEEDS

    return [str(v) for v in dict(RSS_FEEDS).values()]


def _main() -> int:
    parser = argparse.ArgumentParser(description="Record FRED series and news feeds.")
    parser.add_argument("--series", nargs="*", default=[], help="FRED series ids")
    parser.add_argument("--url", nargs="*", default=[], help="feed URLs")
    parser.add_argument("--out", default=str(DEFAULT_RECORDING))
    parser.add_argument(
        "--merge", action="store_true",
        help="keep what the recording already holds and add the given series/urls",
    )
    args = parser.parse_args()
    urls = list(args.url) or ([] if args.merge else _default_urls())
    result = capture(args.series, urls, args.out, merge=args.merge)
    print(
        f"recorded {len(result['fred_series'])} FRED series and "
        f"{len(result['http'])} URLs to {args.out}"
    )
    for line in result["refused"]:
        print(f"REFUSED (not recorded): {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
