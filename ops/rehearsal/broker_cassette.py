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
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Mapping

from ops.rehearsal.broker_cassette_codec import (
    Codec as _Codec,
    IdentifierTokenizer as _IdentifierTokenizer,
    SCHEMA,
)
from ops.rehearsal.isolation import SENTINEL_KEY
from ops.rehearsal.public_bundle import assert_public_safe


class BrokerCassetteError(RuntimeError):
    """Base class for deterministic cassette failures."""


class CassetteMismatch(BrokerCassetteError):
    """The next replay call differs from the next recorded call."""


class MissingRecordedBrokerCall(BrokerCassetteError):
    """Replay made a call after the cassette was exhausted."""


class UnusedRecordedBrokerCalls(BrokerCassetteError):
    """Replay finished before every recorded call was consumed."""


class BrokerReplayViolation(BrokerCassetteError):
    """A strict replay failure occurred, even if application code caught it."""


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
        self._violations: list[str] = []
        self.submitted: list[_ReplaySubmittedOrder] = []
        self._cancel_recording_client = None
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
                exc = MissingRecordedBrokerCall(
                    f"unrecorded broker call at index {self._cursor}: "
                    f"{client_name}.{method_name}"
                )
                self._violations.append(str(exc))
                raise exc
            expected = self._entries[self._cursor]
            expected_call = {
                key: expected.get(key) for key in ("client", "method", "args", "kwargs")
            }
            if actual != expected_call:
                exc = CassetteMismatch(
                    f"broker call {self._cursor} did not match the recorded call"
                )
                self._violations.append(str(exc))
                raise exc
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
            answer = self._codec.decode(expected["answer"])
            if client_name == "trading" and method_name == "submit_order":
                self.submitted.append(_ReplaySubmittedOrder.from_call(args, kwargs, answer))
            return answer

    def assert_consumed(self) -> None:
        if self._violations:
            raise BrokerReplayViolation(
                "broker replay violated its recording: " + self._violations[0]
            )
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


@dataclass(frozen=True)
class _ReplaySubmittedOrder:
    """Report only facts present in a consumed submit call and its answer."""

    plain: dict[str, Any]

    @classmethod
    def from_call(cls, args, kwargs, answer):
        request = kwargs.get("order_data")
        if request is None and args:
            request = args[0]

        def value(name, owner):
            raw = getattr(owner, name, None) if owner is not None else None
            return getattr(raw, "value", raw)

        qty = value("qty", request)
        return cls({
            "id": None if value("id", answer) is None else str(value("id", answer)),
            "symbol": value("symbol", answer) or value("symbol", request),
            "side": value("side", request),
            "qty": None if qty is None else float(qty),
            "type": value("type", request),
            "limit_price": value("limit_price", request),
            "stop_price": value("stop_price", request),
            "status": value("status", answer),
        })

    def as_plain(self) -> dict[str, Any]:
        return dict(self.plain)


def install_replay_broker_cassette(broker, payload: Mapping[str, Any]):
    """Install strict offline SDK clients into an existing ``AlpacaBroker``.

    Both clients are installed before the session can ask ``AlpacaBroker`` to
    lazily construct its normal SDK data client.  The trade-updates stream is
    deliberately unsupported: the current cassette records method calls, not
    websocket events, so enabling it would either escape the socket wall or
    require invented fills.  Refuse that configuration instead.
    """
    assert_public_safe(payload)
    if broker.fill_stream_enabled():
        raise BrokerCassetteError(
            "cassette replay cannot run with execution.fill_stream_enabled=true: "
            "trade_updates are not recorded"
        )

    replay = ReplayBrokerCassette(payload)
    trading_client = replay.client("trading")
    data_client = replay.client("stock_historical_data")
    from src.sentinel.cancel_attempts import CancelRecordingClient

    current_client = getattr(broker, "client", None)
    if isinstance(current_client, CancelRecordingClient):
        current_client._inner = trading_client
        replay._cancel_recording_client = current_client
        from src.sentinel.guarded import attach_reconciliation_db

        attach_reconciliation_db(trading_client, current_client._conn_getter)
    else:
        broker.client = trading_client
    broker._data_client = data_client
    broker.api_key = SENTINEL_KEY
    broker.secret_key = SENTINEL_KEY
    broker._trading_day_cache = {}
    broker._session_open_cache = {}
    return replay


def assert_broker_uses_replay(broker, replay: ReplayBrokerCassette) -> str:
    """Prove the constructed broker holds only this cassette's clients."""
    trading_client = getattr(broker, "client", None)
    if replay._cancel_recording_client is not None:
        if trading_client is not replay._cancel_recording_client:
            raise BrokerCassetteError("broker cancel-recording wrapper was discarded")
        trading_client = trading_client._inner
    clients = (trading_client, getattr(broker, "_data_client", None))
    expected_names = ("trading", "stock_historical_data")
    for client, name in zip(clients, expected_names):
        if not isinstance(client, _ReplayBrokerClient):
            raise BrokerCassetteError(f"broker {name} client is not a replay client")
        if client._cassette is not replay or client._client_name != name:
            raise BrokerCassetteError(f"broker {name} client belongs to another replay")
    if (
        getattr(broker, "api_key", None) != SENTINEL_KEY
        or getattr(broker, "secret_key", None) != SENTINEL_KEY
    ):
        raise BrokerCassetteError("broker is holding non-sentinel credentials")
    if broker.fill_stream_enabled():
        raise BrokerCassetteError("broker trade_updates stream is enabled during replay")
    return (
        "both Alpaca SDK clients are strict cassette replays holding sentinel "
        "credentials; trade_updates is disabled"
    )


@dataclass
class RehearsalBrokerTransport:
    """One installed synthetic or strict-cassette rehearsal transport."""

    trading_stub: Any = None
    replay: ReplayBrokerCassette | None = None
    fill_model: str = "immediate"
    notes: list[str] = field(default_factory=list)

    def assert_installed(self, broker) -> str:
        if self.replay is not None:
            return assert_broker_uses_replay(broker, self.replay)
        from ops.rehearsal.isolation import assert_broker_is_stubbed

        return assert_broker_is_stubbed(broker)

    def record_missing_prices(self, broker, unavailable: list[str]) -> None:
        if self.replay is not None:
            return
        for symbol in getattr(broker._data_client, "missing_price_symbols", []):
            unavailable.append(f"a current price for {symbol}")

    def assert_complete(self) -> str:
        if self.replay is not None:
            self.replay.assert_consumed()
            return "every recorded broker call was consumed exactly once"
        from ops.rehearsal.stand_in import assert_stand_in_answered

        return assert_stand_in_answered(self.trading_stub)


def install_rehearsal_broker_transport(
    broker, snapshot, *, now, fill_model: str, payload=None
) -> RehearsalBrokerTransport:
    """Select the old synthetic broker unless strict replay is explicit."""
    if payload is None:
        from ops.rehearsal.broker import install_rehearsal_broker

        trading_stub = install_rehearsal_broker(
            broker, snapshot, now=now, fill_model=fill_model
        )
        return RehearsalBrokerTransport(
            trading_stub=trading_stub, fill_model=fill_model
        )
    replay = install_replay_broker_cassette(broker, payload)
    return RehearsalBrokerTransport(
        trading_stub=replay,
        replay=replay,
        fill_model="cassette",
        notes=[
            "broker SDK calls came from an explicit strict cassette replay; "
            "a synthetic cassette proves this wiring only and does not "
            "complete item 233"
        ],
    )
