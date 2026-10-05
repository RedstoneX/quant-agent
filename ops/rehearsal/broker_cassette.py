"""Strict Alpaca SDK-boundary recording and replay for offline rehearsal.

This module does not construct an Alpaca client and cannot make a network call.
Callers explicitly wrap an already-constructed TradingClient or
StockHistoricalDataClient while capturing, then use :class:`ReplayBrokerCassette`
offline. The existing synthetic rehearsal clients remain untouched.

All stable provider identifiers are private fixture linkage, including Alpaca's
integer Trade/Snapshot market-print IDs. They are deterministically tokenized;
their raw values add no replay behavior and would make public recordings
persistently linkable to the source account/feed observation.
"""

from __future__ import annotations

import copy
import threading
from functools import wraps
from typing import Any, Mapping

from ops.rehearsal.broker_cassette_codec import (
    Codec as _Codec,
    IdentifierTokenizer as _IdentifierTokenizer,
    SCHEMA,
)


class BrokerCassetteError(RuntimeError):
    """Base class for deterministic cassette failures."""


class CassetteMismatch(BrokerCassetteError):
    """The next replay call differs from the next recorded call."""


class MissingRecordedBrokerCall(BrokerCassetteError):
    """Replay made a call after the cassette was exhausted."""


class UnusedRecordedBrokerCalls(BrokerCassetteError):
    """Replay finished before every recorded call was consumed."""


class RecordedBrokerError(BrokerCassetteError):
    """A sanitized broker error recorded during capture."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        status_code: Any = None,
        code: Any = None,
        broker_message: str | None = None,
    ):
        super().__init__(message)
        # Alpaca APIError exposes message/code/status_code while the current
        # broker classifiers also consume str(exc). Preserve both surfaces.
        self.message = broker_message if broker_message is not None else message
        self.error_type = error_type
        self.status_code = status_code
        self.status = status_code
        self.code = code


def _exception_attr(exc: BaseException, name: str) -> Any:
    """Read an optional SDK error attribute without hiding a broken property."""
    try:
        return getattr(exc, name)
    except AttributeError:
        return None


class BrokerCassette:
    """Thread-safe, in-memory ordered recording shared by both SDK clients."""

    def __init__(self):
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._tokenizer = _IdentifierTokenizer()
        self._codec = _Codec(self._tokenizer)

    def invoke(self, client_name: str, method_name: str, method, args, kwargs):
        context = f"{client_name}.{method_name}"
        with self._lock:
            entry = {
                "client": client_name,
                "method": method_name,
                "args": self._codec.encode(list(args), context=f"{context}.args"),
                "kwargs": self._codec.encode(dict(kwargs), context=f"{context}.kwargs"),
            }
            self._entries.append(entry)
        try:
            answer = method(*args, **kwargs)
        except Exception as exc:
            with self._lock:
                display_text = self._tokenizer.sanitize_text(str(exc), context)
                parsed_message = _exception_attr(exc, "message")
                broker_message = (
                    display_text
                    if parsed_message is None
                    else self._tokenizer.sanitize_text(str(parsed_message), context)
                )
                entry["error"] = self._codec.encode(
                    {
                        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                        "text": display_text,
                        "message": broker_message,
                        "status_code": _exception_attr(exc, "status_code"),
                        "code": _exception_attr(exc, "code"),
                    },
                    context=f"{context}.error",
                )
            raise
        with self._lock:
            try:
                entry["answer"] = self._codec.encode(
                    answer, context=f"{context}.answer"
                )
            except Exception as exc:
                # A response has already happened. Never turn a successful
                # broker action into a caller-visible failure that could make
                # the caller retry it. Export still fails closed below.
                entry["capture_error"] = type(exc).__name__
        return answer

    def to_payload(self) -> dict[str, Any]:
        with self._lock:
            incomplete = [
                index
                for index, entry in enumerate(self._entries)
                if "capture_error" in entry
                or ("answer" not in entry and "error" not in entry)
            ]
            if incomplete:
                raise BrokerCassetteError(
                    "cannot export cassette with incomplete or unserializable broker calls"
                )
            return {"schema": SCHEMA, "entries": copy.deepcopy(self._entries)}


class RecordingBrokerClient:
    """Transparent recording proxy around a real Alpaca SDK client."""

    def __init__(self, client, cassette: BrokerCassette, client_name: str):
        self._client = client
        self._cassette = cassette
        self._client_name = client_name

    def __getattr__(self, name: str):
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        @wraps(attribute)
        def recorded(*args, **kwargs):
            return self._cassette.invoke(
                self._client_name, name, attribute, args, kwargs
            )

        return recorded


class ReplayBrokerCassette:
    """Strict global-order replay shared by named SDK-client stand-ins."""

    def __init__(self, payload: Mapping[str, Any]):
        if payload.get("schema") != SCHEMA:
            raise BrokerCassetteError("unsupported broker cassette schema")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise BrokerCassetteError("broker cassette entries must be a list")
        self._entries = copy.deepcopy(entries)
        self._cursor = 0
        self._lock = threading.Lock()
        self._tokenizer = _IdentifierTokenizer()
        self._codec = _Codec(self._tokenizer)

    def client(self, client_name: str):
        return _ReplayBrokerClient(self, client_name)

    def invoke(self, client_name: str, method_name: str, args, kwargs):
        context = f"{client_name}.{method_name}"
        with self._lock:
            actual = {
                "client": client_name,
                "method": method_name,
                "args": self._codec.encode(list(args), context=f"{context}.args"),
                "kwargs": self._codec.encode(
                    dict(kwargs), context=f"{context}.kwargs"
                ),
            }
            if self._cursor >= len(self._entries):
                raise MissingRecordedBrokerCall(
                    f"unrecorded broker call at index {self._cursor}: "
                    f"{client_name}.{method_name}"
                )
            expected = self._entries[self._cursor]
            expected_call = {
                key: expected.get(key) for key in ("client", "method", "args", "kwargs")
            }
            if actual != expected_call:
                raise CassetteMismatch(
                    f"broker call {self._cursor} did not match the recorded call"
                )
            self._cursor += 1
            if "error" in expected:
                error = self._codec.decode(expected["error"])
                display_text = error.get(
                    "text", error.get("message", "recorded broker error")
                )
                raise RecordedBrokerError(
                    display_text,
                    error_type=error.get("type", "broker error"),
                    status_code=error.get("status_code"),
                    code=error.get("code"),
                    broker_message=error.get("message", display_text),
                )
            if "answer" not in expected:
                raise BrokerCassetteError("recorded call has neither answer nor error")
            return self._codec.decode(expected["answer"])

    def assert_consumed(self) -> None:
        remaining = len(self._entries) - self._cursor
        if remaining:
            raise UnusedRecordedBrokerCalls(
                f"{remaining} recorded broker call(s) were not consumed"
            )


class _ReplayBrokerClient:
    def __init__(self, cassette: ReplayBrokerCassette, client_name: str):
        self._cassette = cassette
        self._client_name = client_name

    def __getattr__(self, name: str):
        def replayed(*args, **kwargs):
            return self._cassette.invoke(self._client_name, name, args, kwargs)

        return replayed
