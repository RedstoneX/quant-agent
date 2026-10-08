"""Capture/replay the *session's* provider calls, not a later synthetic fetch.

Install after constructing TradingPipeline and before ``run_morning``.  This
patches the actual transport names used by the pipeline.  The recording stays
private until ``assert_public_safe`` accepts the complete bundle.  Replay
raises on an absent call and ``assert_consumed`` raises on unused evidence.

No prefetch or substituted value is involved: the producer still runs during
capture, including its retries, fallback and error classification.
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import json
import math
import threading
from collections import defaultdict, deque
from datetime import date, datetime
from urllib.error import HTTPError, URLError

import pandas as pd

from ops.rehearsal.feed_recording import _URLOPEN_MODULES, url_key


class SessionInputError(RuntimeError):
    """Missing, malformed, or unconsumed session input."""


_INFO_FIELDS = frozenset({
    "sector", "marketCap", "quoteType", "exDividendDate",
    "lastDividendValue", "trailingAnnualDividendRate", "trailingPE",
    "forwardPE", "priceToSalesTrailing12Months", "longName", "shortName",
    "industry", "country", "yearFounded", "foundedYear", "fullTimeEmployees",
    "totalAssets", "longBusinessSummary", "description",
})


def _value(item):
    """JSON-safe lossless values used by the actual provider methods."""
    if item is None or isinstance(item, (str, bool, int)):
        return item
    if isinstance(item, float):
        if math.isnan(item):
            return {"__float__": "nan"}
        if math.isinf(item):
            return {"__float__": "inf" if item > 0 else "-inf"}
        return item
    if isinstance(item, (datetime, pd.Timestamp)):
        return {"__datetime__": item.isoformat()}
    if isinstance(item, date):
        return {"__date__": item.isoformat()}
    if isinstance(item, pd.DataFrame):
        return {
            "__frame__": True,
            "index": [_value(x) for x in item.index],
            "columns": [_value(x) for x in item.columns],
            "multi_columns": isinstance(item.columns, pd.MultiIndex),
            "rows": [[_value(x) for x in row] for row in item.itertuples(index=False, name=None)],
        }
    if isinstance(item, pd.Series):
        return {
            "__series__": True,
            "index": [_value(x) for x in item.index],
            "values": [_value(x) for x in item.tolist()],
        }
    if isinstance(item, tuple):
        return {"__tuple__": [_value(x) for x in item]}
    if isinstance(item, list):
        return [_value(x) for x in item]
    if isinstance(item, dict):
        if not all(isinstance(k, str) for k in item):
            raise SessionInputError("provider returned a mapping with non-string keys")
        return {k: _value(v) for k, v in item.items()}
    # numpy scalars occur in yfinance frames and FRED metadata.
    if hasattr(item, "item"):
        return _value(item.item())
    raise SessionInputError(f"unsupported provider value: {type(item).__name__}")


def _revive(item):
    if isinstance(item, list):
        return [_revive(x) for x in item]
    if not isinstance(item, dict):
        return item
    if "__float__" in item:
        return float(item["__float__"])
    if "__datetime__" in item:
        return pd.Timestamp(item["__datetime__"])
    if "__date__" in item:
        return date.fromisoformat(item["__date__"])
    if "__tuple__" in item:
        return tuple(_revive(x) for x in item["__tuple__"])
    if item.get("__frame__") is True:
        columns = [_revive(x) for x in item["columns"]]
        if item["multi_columns"]:
            columns = pd.MultiIndex.from_tuples(columns)
        index = pd.Index([_revive(x) for x in item["index"]])
        return pd.DataFrame(
            [[_revive(x) for x in row] for row in item["rows"]],
            index=index, columns=columns,
        )
    if item.get("__series__") is True:
        return pd.Series(
            [_revive(x) for x in item["values"]],
            index=pd.Index([_revive(x) for x in item["index"]]),
        )
    return {k: _revive(v) for k, v in item.items()}


def _error(exc):
    if isinstance(exc, HTTPError):
        return {"type": "HTTPError", "code": exc.code, "reason": str(exc.reason)}
    if isinstance(exc, URLError):
        return {"type": "URLError", "reason": str(exc.reason)}
    return {"type": type(exc).__name__, "message": str(exc)}


def _raise_error(error, key):
    kind = error.get("type")
    if kind == "HTTPError":
        raise HTTPError(key, int(error["code"]), error.get("reason", ""), {}, None)
    if kind == "URLError":
        raise URLError(error.get("reason", ""))
    if kind == "TimeoutError":
        raise TimeoutError(error.get("message", ""))
    if kind == "ValueError":
        raise ValueError(error.get("message", ""))
    if kind == "RuntimeError":
        raise RuntimeError(error.get("message", ""))
    raise SessionInputError(f"unreplayable recorded failure for {key}: {kind}")


class SessionInputs:
    """Thread-safe per-request ledger; order between independent feeds is free."""

    def __init__(self, payload=None, *, max_bytes: int | None = None):
        self.recording = payload is None
        self.entries = [] if payload is None else list(payload.get("entries", ()))
        self._lock = threading.Lock()
        self._pending = defaultdict(deque)
        self._capture_errors = []
        self._replay_violations = []
        self._max_bytes = max_bytes
        self._captured_bytes = 0
        if max_bytes is not None and max_bytes <= 0:
            raise SessionInputError("positive provider capture byte bound is required")
        if not self.recording:
            if payload.get("schema") != 1:
                raise SessionInputError("unsupported session-input schema")
            for entry in self.entries:
                self._pending[(entry["kind"], entry["key"])].append(entry)

    def call(self, kind, key, original, *, transform=_value):
        if self.recording:
            try:
                value = original()
            except Exception as exc:
                self._append_capture({"kind": kind, "key": key, "error": _error(exc)})
                raise
            try:
                encoded = transform(value)
            except Exception as exc:
                # A capture must observe, never veto, a real Paper session.
                # The evidence is unusable and payload() will refuse it, but
                # the producer sees precisely the value it actually returned.
                with self._lock:
                    self._capture_errors.append(f"{kind}: {type(exc).__name__}")
                return value
            self._append_capture({"kind": kind, "key": key, "value": encoded})
            return value
        with self._lock:
            pending = self._pending[(kind, key)]
            if not pending:
                self._replay_violations.append("missing recorded provider call")
                raise SessionInputError(f"missing recorded {kind} call for {key}")
            entry = pending.popleft()
        if "error" in entry:
            try:
                _raise_error(entry["error"], key)
            except SessionInputError:
                with self._lock:
                    self._replay_violations.append("unreplayable recorded provider error")
                raise
        if "value" not in entry:
            with self._lock:
                self._replay_violations.append("recorded provider call has no outcome")
            raise SessionInputError(f"recorded {kind} call has no outcome for {key}")
        try:
            return _revive(entry["value"])
        except Exception:
            with self._lock:
                self._replay_violations.append("recorded provider value is malformed")
            raise SessionInputError("recorded provider value is malformed") from None

    def _append_capture(self, entry):
        # Keep the natural session behavior when a response exceeds the
        # recording budget, but refuse to export an incomplete recording.
        try:
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            with self._lock:
                self._capture_errors.append("provider capture could not be encoded")
            return
        with self._lock:
            if self._capture_errors:
                return
            if self._max_bytes is not None and self._captured_bytes + size > self._max_bytes:
                self._capture_errors.append("provider capture byte bound exceeded")
                return
            self.entries.append(entry)
            self._captured_bytes += size

    def payload(self):
        if not self.recording:
            raise SessionInputError("replay ledger cannot become a capture")
        if self._capture_errors:
            raise SessionInputError(
                "capture contains unreplayable provider outputs: "
                + ", ".join(self._capture_errors)
            )
        return {"schema": 1, "entries": list(self.entries)}

    def assert_consumed(self):
        if self.recording:
            raise SessionInputError("capture ledger has nothing to consume")
        if self._replay_violations:
            raise SessionInputError(
                f"{len(self._replay_violations)} strict provider replay violation(s) occurred"
            )
        remaining = sum(len(v) for v in self._pending.values())
        if remaining:
            raise SessionInputError(f"{remaining} recorded provider call(s) were not consumed")


class _Response:
    def __init__(self, url, body, status, headers):
        self._url, self._body, self.status, self.headers = url, body, status, headers

    def read(self, *_args):
        return self._body

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@contextlib.contextmanager
def session_inputs(pipeline, payload=None, *, max_bytes: int | None = None):
    """Yield a capture/replay ledger around natural morning provider calls.

    Caller must install after constructing the pipeline, before its first
    provider call, and retain the context until all worker threads finish.
    """
    import yfinance as yf

    ledger = SessionInputs(payload, max_bytes=max_bytes)
    patches = []

    def patch(owner, attr, replacement):
        original = getattr(owner, attr)
        setattr(owner, attr, replacement)
        patches.append((owner, attr, original))
        return original

    original_download = yf.download

    def download(*args, **kwargs):
        key = str(_value({"args": list(args), "kwargs": kwargs}))
        return ledger.call("yfinance.download", key,
                           lambda: original_download(*args, **kwargs))

    patch(yf, "download", download)
    original_ticker = yf.Ticker

    class Ticker:
        def __init__(self, symbol, *args, **kwargs):
            self.symbol = str(symbol).upper()
            self._real = original_ticker(symbol, *args, **kwargs) if ledger.recording else None

        def _read(self, name):
            def fetch():
                return getattr(self._real, name)

            def encode(value):
                if name == "info":
                    if not isinstance(value, dict):
                        raise SessionInputError("yfinance info was not a mapping")
                    # The live caller sees the original mapping. Replay needs
                    # only fields this codebase reads, not hundreds of other
                    # fields that may contain private profile URLs.
                    return _value({key: value[key] for key in _INFO_FIELDS if key in value})
                return _value(value)

            return ledger.call("yfinance.Ticker." + name, self.symbol, fetch,
                               transform=encode)

        @property
        def info(self):
            return self._read("info")

        @property
        def calendar(self):
            return self._read("calendar")

        @property
        def earnings_dates(self):
            return self._read("earnings_dates")

        @property
        def dividends(self):
            return self._read("dividends")

    patch(yf, "Ticker", Ticker)

    # The Fred object was built in TradingPipeline.__init__, so rebinding
    # src.data.macro.Fred now would leave the real instance on the wire.
    fred = pipeline.macro.fred
    for method in ("get_series", "get_series_info"):
        original = getattr(fred, method)

        def wrapped(series_id, *args, _method=method, _original=original, **kwargs):
            key = str(_value({"series": str(series_id).upper(), "args": list(args),
                              "kwargs": {k: v for k, v in kwargs.items() if k != "request_timeout_s"}}))
            return ledger.call("FRED." + _method, key,
                               lambda: _original(series_id, *args, **kwargs))

        patch(fred, method, wrapped)

    for module_name in _URLOPEN_MODULES:
        module = importlib.import_module(module_name)
        if not hasattr(module, "urlopen"):
            continue
        original = module.urlopen

        def urlopen(request, *args, _original=original, **kwargs):
            raw_url = getattr(request, "full_url", None) or str(request)
            key = url_key(raw_url)

            def fetch():
                with _original(request, *args, **kwargs) as response:
                    body = response.read()
                    return {
                        "body_b64": base64.b64encode(body).decode("ascii"),
                        "status": getattr(response, "status", 200),
                        "headers": dict(getattr(response, "headers", {}) or {}),
                    }

            value = ledger.call("urlopen", key, fetch)
            return _Response(raw_url, base64.b64decode(value["body_b64"]),
                             value["status"], value["headers"])

        patch(module, "urlopen", urlopen)

    try:
        yield ledger
    finally:
        for owner, attr, original in reversed(patches):
            setattr(owner, attr, original)
