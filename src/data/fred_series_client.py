"""FRED JSON transport with a per-request timeout.

One GET, one timeout, no process-wide state. This replaces `fredapi` in the
macro summary: `fredapi` takes no timeout, so the old code set
`socket.setdefaulttimeout` around each call, and that default is shared by
every thread in the process (the other research branches and the broker
websocket) -- their new sockets silently inherited the shortened limit.

Here the timeout travels with the request (`urlopen(..., timeout=...)`), and
nothing global is touched.

Transport goes through `src.data.news.urlopen` -- the one module whose
`urlopen` the rehearsal rig rebinds -- so recording and replay still see these
calls. Retries and the wall-clock budget stay in `MacroDataProvider`; this
module never retries.

`FredSeriesClient` keeps `fredapi.Fred`'s two method names and return shapes
(`get_series` -> float Series indexed by date with NaN for FRED's "."
missing marker; `get_series_info` -> Series of the metadata fields), so every
caller, freshness check and rehearsal hook sees identical data. The API key
is sent as a query parameter and is never logged or put in an exception.
"""

import json
from datetime import date, datetime
from urllib.error import HTTPError
from urllib.parse import urlencode

import pandas as pd

from src.data import news as _transport  # the one module whose `urlopen` the rehearsal rebinds

FRED_BASE_URL = "https://api.stlouisfed.org/fred"
_USER_AGENT = "quant-agent fred-series"


def http_get_json(url: str, timeout: float | None, user_agent: str = _USER_AGENT) -> dict:
    """One GET returning parsed JSON, with `timeout` applied to this request only."""
    request = _transport.Request(url, headers={"User-Agent": user_agent})
    with _transport.urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https host
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def _param(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value)


class FredSeriesClient:
    """Drop-in for the two `fredapi.Fred` calls the macro summary uses."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, path: str, params: dict, timeout: float | None) -> dict:
        query = {k: _param(v) for k, v in params.items() if v is not None}
        query["api_key"] = self.api_key
        query["file_type"] = "json"
        url = f"{FRED_BASE_URL}/{path}?{urlencode(query)}"
        try:
            return http_get_json(url, timeout)
        except HTTPError as exc:
            # fredapi surfaces FRED's own message; keep that, drop the URL (it holds the key).
            message = ""
            try:
                message = json.loads(exc.read().decode("utf-8")).get("error_message", "")
            except Exception:  # noqa: BLE001 — body is optional detail only
                pass
            raise ValueError(message or f"HTTP Error {exc.code}: {exc.reason}") from None

    def get_series(self, series_id: str, request_timeout_s: float | None = None, **kwargs) -> pd.Series:
        """Observations as a float Series indexed by date; "." becomes NaN."""
        data = self._get("series/observations", {"series_id": series_id, **kwargs}, request_timeout_s)
        rows = data.get("observations") or []
        index = pd.to_datetime([r["date"] for r in rows])
        values = pd.to_numeric(
            pd.Series([r["value"] for r in rows], dtype=object).replace(".", float("nan")),
            errors="coerce",
        ).astype(float)
        return pd.Series(values.to_numpy(), index=index, dtype=float)

    def get_series_info(self, series_id: str, request_timeout_s: float | None = None) -> pd.Series:
        """Metadata fields of one series (observation_end, last_updated, ...)."""
        data = self._get("series", {"series_id": series_id}, request_timeout_s)
        found = data.get("seriess") or []
        if not found:
            raise ValueError(f"No series found with id {series_id}")
        return pd.Series(found[0])
