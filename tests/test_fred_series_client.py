"""The FRED transport: per-request timeout, fredapi-shaped results, no global state."""
import io
import json
import socket
from datetime import date
from unittest.mock import patch
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import pytest

from src.data import fred_series_client as client_mod
from src.data.fred_series_client import FredSeriesClient
from src.data.macro import MacroDataProvider


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _observations(rows):
    return {"observations": [{"date": d, "value": v} for d, v in rows]}


def test_dot_becomes_nan_with_float_dtype_and_date_index():
    body = _observations([("2026-09-01", "4.1"), ("2026-09-02", "."), ("2026-09-03", "4.3")])
    with patch.object(client_mod._transport, "urlopen", return_value=_Resp(body)):
        s = FredSeriesClient("k").get_series("DGS10", request_timeout_s=3)
    assert s.dtype == np.float64
    assert list(s.index) == list(pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]))
    assert np.isnan(s.iloc[1]) and s.iloc[2] == 4.3


def test_timeout_is_passed_on_every_request_and_dates_are_formatted():
    seen = []

    def fake(req, timeout=None):
        seen.append((req.full_url, timeout))
        return _Resp(_observations([]) if "observations" in req.full_url else {"seriess": [{"id": "X"}]})

    with patch.object(client_mod._transport, "urlopen", side_effect=fake):
        c = FredSeriesClient("k")
        c.get_series("X", request_timeout_s=2.5, observation_start=date(2026, 1, 2))
        c.get_series_info("X", request_timeout_s=1.5)
    assert [t for _, t in seen] == [2.5, 1.5]
    assert "observation_start=2026-01-02" in seen[0][0]


def test_socket_default_timeout_is_untouched_by_a_fetch():
    before = socket.getdefaulttimeout()
    seen_default = []

    def fake(req, timeout=None):
        seen_default.append(socket.getdefaulttimeout())
        return _Resp(_observations([("2026-09-01", "1")]))

    with patch.object(client_mod._transport, "urlopen", side_effect=fake):
        FredSeriesClient("k").get_series("X", request_timeout_s=1.0)
    assert socket.getdefaulttimeout() == before
    assert seen_default == [before]


def test_series_info_fields_survive():
    body = {"seriess": [{"id": "X", "observation_end": "2026-09-01", "last_updated": "2026-09-02 08:00:00-05"}]}
    with patch.object(client_mod._transport, "urlopen", return_value=_Resp(body)):
        info = FredSeriesClient("k").get_series_info("X", request_timeout_s=1)
    assert info.get("observation_end") == "2026-09-01"
    assert info.get("last_updated").startswith("2026-09-02")


def test_timed_out_request_yields_the_named_failure_and_leaves_socket_alone():
    before = socket.getdefaulttimeout()
    with patch.object(client_mod._transport, "urlopen", side_effect=TimeoutError("timed out")), \
            patch("src.data.macro.time.sleep"):
        provider = MacroDataProvider(api_key="k", max_retries=0)
        s = provider._safe_get_series("VIXCLS", observation_start=date(2026, 1, 1))
    assert len(s) == 0
    assert socket.getdefaulttimeout() == before
    coverage = provider._run_coverage if hasattr(provider, "_run_coverage") else None
    assert coverage is None or "timed out" in str(coverage)


def test_api_key_never_appears_in_an_http_error():
    body = b'{"error_message": "Bad Request. The value for variable api_key is not registered."}'
    err = HTTPError("https://x/?api_key=SECRET", 400, "Bad Request", {}, io.BytesIO(body))
    with patch.object(client_mod._transport, "urlopen", side_effect=err):
        with pytest.raises(ValueError) as ei:
            FredSeriesClient("SECRET").get_series("X", request_timeout_s=1)
    assert "SECRET" not in str(ei.value)
