"""The `trade_updates` authentication handling, lifted verbatim from trade_stream.py."""
from __future__ import annotations

import asyncio
import json
import logging

from src.execution.broker_parts.trade_stream_bounds import _ALPACA_STREAM_AUTH_DEADLINE_S
from src.sentinel.guarded import record_guarded_pass
from src.session_identity import _credential_fingerprint

logger = logging.getLogger("src.execution.broker")

# THE trade_updates AUTH FORMAT, AND WHY THIS MODULE SENDS THE FRAME.
#
# alpaca-py builds the auth payload itself, in
# `alpaca/trading/stream.py::TradingStream._auth`, as
#   {"action":"authenticate","data":{"key_id":K,"secret_key":S}}
# Alpaca's own authorization reply says that form is being DEPRECATED in
# favour of
#   {"action":"auth","key":K,"secret":S}
# Measured against the live paper broker 2026-09-18: both forms return
# `status: authorized`, and the deprecated one carries the notice.
#
# A dependency bump would be the right fix and there is nothing to bump to:
# alpaca-py 0.44.0, the newest release on PyPI on that date, still sends the
# deprecated form. So the choice is between sending a frame the counterparty
# has told us it is retiring, or sending the current one ourselves. The desk
# sends the current one, on the smallest possible surface: ONE frame, with
# alpaca-py's own verdict rule (`data.status == "authorized"`) unchanged, on
# a per-instance wrapper. Nothing in site-packages is edited.
#
# THE FALLBACK IS LOAD-BEARING AND STAYS. The current form is proven on
# `paper-api.alpaca.markets` and nowhere else; the desk has exactly one
# account and cannot show it is accepted everywhere the old one is. So a
# refusal of the current form marks the stream and the NEXT handshake sends
# the deprecated form on a fresh socket — see `_fell_back_to_deprecated_auth`
# for why it is not re-sent on the same socket. Either way the reconnect
# ceiling bounds the sequence, the owner is told once in plain English, and
# fills fall back to the bounded REST path. Retire the fallback only when
# the current form is proven on every host the desk authenticates against.
_STREAM_AUTH_DEPRECATION_MARKER = "deprecat"
_stream_auth_deprecation_logged = False
_stream_current_auth_format_logged = False


class TradeStreamAuthRejected(Exception):
    """The broker REFUSED the trade_updates credential, in the broker's own words.

    Why this class exists (2026-09-18). For a fortnight this failure was
    indistinguishable from a transport fault in the desk's logs, and that
    is what cost six pull requests of connection re-sequencing:

      * The installed SDK's `TradingStream._auth` compares
        `msg["data"]["status"] != "authorized"` and then raises a bare
        `ValueError("failed to authenticate")` — it DISCARDS the reply.
        Alpaca actually sends
        `{"stream":"authorization","data":{"message":"code=401, message=Unauthorized","status":"unauthorized"}}`
        and that `message` never reached any log line.
      * A `ValueError` carries no HTTP-status attribute, so
        `_stream_http_status` returned None and the desk logged
        `status=unknown` — which reads as "the socket would not open",
        not "the password was wrong".

    This exception carries Alpaca's own `message`/`status` plus a
    NON-REVEALING credential fingerprint (length and first two characters
    only). The secret is never touched, and the key is never logged.
    """

    def __init__(
        self,
        *,
        broker_message: str | None,
        broker_status: str | None,
        credential: str | None,
        cause: BaseException | None = None,
    ) -> None:
        self.broker_message = broker_message
        self.broker_status = broker_status
        self.credential_fingerprint = _credential_fingerprint(credential)
        super().__init__(
            "broker refused the trade_updates credential "
            f"(broker said: {broker_message or 'no message returned'}; "
            f"broker status: {broker_status or 'not stated'}; "
            f"api key {self.credential_fingerprint})"
        )
        self.__cause__ = cause


def _parse_stream_auth_reply(raw: object) -> tuple[str | None, str | None]:
    """(message, status) out of Alpaca's authorization frame. Never raises.

    Shape per Alpaca's streaming docs and observed rejections:
    `{"stream":"authorization","data":{"message":...,"status":...}}`.
    Anything unparseable degrades to (None, None) — a missing diagnostic
    must never replace the failure it was added to explain.
    """
    if raw is None:
        return None, None
    payload: object = raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            payload = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None, None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            # Not JSON: the body itself is the most informative thing we have.
            text = str(raw).strip()
            return (text[:200] or None), None
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    message = data.get("message")
    status = data.get("status")
    return (
        str(message) if message is not None else None,
        str(status) if status is not None else None,
    )


def _install_trading_stream_auth_diagnostics(stream: object) -> None:
    """Keep Alpaca's authorization reply so a refusal can be logged verbatim.

    Deliberately NOT a monkeypatch of the vendor SDK and NOT a
    reimplementation of the auth protocol: the SDK's own `_auth` still
    sends the frame and still decides the outcome. We only (a) record the
    first frame the socket hands back during auth, via a one-shot wrapper
    on this instance's `recv`, (b) translate the SDK's information-free
    `ValueError` into `TradeStreamAuthRejected` carrying that frame, and
    (c) on SUCCESS, surface any deprecation notice the broker put in that
    same frame instead of discarding it — see
    `_note_stream_auth_deprecation` and `_STREAM_AUTH_DEPRECATION_MARKER`.

    Both wrappers are per-instance attributes on objects this module
    constructed. Nothing in site-packages is edited. No timeout, retry
    count or backoff is introduced — the reconnect guard owns all of those
    and is untouched.

    No-op when the object has no `_auth` (the test doubles).
    """
    original_auth = getattr(stream, "_auth", None)
    if not callable(original_auth):
        return

    async def _send_current_auth_format() -> None:
        """Send the format Alpaca asks for, and apply the SDK's own verdict.

        Identical decision rule to alpaca-py's `_auth` — `data.status` must
        read `authorized`, otherwise `ValueError`, which is what every
        caller and test in this module already expects. Only the frame
        differs, because only the frame is what the broker deprecated.
        """
        ws_now = getattr(stream, "_ws", None)
        await ws_now.send(json.dumps({
            "action": "auth",
            "key": getattr(stream, "_api_key", None),
            "secret": getattr(stream, "_secret_key", None),
        }))
        raw = await ws_now.recv()
        msg = json.loads(raw)
        data = msg.get("data") or {}
        if data.get("status") != "authorized":
            raise ValueError("failed to authenticate")

    async def _auth():
        captured: dict[str, object] = {}
        ws = getattr(stream, "_ws", None)
        original_recv = getattr(ws, "recv", None) if ws is not None else None
        restored = False

        def _restore() -> None:
            nonlocal restored
            if restored or ws is None or original_recv is None:
                return
            restored = True
            try:
                ws.recv = original_recv  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(None, "trade_stream.auth_capture.restore_recv", exc, log=logger,
                                    context={"effect": "recv wrapper stays on the socket"})

        if callable(original_recv):
            async def _recv_once():
                raw = await original_recv()
                if "raw" not in captured:
                    captured["raw"] = raw
                    _restore()
                return raw

            try:
                ws.recv = _recv_once  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(None, "trade_stream.auth_capture.wrap_recv", exc, log=logger,
                                    context={"effect": "auth reply not captured"})
                original_recv = None

        # WHICH FRAME THIS HANDSHAKE SENDS. New form unless a previous
        # handshake on THIS stream was refused with it — see
        # `_fell_back_to_deprecated_auth`.
        use_deprecated = bool(getattr(stream, "_qamc_auth_fallback", False))
        if getattr(stream, "_ws", None) is None:
            # No socket to send our own frame on. Hand the whole handshake
            # back to the vendor rather than raise a shape error the
            # reconnect guard would report as a broker fault.
            use_deprecated = True
        attempt_auth = original_auth if use_deprecated else _send_current_auth_format
        try:
            # BOUND THE HANDSHAKE. alpaca-py's `_auth` awaits `self._ws.recv()`
            # with NO timeout (verified in 0.43.5 and 0.44.0), while its own
            # `_consume` bounds the identical call at 5s. So a broker that
            # stops ANSWERING — which is one way the deprecated auth format
            # could be retired — parks this thread in `_auth` forever: no
            # exception, so the reconnect guard never counts a failure, the
            # session ceiling never engages, the owner is never told, and
            # `trade_updates_started()` keeps reporting a live hub. Fill waits
            # would still fall to REST on their own deadline, so the desk
            # survives; the socket would just lie about being up.
            #
            # Same budget the hub already gives the handshake
            # (`_ALPACA_STREAM_AUTH_DEADLINE_S`), not a second clock. A
            # timeout raises out of `_start_ws`, which is exactly the shape
            # the ceiling already counts and gives up on.
            await asyncio.wait_for(
                attempt_auth(), timeout=_ALPACA_STREAM_AUTH_DEADLINE_S,
            )
        except asyncio.TimeoutError as exc:
            message, status = _parse_stream_auth_reply(captured.get("raw"))
            raise TradeStreamAuthRejected(
                broker_message=(
                    message
                    or "no reply to the authentication frame within "
                       f"{_ALPACA_STREAM_AUTH_DEADLINE_S:.0f}s"
                ),
                broker_status=status or "no reply",
                credential=getattr(stream, "_api_key", None),
                cause=exc,
            ) from exc
        except ValueError as exc:
            if not use_deprecated:
                # The CURRENT format was refused. Do NOT re-send on this
                # socket: Alpaca closes a connection it refused, so a second
                # frame here would fail for a reason that has nothing to do
                # with the format and would look like a credential problem.
                # Mark the stream instead; the reconnect guard's next
                # handshake opens a fresh socket and sends the deprecated
                # form, which is the one measured to work today. The ceiling
                # still bounds the whole sequence.
                _fell_back_to_deprecated_auth(stream, captured.get("raw"))
            message, status = _parse_stream_auth_reply(captured.get("raw"))
            raise TradeStreamAuthRejected(
                broker_message=message,
                broker_status=status,
                credential=getattr(stream, "_api_key", None),
                cause=exc,
            ) from exc
        else:
            if not use_deprecated:
                _note_current_auth_format_accepted()
            _note_stream_auth_deprecation(captured.get("raw"))
        finally:
            _restore()

    stream._auth = _auth


def _fell_back_to_deprecated_auth(stream: object, raw: object) -> None:
    """Mark a stream so its NEXT handshake sends the deprecated auth frame.

    Loud, because this is the fail-visible half of the migration: the desk
    is now sending a format the broker has said it is retiring, and the
    only alternative to saying so is silently degrading.

    Marked per-stream rather than per-process: a refusal is evidence about
    the socket in front of us, and a process-wide latch would pin every
    later socket to the old form on one bad handshake.
    """
    try:
        setattr(stream, "_qamc_auth_fallback", True)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(None, "trade_stream.auth_fallback.mark", exc, log=logger,
                            context={"effect": "fallback flag not set"})
        return
    message, status = _parse_stream_auth_reply(raw)
    logger.warning(
        "trade_updates auth: the CURRENT format {\"action\":\"auth\"} was "
        "refused (broker said: %s; broker status: %s) — the next handshake "
        "falls back to the deprecated format on a fresh socket. If the "
        "credential is good, this means the current format is not accepted "
        "here and the fallback is load-bearing.",
        message or "no message returned",
        status or "not stated",
    )


def _note_current_auth_format_accepted() -> None:
    """Record, once per process, that the non-deprecated frame was accepted.

    Without this line there is no positive evidence in the desk's own log
    that the migration took — only the absence of a failure, which is what
    let a socket that never authenticated look healthy for three days.
    """
    global _stream_current_auth_format_logged
    if _stream_current_auth_format_logged:
        return
    _stream_current_auth_format_logged = True
    logger.info(
        "trade_updates authenticated with the CURRENT auth format "
        "({\"action\":\"auth\"}) — the deprecated format alpaca-py builds "
        "was not used",
    )


def _note_stream_auth_deprecation(raw: object) -> None:
    """Log Alpaca's deprecation notice once per process, in its own words.

    The authorization reply to a SUCCESSFUL handshake was captured for the
    refusal path and then thrown away, so the broker telling us our auth
    format is going away reached the desk and left no trace. That is a
    silent degradation waiting to happen: the day Alpaca enforces it, the
    only record would be a handshake that stopped working.

    WARNING, not ERROR: nothing is broken yet and nothing is required of
    anyone today. Once per process, never per attempt — the 2026-09-15
    storm is what a per-attempt line costs. Never raises, never carries a
    credential (the reply frame contains neither).
    """
    global _stream_auth_deprecation_logged
    if _stream_auth_deprecation_logged:
        return
    try:
        message, _status = _parse_stream_auth_reply(raw)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not break the handshake
        record_guarded_pass(None, "trade_stream.auth_deprecation.parse", exc, log=logger,
                            context={"effect": "deprecation notice not logged"})
        return
    if not message or _STREAM_AUTH_DEPRECATION_MARKER not in message.lower():
        return
    _stream_auth_deprecation_logged = True
    logger.warning(
        "trade_updates auth format is DEPRECATED by the broker — broker "
        "said: %s. The payload is built by alpaca-py "
        "(TradingStream._auth), not by this desk, and the newest release "
        "still sends the old form; the handshake is accepted today. See "
        "_STREAM_AUTH_DEPRECATION_MARKER in this module.",
        message,
    )
