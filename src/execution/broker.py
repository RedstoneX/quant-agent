import asyncio
import functools
import fcntl
import json
import logging
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date
from dataclasses import dataclass
from pathlib import Path

import yfinance as yf
from alpaca.trading.client import TradingClient
try:
    from alpaca.trading.stream import TradingStream
except ImportError:  # pragma: no cover - optional dependency surface
    TradingStream = None
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, StopLimitOrderRequest,
    StopOrderRequest,
    TakeProfitRequest, StopLossRequest, ReplaceOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus

from src.stop_cancel_outcome import (StopCancelOutcome, StopCoverageLost, settle_cancel)
from src.models import Position
from src import sector_reference as _sector_reference
# THE stop-value judgement (docs/WORK.md item 88). `src.execution.stop_records`
# imports nothing from this module, so this is a leaf dependency.
from src.execution.stop_records import STOP_USABLE, classify_stop_price
from src.execution.held_qty import cover_qty_for_rearm
from src.execution.broker_parts.stop_amend import (  # noqa: F401 (re-exports keep patch targets)
    StopAmender, _AMEND_NOT_ATTEMPTED, _is_terminal_broker_rejection, _quantize_price,
)
from src.execution.broker_parts.stop_place import (  # noqa: F401 (re-exports keep patch targets)
    StopPlacer, PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES, PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES, _STOP_PLACEMENT_MAX_ATTEMPTS, _STOP_PLACEMENT_BACKOFF_S, _FRACTIONAL_QTY_EPSILON, PROTECTIVE_ORDER_ACTIVE_STATUSES, _is_held_for_orders_error, _is_unsupported_stop_market_rejection, _split_protective_qty, _derive_stop_tif, _alpaca_symbol, _internal_symbol, real_broker_order_id,
)
from src.execution.broker_parts.order_desk import (  # noqa: F401 (re-exports keep patch targets)
    OrderDesk, _PLAIN_PRICE_LABELS, _outlier_refusal_detail, _is_terminal_submission_rejection,
)
from src.execution.broker_parts.account_reads import AccountReads
from src.sentinel.guarded import attach_reconciliation_db, record_guarded_pass  # noqa: F401 (re-export)
from src.execution.order_gates import BadOrderQuantity, QTY_REJECTED, check_order_quantity  # noqa: F401 (re-exports keep patch targets)
from src.execution.order_idempotency import _client_order_id, _is_dead_stop_result, _session_date_key  # noqa: F401 (re-exports keep patch targets)
from src.execution.broker_parts.trade_stream import (  # noqa: F401 (re-exports keep patch targets)
    TradeStreamAuthRejected, TradeStreamGaveUp, TradeStreamWarmup, _ALPACA_STREAM_AUTH_DEADLINE_S,
    _ALPACA_STREAM_RECONNECT_MAX_S, _ALPACA_STREAM_RECONNECT_MIN_S, _HubWaiter,
    _STREAM_ATTEMPT_BUDGET, _STREAM_ATTEMPT_CEILING_PER_DAY, _STREAM_ATTEMPT_CEILING_PER_SESSION,
    _STREAM_AUTH_DEPRECATION_MARKER, _STREAM_RATE_LIMIT_STAND_DOWN_S, _StreamAttemptBudget,
    _TRADE_UPDATES_STREAM_LOCK, _TradeUpdatesHub, _TradeUpdatesLease, _alert_stream_gave_up,
    _credential_fingerprint, _default_trade_updates_lease_path, _equal_jitter_backoff,
    _fell_back_to_deprecated_auth, _install_trading_stream_auth_diagnostics,
    _install_trading_stream_reconnect_guard, _note_current_auth_format_accepted,
    _note_stream_auth_deprecation, _parse_stream_auth_reply, _stream_giveup_owner_message,
    _stream_http_status, _stream_retry_after_seconds, _trading_stream_reconnect_delay,
    TradeStreamWaits,
)
from src.execution.broker_parts.market_data import (  # noqa: F401 (re-exports keep patch targets)
    LivePrice, _BROKER_HTTP_TIMEOUT, _install_http_timeout, MarketData,
)

logger = logging.getLogger(__name__)

# `_PLAIN_PRICE_LABELS` moved to src/execution/broker_parts/order_desk.py (re-exported above).


# `_outlier_refusal_detail` moved to src/execution/broker_parts/order_desk.py (re-exported above).

# The trade_updates stream plumbing moved to src/execution/broker_parts/trade_stream.py (re-exported above).


# `_BROKER_HTTP_TIMEOUT` moved to src/execution/broker_parts/market_data.py (re-exported above).

# 2026-09-10: 15 -> 30 -> 90. `wait_for_order_terminal` now watches Alpaca's
# real-time trade_updates stream first (see that method) — a fill is
# detected the instant Alpaca reports it, not on the next poll tick, so this
# number no longer trades speed against safety in the common case. It is
# now purely the ceiling for the RARE path where the stream itself could
# not be used (import/auth/network failure) and the code falls back to REST
# polling exactly as before. 90 seconds is the originally-researched value
# (an ordinary marketable limit on a liquid US equity fills in seconds, but
# a stale/illiquid DAY order should be given real room before being pulled)
# — it was never actually shipped because the fallback-only framing didn't
# exist yet. Bounded well below a stale DAY order regardless. Later entries
# in a submission burst have already rested while earlier entries are
# finalized, so this is a conservative ceiling, not a blind per-order sleep
# added to every order.
_ENTRY_FILL_TIMEOUT_S = 90.0

# `_STOP_PLACEMENT_MAX_ATTEMPTS` moved to src/execution/broker_parts/stop_place.py (re-exported above).
# `_STOP_PLACEMENT_BACKOFF_S` moved to src/execution/broker_parts/stop_place.py (re-exported above).

# `_FRACTIONAL_QTY_EPSILON` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_AMEND_NOT_ATTEMPTED` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_is_held_for_orders_error` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_is_terminal_broker_rejection` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_is_terminal_submission_rejection` moved to src/execution/broker_parts/order_desk.py (re-exported above).


# `_is_unsupported_stop_market_rejection` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_split_protective_qty` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_derive_stop_tif` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_alpaca_symbol` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_internal_symbol` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_quantize_price` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_install_http_timeout` moved to src/execution/broker_parts/market_data.py (re-exported above).


#: Entry sides `place_entry_protection` will derive a protective side from.
#: Anything else is refused rather than guessed — see the fail-closed note in
#: `place_entry_protection`. "sell" and "sell_short" both open/extend a short.
_ENTRY_SIDES = frozenset({"buy", "sell", "sell_short"})

# `PROTECTIVE_ORDER_ACTIVE_STATUSES` moved to src/execution/broker_parts/stop_place.py (re-exported above).

# `PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES` moved to src/execution/broker_parts/stop_place.py (re-exported above).

#: The set for the OTHER question: "would submitting another stop here
#: create a SECOND live order against the same shares?"
#:
#: Its reader (`TradingPipeline._reprotect_residual`'s idempotency check)
#: is looking at a stop this desk placed SECONDS ago on a prior attempt of
#: the same reprotect, after excluding by order id every stop this run
#: itself cancelled. Nothing in this codebase reconciles a duplicate
#: protective stop (see `src/coverage_watchdog.py`, which states it never
#: cancels or modifies; the only duplicate handling anywhere is a message
#: asking the owner to cancel one by hand), so the duplicate must be
#: prevented rather than cleaned up.
#:
#: CORRECTED 2026-10-01 (adversary round 2, defect 2): that reader no
#: longer treats this union as one answer. A `pending_new` stop can still
#: become `rejected`, so it is neither protection to bank nor an order to
#: place a second stop over; the reprotect path reads the two member sets
#: SEPARATELY and gives the in-flight case its own outcome — no write-back,
#: no drain of the recovery intent, re-read on the next pass. The union is
#: kept as the vocabulary for "neither terminal nor dying".
#:
#: `pending_cancel` stays OUT of both sets: a dying order is never
#: protection, whichever question is being asked.
PROTECTIVE_ORDER_ALIVE_STATUSES = (
    PROTECTIVE_ORDER_ACTIVE_STATUSES | PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES
)


# `real_broker_order_id` moved to src/execution/broker_parts/stop_place.py (re-exported above).


def _is_broker_class_shim(obj, attr: str) -> bool:
    """True when `obj` is AlpacaBroker's own thin shim for `attr`, however it was
    bound: a bound method (`__func__`), a `functools.partial` over the plain
    function (`func`, unwrapped through nested partials), or the plain function."""
    target = getattr(AlpacaBroker, attr, None)
    if target is None:
        return False
    seen = obj
    for _ in range(8):
        if seen is target:
            return True
        if isinstance(seen, functools.partial):
            seen = seen.func
            continue
        bound = getattr(seen, "__func__", None)
        if bound is None:
            return False
        seen = bound
    return False


class AlpacaBroker:
    #: Set in __init__. Declared here so an instance built without __init__
    #: reads None rather than raising; `_kill_switch_active` already treats
    #: None as "no switch configured", i.e. inert.
    _kill_switch_path: "Path | None" = None
    #: Set in __init__ (RiskConfig via src/pipeline.py). None = no notional
    #: check; declared here so an instance built without __init__ reads None.
    _max_position_pct: "float | None" = None
    #: Called with the facts of every protective stop the kill switch
    #: refuses (see `_submit_stop_limit_order`). The broker holds no
    #: database, so the owner of one wires this — `TradingPipeline` does, to
    #: `src/execution/exit_path_records.record_protective_stop_blocked`.
    #: None (the default) records nothing, exactly as before. Recording
    #: only: its result and any exception it raises are ignored.
    protective_stop_block_recorder: "object | None" = None
    #: Live `trade_updates` websocket feed. Declared here, FALSE, for the
    #: same reason as `_kill_switch_path` above: an instance built without
    #: __init__ must read the safe value rather than raise. Fail-closed
    #: direction is OFF — a construction site that never threads the flag
    #: through (the read-only broker in src/api/broker_reads.py, the
    #: heartbeat, one-off scripts, isolated unit tests) must not be able to
    #: open the account's single socket by omission. See
    #: `ExecutionConfig.fill_stream_enabled` for the flag's history (off
    #: 2026-09-17, on in production again since 2026-09-18);
    #: src/pipeline.py is the only site that passes it.
    _fill_stream_enabled: bool = False
    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 kill_switch_path: str | None = None,
                 trade_updates_lease_path: str | None = None,
                 fill_stream_enabled: bool = False,
                 max_position_pct: float | None = None):
        self.api_key = api_key
        self._max_position_pct = max_position_pct  # RiskConfig via src/pipeline.py; None = no notional check
        self.secret_key = secret_key
        self._paper = paper
        self.client = TradingClient(api_key, secret_key, paper=paper)
        _install_http_timeout(self.client)
        self._data_client = None
        # In-memory cache for completed (closed) price bars only — see
        # get_bars / get_intraday_chart_bars. Keyed so that the current,
        # still-forming trading day is never stored and always refetched
        # fresh; only prior, fully-closed days are ever served from here.
        self._closed_bars_cache: dict = {}
        self._closed_bars_cache_lock = threading.Lock()
        # Guard 1 (2026-09-02 operational safety guard — see
        # RiskConfig.kill_switch_path). `None` leaves the guard disabled,
        # which is only reachable from a construction site that predates
        # this parameter and never threads a path through (e.g. an isolated
        # unit test building `AlpacaBroker` directly) — `src/pipeline.py`
        # always passes the configured path. See `_kill_switch_active`.
        self._kill_switch_path = (
            Path(kill_switch_path) if kill_switch_path else None
        )
        # Per-date cache for is_trading_day. Trading-day status is set by
        # the exchange calendar months in advance — invariant within the
        # day — so a per-date dict that grows unbounded over a multi-year
        # process lifetime is still fine (1 entry per calendar day ≈ a
        # few KB / year).
        self._trading_day_cache: dict[date, bool] = {}
        # Per-date cache for get_session_open, same lifetime/invariance
        # argument as `_trading_day_cache` above — the exchange's regular
        # session open (including early-close-day exceptions) is fixed by
        # the calendar in advance. Backs `broker_reads._quote_freshness`
        # (docs/WORK.md item 15), which may run on every /quotes read.
        self._session_open_cache: dict[date, "datetime | None"] = {}
        # Stage 3 (shorts, D6). Per-run cache, same shape/lifetime as
        # `_trading_day_cache` above — a symbol's shortable/easy_to_borrow
        # flags don't change intra-session, so one asset-directory lookup
        # per symbol per process is enough.
        self._shortable_cache: dict[str, dict] = {}
        # Spec §11.1. Same shape/lifetime and same reasoning as
        # `_shortable_cache`: `fractionable` is an asset-directory fact that
        # does not change intra-session.
        self._fractionable_cache: dict[str, dict] = {}
        self._last_stream_warmup: TradeStreamWarmup | None = None
        self._trade_hub: _TradeUpdatesHub | None = None
        self._trade_hub_lock = threading.Lock()
        self._trade_lease = _TradeUpdatesLease(
            Path(trade_updates_lease_path or _default_trade_updates_lease_path()),
            owner=self,
        )
        self._trade_slot_held = False
        self._trade_lease_contended = False
        self._fill_stream_enabled = bool(fill_stream_enabled)
        self._fill_stream_off_logged = False

    def _kill_switch_active(self) -> bool:
        """Guard 1: True once ops has halted the desk by `touch`-ing the
        configured flag file.

        `path.exists()` and NOTHING else — no read, no parse, no schema —
        so a zero-byte file, a file full of garbage, and a file the operator
        can no longer remember the format of all halt identically. The
        check cannot fail open on bad content because it never looks at any
        content.

        Called at the top of every method on this class that places or
        replaces an order at the broker (`submit_order`,
        `_submit_stop_limit_order`, `replace_entry_limit`) — deliberately
        including the exit and protective-stop paths. See
        RiskConfig.kill_switch_path for why this is the one guard in the
        codebase that also blocks a risk-reducing order.
        """
        return self._kill_switch_path is not None and self._kill_switch_path.exists()

    def _account_reads(self) -> AccountReads:
        """Thin shim: builds the standalone reads object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/account_reads.py). Built per call so a
        client swapped after construction is what the body sees; the caches are this
        broker's own dicts, mutated in place."""
        return AccountReads(
            client=self.client,
            shortable_cache=self._shortable_cache,
            fractionable_cache=self._fractionable_cache,
            trading_day_cache=self._trading_day_cache,
            session_open_cache=self._session_open_cache,
            # `_session_edge` is itself a moved body -- same recursion guard as
            # `_stop_placer`: pass it ONLY when it is NOT this broker's shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (("session_edge", "_session_edge"),)
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def get_account(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_account(*args, **kwargs)

    def get_margin_interest_activities(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_margin_interest_activities(*args, **kwargs)

    def get_all_account_activities(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_all_account_activities(*args, **kwargs)

    def get_transient_equity_eligibility(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_transient_equity_eligibility(*args, **kwargs)

    def list_assets(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().list_assets(*args, **kwargs)

    def get_asset_record(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_asset_record(*args, **kwargs)

    def get_shortability(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_shortability(*args, **kwargs)

    def get_fractionability(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_fractionability(*args, **kwargs)

    def get_recent_daily_closes(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_recent_daily_closes(*args, **kwargs)

    def get_full_portfolio_history(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_full_portfolio_history(*args, **kwargs)

    def get_positions(self) -> list[Position]:
        raw_positions = self.client.get_all_positions()
        positions = []
        for p in raw_positions:
            symbol = _internal_symbol(p.symbol)
            positions.append(Position(
                symbol=symbol,
                qty=float(p.qty),
                avg_entry=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=float(p.market_value),
                unrealized_pnl=float(p.unrealized_pl),
                unrealized_intraday_pnl=float(getattr(p, "unrealized_intraday_pl", 0) or 0),
                sector=_sector_reference._get_sector(symbol),
            ))
        return positions

    def is_trading_day(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().is_trading_day(*args, **kwargs)

    def trading_sessions_held(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().trading_sessions_held(*args, **kwargs)

    def is_last_trading_day_of_quarter(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().is_last_trading_day_of_quarter(*args, **kwargs)

    def get_session_close(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_session_close(*args, **kwargs)

    def get_session_open(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_session_open(*args, **kwargs)

    def _session_edge(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads()._session_edge(*args, **kwargs)


    def _market_data(self) -> MarketData:
        """Thin shim: builds the standalone object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/market_data.py). Built per call so a
        collaborator set or swapped after construction is what the body sees."""
        return MarketData(
            state=self,
            api_key=self.api_key,
            secret_key=self.secret_key,
            closed_bars_cache=self._closed_bars_cache,
            closed_bars_cache_lock=self._closed_bars_cache_lock,
            # The collaborators below are themselves moved bodies, so the object
            # already owns them. Passing this broker's same-named shim would
            # overwrite its own method with a function that calls straight back
            # into it -- infinite recursion. Same guard as `_stop_placer`: pass one
            # ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("get_latest_price_stamped", "get_latest_price_stamped"),
                    ("_extract_symbol_payload", "_extract_symbol_payload"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def get_top_movers(self, *args, **kwargs):
        return self._market_data().get_top_movers(*args, **kwargs)

    def get_bars(self, *args, **kwargs):
        return self._market_data().get_bars(*args, **kwargs)

    def get_intraday_chart_bars(self, *args, **kwargs):
        return self._market_data().get_intraday_chart_bars(*args, **kwargs)

    def get_latest_price_stamped(self, *args, **kwargs):
        return self._market_data().get_latest_price_stamped(*args, **kwargs)

    def get_latest_price(self, *args, **kwargs):
        return self._market_data().get_latest_price(*args, **kwargs)

    def get_latest_quote(self, *args, **kwargs):
        return self._market_data().get_latest_quote(*args, **kwargs)

    def get_intraday_snapshots(self, *args, **kwargs):
        return self._market_data().get_intraday_snapshots(*args, **kwargs)

    def _extract_symbol_payload(self, *args, **kwargs):
        return self._market_data()._extract_symbol_payload(*args, **kwargs)


    def get_current_stop_price(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_current_stop_price(*args, **kwargs)


    def _order_desk(self) -> OrderDesk:
        """Thin shim: builds the standalone order desk from this broker's collaborators
        (bodies moved to src/execution/broker_parts/order_desk.py). Built per call so a
        client or cluster method swapped after construction is what the body sees."""
        return OrderDesk(
            client=self.client,
            kill_switch_active=self._kill_switch_active,
            kill_switch_path=self._kill_switch_path,
            wait_for_order_status=self._wait_for_order_status,
            wait_for_order_status_via_stream=self._wait_for_order_status_via_stream,
            get_latest_price=self.get_latest_price,
            order_terminal_states=self._ORDER_TERMINAL_STATES,
            order_replaceable_states=self._ORDER_REPLACEABLE_STATES,
            max_replacement_hops=self._MAX_REPLACEMENT_HOPS,
            get_fractionability=self.get_fractionability,
            get_account=self.get_account,
            max_position_pct=self._max_position_pct,
            # Four collaborators below are themselves moved bodies, so the desk
            # already owns them. Passing this broker's same-named shim would
            # overwrite the desk's own method with a function that calls
            # straight back into the desk -- infinite recursion. Same guard
            # as `_stop_placer`: pass one ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("wait_for_order_terminal", "wait_for_order_terminal"),
                    ("resolve_replacement_chain", "resolve_replacement_chain"),
                    ("wait_for_order_status_via_polling", "_wait_for_order_status_via_polling"),
                    ("list_open_entry_orders_checked", "list_open_entry_orders_checked"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def cancel_open_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_open_orders(*args, **kwargs)

    def snapshot_protective_stops(
        self, symbol: str, *, side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """List + snapshot open protective stop orders WITHOUT cancelling them.

        audit F1 (review #1): the write-ahead recovery row must be
        persisted BEFORE any broker mutation. Splitting the read
        (snapshot) from the write (cancel) lets the pipeline do
        snapshot → persist WAL → cancel, so a process kill anywhere
        from the cancel onward is recoverable. Previously the WAL insert
        ran AFTER cancel_protective_stops had already cancelled the
        stops at the broker — a kill in that window left a naked
        position with no recovery intent.

        `side` is the STOP order's own side: "sell" (default) finds the
        stops protecting a long; "buy" finds the stops protecting a short.
        Every existing caller cancels/restores/re-protects a long being
        SOLD, so the default is unchanged; the coverage reconciler is the
        one caller that passes `side="buy"` to check a short.

        Returns ``(ok, specs)``. ``ok`` is FALSE when the broker's own
        order listing failed — board item 172, and this used to be the
        single most dangerous lie on the read path.

        It was documented as "always True", because a listing API error was
        swallowed by `_list_open_protective_stop_orders` and surfaced as an
        empty list. An empty list means "this position has no protective
        stop", so a broker outage was reported to the desk as a CONFIRMED
        NAKED POSITION — and the coverage reconciler then repaired against
        it, placing a full-size stop on top of a live stop it could not
        see. Both the strongest possible false statement about loss
        protection and a duplicate-protection write, from one swallowed
        exception.

        Callers that ignore `ok` are no worse off than before: `specs` is
        still empty in that case. Callers that read it can tell "there is
        no stop" from "I could not ask", which is the whole distinction
        item 172 exists for.

        NOT true of every caller, and the first version of this docstring
        said it was. `TradingPipeline._cancel_stops_with_write_ahead` reads
        `ok` and skips the SELL on False, so making this return False where
        it previously always returned True changed the EXIT path as well as
        the read path — five exit call sites, none of them reviewed when
        that change was made. The test pinning that skip
        (`test_cancel_stops_with_write_ahead_skips_on_snapshot_failure`,
        added 2026-05-16) pinned unreachable code for four months, because
        until board item 172 `ok` could not be False [measured from
        `git log -S`, 2026-09-23]. Nobody chose that behaviour; it was
        inherited. What it does now is decided at that call site and
        documented there.

        THE READ IS RETRIED before it reports UNKNOWN. The listers have had
        no retry at all: one exception and the answer was "I cannot ask",
        which now costs a skipped exit. The desk's own derived retry shape
        for the stop path — `_STOP_PLACEMENT_MAX_ATTEMPTS` attempts with
        `_STOP_PLACEMENT_BACKOFF_S` backoff — is justified on the grounds
        that "every failure worth retrying is transient: a 429, a 5xx, a
        dropped connection". That argument is STRONGER for a read than for
        the write it was written for: a retried read cannot double-place
        anything. Same constants, so there is no new number here.
        """
        errors: list = []
        stops: list = []
        for attempt in range(_STOP_PLACEMENT_MAX_ATTEMPTS):
            errors = []
            stops = self._list_open_protective_stop_orders(
                symbol, side=side, errors=errors,
            )
            if not errors:
                break
            if attempt + 1 < _STOP_PLACEMENT_MAX_ATTEMPTS:
                delay = _STOP_PLACEMENT_BACKOFF_S[
                    min(attempt, len(_STOP_PLACEMENT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "snapshot_protective_stops: listing %s's protective "
                    "stops failed (%s) — retrying in %.1fs (attempt %d of "
                    "%d).",
                    symbol, "; ".join(errors), delay,
                    attempt + 2, _STOP_PLACEMENT_MAX_ATTEMPTS,
                )
                time.sleep(delay)
        if errors:
            logger.error(
                "snapshot_protective_stops: could not READ %s's protective "
                "stops after %d attempts (%s) — reporting UNKNOWN, not "
                "'no stop'.",
                symbol, _STOP_PLACEMENT_MAX_ATTEMPTS, "; ".join(errors),
            )
            return False, []
        if not stops:
            return True, []
        specs: list[dict] = []
        for order in stops:
            spec = self._snapshot_stop_order(order)
            if spec:
                specs.append(spec)
        return True, specs

    def cancel_snapshotted_stops(
        self, symbol: str, specs: list[dict],
    ) -> StopCancelOutcome:
        """Cancel pre-snapshotted protective stops by id.

        Returns a :class:`StopCancelOutcome`, never a bool. Three states the
        caller MUST distinguish: ``cleared`` (SELL may proceed), not cleared
        with ``coverage_shrank`` False (nothing moved on net, every share is
        still covered), and ``coverage_shrank`` True (a cancel failed, the
        rollback also failed, and ``outcome.unprotected`` names the shares
        that are naked at the broker right now — skip the SELL AND keep the
        recovery row for them). That third state is what the old bare
        ``False`` hid: two callers read it as "nothing moved" and deleted
        the only durable intent that could re-attach the missing stops.
        """
        if not specs:
            return StopCancelOutcome.nothing_to_do(symbol)
        cancelled: list[dict] = []
        untouched: list[dict] = []
        cancel_failed: list[dict] = []
        for spec in specs:
            sid = spec.get("id")
            if not sid:
                # Never sent to the broker: still alive, still covering.
                untouched.append(spec)
                continue
            try:
                self.client.cancel_order_by_id(sid)
                cancelled.append(spec)
                record_guarded_pass(self, "cancel_snapshotted_stops.cancel", context={"symbol": symbol, "order": sid})
            except Exception as exc:
                record_guarded_pass(self, "cancel_snapshotted_stops.cancel", exc, log=logger,
                           context={"symbol": symbol, "order": sid, "effect": "stop left resting; rollback decides coverage"})
                cancel_failed.append(spec)
        return settle_cancel(
            symbol, specs, cancelled, untouched, cancel_failed,
            self._restore_stop_orders, logger,
        )

    def cancel_protective_stops(self, symbol: str) -> tuple[bool, list[dict]]:
        """Cancel all open SELL stop orders for one symbol so a fresh exit
        order has free shares to work with.

        Returns ``(success, cancelled_specs)``:
          - ``success`` is True iff every stop was cancelled cleanly (or
            none existed). Caller should skip the SELL on False.
          - ``cancelled_specs`` is the list of stop snapshots (qty,
            stop_price, limit_price) that were successfully cancelled.
            Caller uses this to:
              1. ``_restore_stop_orders`` if the SELL is rejected by
                 the broker (rollback the cancellation so coverage is
                 preserved).
              2. ``_submit_stop_limit_order`` on the residual qty after
                 a *partial* exit (TAKE_PROFIT / REDUCE / PARTIAL_SELL)
                 — without this, the residual position rides naked
                 until the next session re-attaches an OTO stop.

        Why this exists: Alpaca rejects new SELL orders when shares are
        held_for_orders by an existing protective stop — the OTO stop-loss
        leg attached to a morning BUY, or a TRAIL_STOP placed by midday.
        Without clearing those holds first, REDUCE / SELL / EMERGENCY_SELL
        / TAKE_PROFIT all surface as 'insufficient qty available' rejects
        (2026-04-25 AMZN incident, related_orders=[<TRAIL_STOP id>]).

        On partial cancel failure (some succeed, then one raises) the
        already-cancelled stops are restored before returning False —
        same rollback discipline as ``replace_stop_loss``. The caller
        won't proceed with the SELL anyway, so leaving partial-cancelled
        state at the broker would just shrink coverage for no gain.

        Now composed from snapshot_protective_stops +
        cancel_snapshotted_stops (audit F1 review #1). The external
        contract is unchanged: no stops -> (True, []); all cancelled ->
        (True, specs); partial failure -> rolled back, (False, []).
        Direct callers/tests are unaffected; SELL paths use the
        pipeline's write-ahead orchestrator instead so the recovery row
        lands before the cancel.
        """
        ok, specs = self.snapshot_protective_stops(symbol)
        if not ok:
            return False, []
        if not specs:
            return True, []
        outcome = self.cancel_snapshotted_stops(symbol, specs)
        if not outcome.cleared:
            if outcome.coverage_shrank:
                # This composite has no channel for a partial loss, so it
                # must not quietly answer "nothing happened". Direct callers
                # get the specs they now have to re-protect.
                raise StopCoverageLost(outcome)
            return False, []
        return True, specs

    def cancel_stray_protective_stops(
        self, symbol: str, *, side: str = "sell",
    ) -> int:
        """Cancel every protective stop still resting on a symbol that is
        now FLAT. Returns the count cancelled.

        Board item 127(b), owner ruling 2026-09-25: a forced/emergency exit
        fires IMMEDIATELY and never waits on stop-work, so a concurrent
        stop-repair can re-add a protective stop inside the cancel-then-sell
        window. Once the exit takes the position to zero shares that stop is
        a stray — it protects nothing, the reprotect path never sees it (it
        was placed AFTER the pre-sell snapshot, so it is not in the sell's
        ``cancelled_specs``), and ``_reconcile_stop_coverage`` skips flat
        symbols outright — so nothing else would ever clear it, and a stop
        left resting on zero shares can later elect into an unintended
        short. This is the cheap cleanup the ruling assumes in place of the
        rejected lock-wait.

        Unlike ``cancel_snapshotted_stops`` there is NO rollback: the
        position is flat, so there is nothing to protect and a "restore"
        would only re-place the very stray order being removed. Best-effort
        and side-correct (``side="buy"`` finds the buy-stops that had
        protected a short); a cancel that raises is logged and never blocks
        the others, and the whole thing degrades to a no-op — the exit has
        already succeeded and must not be undone by a housekeeping error.
        """
        try:
            ok, specs = self.snapshot_protective_stops(symbol, side=side)
            record_guarded_pass(self, "cancel_stray_protective_stops.list", context={"symbol": symbol, "side": side})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self, "cancel_stray_protective_stops.list", exc, log=logger,
                       context={"symbol": symbol, "side": side, "effect": "a stray stop may still rest; the operator should confirm it is gone"})
            return 0
        if not ok or not specs:
            return 0
        cancelled = 0
        for spec in specs:
            sid = spec.get("id")
            if not sid:
                continue
            try:
                self.client.cancel_order_by_id(sid)
                cancelled += 1
                record_guarded_pass(self, "cancel_stray_protective_stops.cancel", context={"symbol": symbol, "order": sid, "side": side})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(self, "cancel_stray_protective_stops.cancel", exc, log=logger,
                           context={"symbol": symbol, "order": sid, "side": side, "effect": "a stop may still rest on a flat position; clear it by hand"})
        if cancelled:
            logger.info(
                "Cancelled %d stray protective %s-stop(s) on now-flat %s "
                "(item 127(b): a repair re-added protection inside the "
                "cancel-then-sell window)", cancelled, side, symbol,
            )
        return cancelled

    def cancel_open_entry_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_open_entry_orders(*args, **kwargs)

    def list_open_entry_order_ids(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_open_entry_order_ids(*args, **kwargs)

    def list_open_entry_orders_checked(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_open_entry_orders_checked(*args, **kwargs)

    def open_buy_notional(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().open_buy_notional(*args, **kwargs)

    def list_recent_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_recent_orders(*args, **kwargs)

    def list_filled_sell_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_filled_sell_orders(*args, **kwargs)

    def get_order_fill_info(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().get_order_fill_info(*args, **kwargs)

    #: `OrderStatus`/`TradeEvent` values that mean "this order will not
    #: change again" — shared between the stream and polling paths so the
    #: two mechanisms can never quietly disagree about what "terminal" means.
    _ORDER_TERMINAL_STATES = frozenset({
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "replaced",
    })

    #: Statuses in which Alpaca has the order but the EXECUTION VENUE does
    #: not yet. Source: Alpaca's own order-lifecycle reference
    #: (docs.alpaca.markets/docs/orders-at-alpaca, "Order Lifecycle"):
    #:   accepted    — "received by Alpaca, but hasn't yet been routed to the
    #:                  execution venue"
    #:   pending_new — "received by Alpaca, and routed to the exchanges, but
    #:                  has not yet been accepted"
    #: `new` is the first status that means "routed to exchanges for
    #: execution". A replace PATCH against an order still in one of these
    #: states is rejected by the broker ("unable to replace order, order
    #: isn't sent to exchange yet" / "cannot replace order in accepted
    #: status" in Alpaca's own community forum) — and the two transitional
    #: statuses `pending_cancel`/`pending_replace` are likewise listed by
    #: Alpaca's Replace-Order reference as non-replaceable. These are a
    #: distinct set from `_ORDER_TERMINAL_STATES`: not "done", just "not
    #: there yet". At the market open — the slowest acknowledgement and the
    #: time this desk trades most — an order can sit here for seconds.
    _ORDER_PRE_EXCHANGE_STATES = frozenset({"accepted", "pending_new"})

    #: The only working status a replace is documented AND observed to
    #: succeed against. `partially_filled` is deliberately excluded: it is
    #: replaceable at the broker, but this desk never replaces a partially
    #: filled order (see `_repeg_entry_order`'s partial-fill guard).
    _ORDER_REPLACEABLE_STATES = frozenset({"new"})

    def _trade_stream_waits(self) -> TradeStreamWaits:
        """Thin shim: builds the standalone object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/trade_stream.py). Built per call so a
        collaborator set or swapped after construction is what the body sees."""
        return TradeStreamWaits(
            state=self,
            get_order_status_once=self._get_order_status_once,
            wait_for_order_status_via_polling=self._wait_for_order_status_via_polling,
            # The collaborators below are themselves moved bodies, so the object
            # already owns them. Passing this broker's same-named shim would
            # overwrite its own method with a function that calls straight back
            # into it -- infinite recursion. Same guard as `_stop_placer`: pass one
            # ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("start_trade_updates", "start_trade_updates"),
                    ("_wait_for_order_status_via_stream", "_wait_for_order_status_via_stream"),
                    ("_wait_for_order_status_via_stream_locked", "_wait_for_order_status_via_stream_locked"),
                    ("fill_stream_enabled", "fill_stream_enabled"),
                    ("_release_trade_updates_slot", "_release_trade_updates_slot"),
                    ("_acquire_trade_updates_slot", "_acquire_trade_updates_slot"),
                    ("_hub_warmup", "_hub_warmup"),
                    ("_fill_stream_off_warmup", "_fill_stream_off_warmup"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def _acquire_trade_updates_slot(self, *args, **kwargs):
        return self._trade_stream_waits()._acquire_trade_updates_slot(*args, **kwargs)

    def _release_trade_updates_slot(self, *args, **kwargs):
        return self._trade_stream_waits()._release_trade_updates_slot(*args, **kwargs)

    def fill_stream_enabled(self, *args, **kwargs):
        return self._trade_stream_waits().fill_stream_enabled(*args, **kwargs)

    def _fill_stream_off_warmup(self, *args, **kwargs):
        return self._trade_stream_waits()._fill_stream_off_warmup(*args, **kwargs)

    def ensure_trade_updates(self, *args, **kwargs):
        return self._trade_stream_waits().ensure_trade_updates(*args, **kwargs)

    def trade_updates_lease_contended(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_lease_contended(*args, **kwargs)

    def trade_updates_started(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_started(*args, **kwargs)

    def trade_updates_authed(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_authed(*args, **kwargs)

    def trade_updates_auth_remaining_s(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_auth_remaining_s(*args, **kwargs)

    def _hub_warmup(self, *args, **kwargs):
        return self._trade_stream_waits()._hub_warmup(*args, **kwargs)

    def start_trade_updates(self, *args, **kwargs):
        return self._trade_stream_waits().start_trade_updates(*args, **kwargs)

    def stop_trade_updates(self, *args, **kwargs):
        return self._trade_stream_waits().stop_trade_updates(*args, **kwargs)

    def _wait_for_order_status(self, *args, **kwargs):
        return self._trade_stream_waits()._wait_for_order_status(*args, **kwargs)

    def _wait_for_order_status_via_stream(self, *args, **kwargs):
        return self._trade_stream_waits()._wait_for_order_status_via_stream(*args, **kwargs)

    def _wait_for_order_status_via_stream_locked(self, *args, **kwargs):
        return self._trade_stream_waits()._wait_for_order_status_via_stream_locked(*args, **kwargs)


    def wait_for_order_at_exchange(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().wait_for_order_at_exchange(*args, **kwargs)

    def wait_for_order_terminal(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().wait_for_order_terminal(*args, **kwargs)

    def _get_order_status_once(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._get_order_status_once(*args, **kwargs)

    def _wait_for_order_terminal_via_stream(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_terminal_via_stream(*args, **kwargs)


    def _wait_for_order_terminal_via_polling(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_terminal_via_polling(*args, **kwargs)

    def _wait_for_order_status_via_polling(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_status_via_polling(*args, **kwargs)

    def submit_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().submit_order(*args, **kwargs)

    # SCOPE (owner ratified 2026-09-25): primary PROTECTIVE stops are now
    # stop-MARKET (guaranteed exit), so this buffer NO LONGER governs the
    # protective stop the desk normally places. It governs only (a) the
    # stop-LIMIT FALLBACK taken when the broker refuses a stop-market for an
    # unsupported type/tif combo, and (b) the force-de-lever must-fill SELL.
    #
    # 3% beyond the stop: a stop-MARKET fills at whatever the book has on a
    # gap (10%+ worse than the stop); a stop-limit caps the worst-case fill.
    # The buffer must be wide enough that routine volatility clears it
    # ("prioritize fill over price"). Trade-off on those fallback/de-lever
    # legs: on gaps beyond 3% the limit won't fill and the position stays
    # open until a session can act — which is exactly why the primary
    # protective stop is now market and not subject to this trade-off.
    #
    # "Beyond", not "below": a long's protective order is a SELL stop, so
    # its limit sits 3% BELOW the trigger (a SELL needs its floor under the
    # stop to have room to fill on the way down). A short's protective order
    # is a BUY stop, so its limit must sit 3% ABOVE the trigger — a BUY
    # needs headroom over the stop to fill on the way up. Getting this
    # backwards for a short is silent: the order still submits, but the
    # limit sits on the wrong side of the trigger, so it can never fill.
    # The stop then "fires" and does nothing, and the position runs
    # unprotected in the one direction that matters.
    STOP_LIMIT_BUFFER_PCT = 0.03

    # Order states that mean "this order can never fill another share".
    _TERMINAL_ORDER_STATES = frozenset({
        "filled", "canceled", "cancelled", "expired", "rejected",
        "done_for_day", "stopped", "suspended",
    })

    # Bounded number of `replaced_by` hops to follow when resolving what a
    # replaced order became. Each re-peg adds exactly one hop and re-pegs are
    # capped in the low single digits, so 8 is generous; the bound exists so a
    # broker-side cycle or a pathological chain can never spin this forever.
    _MAX_REPLACEMENT_HOPS = 8

    def cancel_entry_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_entry_order(*args, **kwargs)

    def resolve_replacement_chain(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().resolve_replacement_chain(*args, **kwargs)

    def replace_entry_limit(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().replace_entry_limit(*args, **kwargs)

    def await_replacement_confirmed(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().await_replacement_confirmed(*args, **kwargs)

    def place_entry_protection(
        self, symbol: str, order_id: str, stop_price: float,
        *, requested_qty: float | None = None, side: str = "buy",
        superseded_filled_qty: float = 0.0,
        on_unfilled_cancel=None,
        cover_full_position: bool = False,
        held_qty_before: float = 0.0,
    ) -> dict | None:
        """Wait for an entry order to reach terminal, then place a GTC
        protective stop (stop-MARKET, guaranteed exit) for the ACTUAL filled
        qty.

        If the entry is STILL WORKING after the wait (slow tape, wide limit),
        the unfilled remainder is CANCELLED first — audit round 2: the 15s
        wait treated "still live" identically to "terminal 0-fill" and walked
        away, so a DAY entry limit could fill hours later with no stop
        watching it (and a resting BUY could even re-buy into a crash after an
        emergency liquidation). Cancelling converges the order; whatever DID
        fill by then gets its stop from the post-cancel re-read. Losing the
        unfilled remainder is the accepted cost of protection-first.

        THIS CANCEL IS THE END-OF-CYCLE CANCEL (owner-approved 2026-09-12),
        and its timing is derived, not chosen. This desk does not run
        continuously: it runs as separate scheduled SESSIONS — see
        `SESSION_WINDOWS` in `src/trading_calendar.py` — each a single
        process that analyses at the prices and levels of that moment,
        proposes entries, submits them, protects the fills, and EXITS. New
        entries come only from the morning session; the midday and close
        sessions review positions. (The systemd/launchd timer ticks every 30
        minutes, but that tick only asks `scripts/run_if_et_window.sh`
        whether a session is due; it is not a re-scan.) So "the decision
        cycle that created the order" is this very process, and its boundary
        is the point where this process stops waiting for the fill and moves
        on — which is exactly here. An entry that outlived its own session
        would be resting on a thesis nobody is still holding: the next
        session re-analyses from scratch at real current prices and will
        re-propose the trade if it still wants it. Cancelling here — rather
        than leaving a DAY order resting until 16:00 ET — is therefore
        binding the order's life to the desk's own heartbeat, not to a
        timeout somebody picked. There is deliberately no separate "cancel
        after N minutes" constant: the boundary IS the end of this stage, and
        stays correct if the session schedule ever changes. The only number
        in play is `_ENTRY_FILL_TIMEOUT_S`, the in-cycle patience for a fill,
        which predates this and is unchanged.

        `on_unfilled_cancel`, when given, is called with a small dict
        (`order_id`, `status`, `filled_qty`) after a still-working entry was
        cancelled here and the post-cancel re-read shows NOTHING filled under
        any id in its chain. The caller uses it to page the owner with the
        prices that were tried — this method does not know them. Not invoked
        for a partial fill (shares were acquired and the stop covers them)
        or for an order that reached terminal on its own. Never allowed to
        raise into this method.

        `side` is the ENTRY order's own side — "buy" opens or adds to a long
        (the only side any order path in this repo has ever submitted, hence
        the default), "sell"/"sell_short" opens a short. The protective stop
        is always the OPPOSITE side, at the opposite buffer: a SELL stop
        below a long, a BUY stop above a short. See `STOP_LIMIT_BUFFER_PCT`.

        `superseded_filled_qty` is shares this entry already acquired under a
        DIFFERENT order id — the ancestors of a re-peg chain. `order_id` is
        the last order in that chain, and Alpaca's fill counters do not carry
        across a replacement, so the shares an ancestor filled are invisible
        here. They are real shares in a real position, and a stop sized to
        only the last order's fill would leave them naked. Adding them is what
        keeps the invariant "every filled share is under a stop" true across a
        re-peg. Default 0.0: for every caller that never re-pegs, this method
        behaves exactly as it did before.

        `cover_full_position` (long scale-in path B, 2026-09-15): after a
        positive fill, size the protective sell to the broker's FULL
        position quantity, not this order's fill. A partial add on a name
        that already held shares would otherwise rearm a stop over the
        add alone and leave the original lot naked. `held_qty_before` is
        the fallback if the broker position cannot be read: fill + what
        was held, the two quantities already measured, not a third number.

        Returns the stop order dict, or None when nothing was placed (entry
        filled 0 / stop submit failed). Never raises — a failure here must not
        abort the session.

        Spec §11.1 guard 1: the stop submission now RETRIES immediately and
        hard before giving up (`_submit_protective_stop_retrying`). A None
        return therefore means the retries were exhausted, and the position is
        naked — the CALLER owes an owner alert on it (guard 2); the
        coverage-reconcile auto-repair belt remains the backstop, not the
        first line.
        """
        # Fail closed on a side we do not recognise, BEFORE touching the
        # broker. `"sell" if side == "buy" else "buy"` reads harmlessly but is
        # fail-OPEN: a typo, a None, or some future side string falls into the
        # short branch, and a LONG then gets a BUY stop placed ABOVE it — not
        # weak protection, but a standing order to buy more of a position that
        # is already losing.
        #
        # This returns rather than raising, because the contract above is that
        # this function never aborts a session. Returning None is the same
        # outcome as any other protection failure: logged at ERROR, position
        # left naked-but-KNOWN, and picked up by the coverage-reconcile
        # auto-repair belt. Naked-and-believed-covered is the state that
        # actually costs money, and refusing here is what prevents it.
        normalized = (side or "").strip().lower()
        if normalized not in _ENTRY_SIDES:
            logger.error(
                "entry protection: %s refusing to guess a protective side for "
                "entry side %r (expected one of %s) — NO stop placed, position "
                "will be left uncovered and must be repaired by reconcile",
                symbol, side, sorted(_ENTRY_SIDES),
            )
            return None

        try:
            status = self.wait_for_order_terminal(
                order_id, timeout_seconds=_ENTRY_FILL_TIMEOUT_S,
            )
            record_guarded_pass(self, "entry_protection.wait_terminal", context={"symbol": symbol, "order": order_id})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self, "entry_protection.wait_terminal", exc, log=logger,
                       context={"symbol": symbol, "order": order_id, "effect": "status unknown"})
            status = None

        cancelled_here = False
        if (status or "").lower() not in self._TERMINAL_ORDER_STATES:
            # Still working at the end of its cycle — cancel the remainder so
            # it can't fill unwatched and so it stops resting on a thesis
            # this session is about to walk away from (see the docstring).
            # A fill can land during cancel propagation; the post-cancel
            # re-read below protects whatever landed.
            logger.warning(
                "entry protection: %s entry %s still working at the end of "
                "its session (status=%s) — cancelling the unfilled remainder "
                "so no share can fill without a stop watching it and no "
                "order outlives the analysis that created it",
                symbol, order_id, status or "unknown",
            )
            try:
                self.client.cancel_order_by_id(order_id)
                cancelled_here = True
                record_guarded_pass(self, "entry_protection.cancel_working_entry", context={"symbol": symbol, "order": order_id})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(self, "entry_protection.cancel_working_entry", exc, log=logger,
                           context={"symbol": symbol, "order": order_id, "effect": "a later fill will be UNPROTECTED until the next coverage reconcile"})
            try:
                status = self.wait_for_order_terminal(
                    order_id, timeout_seconds=10.0,
                ) or status
                record_guarded_pass(self, "entry_protection.wait_after_cancel", context={"symbol": symbol, "order": order_id})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(self, "entry_protection.wait_after_cancel", exc, log=logger,
                           context={"symbol": symbol, "order": order_id, "effect": "falls through to the unconfirmed-outcome branch below"})
            if (status or "").lower() not in self._TERMINAL_ORDER_STATES:
                # Fill confirmation has genuinely DEGRADED: the bounded
                # window closed, the cancel-and-recheck closed too, and the
                # broker still has not said what happened to a live order.
                # The desk proceeds on filled_qty=0 below — the safe
                # assumption, possibly a wrong one — so the owner has to be
                # told, not just the log. This is NOT "the websocket is
                # off": it is reachable identically with the socket on, and
                # is exactly the outcome the REST path is supposed to
                # prevent. See src/notifier.py's fill-confirmation block.
                try:
                    from src.notifier import alert_order_outcome_unconfirmed
                    alert_order_outcome_unconfirmed(
                        symbol, order_id,
                        waited_seconds=_ENTRY_FILL_TIMEOUT_S,
                        last_status=(status or "").lower() or None,
                    )
                    record_guarded_pass(self, "entry_protection.unconfirmed_alert", context={"symbol": symbol, "order": order_id})
                except Exception as exc:  # noqa: BLE001
                    record_guarded_pass(self, "entry_protection.unconfirmed_alert", exc, log=logger,
                               context={"symbol": symbol, "order": order_id, "effect": "the owner was NOT told the outcome is unconfirmed"})

        try:
            info = self.get_order_fill_info(order_id) or {}
            record_guarded_pass(self, "entry_protection.fill_info", context={"symbol": symbol, "order": order_id})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self, "entry_protection.fill_info", exc, log=logger,
                       context={"symbol": symbol, "order": order_id, "effect": "treated as filled_qty=0"})
            info = {}
        try:
            filled_qty = float(info.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            carried = float(superseded_filled_qty or 0)
        except (TypeError, ValueError):
            carried = 0.0
        if carried > 0:
            logger.info(
                "entry protection: %s carries %.4f share(s) filled under a "
                "superseded order id; stop will cover %.4f + %.4f",
                symbol, carried, filled_qty, carried,
            )
            filled_qty += carried
        if filled_qty > 0 and cover_full_position:
            full_qty = cover_qty_for_rearm(
                self, symbol=symbol, filled_qty=filled_qty,
                held_qty_before=held_qty_before,
            )
            if full_qty > filled_qty + 1e-9:
                logger.info(
                    "entry protection: %s scale-in fill %.4f — stop sized to "
                    "broker full position %.4f, not the add alone",
                    symbol, filled_qty, full_qty,
                )
            if full_qty > 0:
                filled_qty = full_qty

        if filled_qty <= 0:
            logger.warning(
                "entry protection: %s entry %s filled 0 (status=%s) — no stop "
                "placed (nothing to protect)", symbol, order_id, status or "unknown",
            )
            if cancelled_here and on_unfilled_cancel is not None:
                try:
                    on_unfilled_cancel({
                        "order_id": order_id,
                        "status": (status or "").lower() or "unknown",
                        "filled_qty": 0.0,
                    })
                    record_guarded_pass(self, "entry_protection.unfilled_cancel_callback", context={"symbol": symbol, "order": order_id})
                except Exception as exc:  # noqa: BLE001
                    record_guarded_pass(self, "entry_protection.unfilled_cancel_callback", exc, log=logger,
                               context={"symbol": symbol, "order": order_id, "effect": "the caller was not told the entry went unfilled"})
            return None
        if (
            requested_qty and filled_qty < requested_qty
            and not cover_full_position
        ):
            logger.warning(
                "entry protection: %s partially filled %.4f/%.4f — stop sized to "
                "the ACTUAL fill", symbol, filled_qty, requested_qty,
            )
        # The protective order's side is the OPPOSITE of the entry's: a BUY
        # entry (long) is protected by a SELL stop below it; a SELL/SELL_SHORT
        # entry (short) is protected by a BUY stop above it. The buffer
        # mirrors the same way — see STOP_LIMIT_BUFFER_PCT above. Getting
        # this backwards is THE most dangerous bug in shorts-safe: the order
        # still submits without error, it just sits on the wrong side of the
        # trigger and can never fill, so the position runs unprotected in
        # exactly the direction it needed protecting.
        protective_side = "sell" if normalized == "buy" else "buy"
        buffer_mult = (
            (1 - self.STOP_LIMIT_BUFFER_PCT) if protective_side == "sell"
            else (1 + self.STOP_LIMIT_BUFFER_PCT)
        )
        stop_order = self._submit_protective_stop_retrying(
            symbol=symbol, qty=filled_qty, stop_price=stop_price,
            limit_price=stop_price * buffer_mult, side=protective_side,
        )
        if stop_order is None:
            logger.error(
                "entry protection FAILED for %s (%.4f shares held, stop $%.2f) "
                "after %d attempt(s) — position is UNPROTECTED; the caller must "
                "raise an OWNER alert (spec §11.1 guard 2) and the coverage "
                "reconcile must repair it",
                symbol, filled_qty, stop_price, _STOP_PLACEMENT_MAX_ATTEMPTS,
            )
            return None
        return stop_order

    def _stop_placer(self) -> StopPlacer:
        """Thin shim: builds the standalone placer from this broker's collaborators
        (bodies moved to src/execution/broker_parts/stop_place.py). Built per call so a
        client or cluster method swapped after construction is what the body sees."""
        return StopPlacer(
            client=self.client,
            list_open_stop_orders_by_side=self._list_open_stop_orders_by_side,
            list_open_protective_stop_orders=self._list_open_protective_stop_orders,
            list_open_sell_stop_orders=self._list_open_sell_stop_orders,
            snapshot_stop_order=self._snapshot_stop_order,
            amend_one_stop_price=self._amend_one_stop_price,
            amend_resting_stop_price=self._amend_resting_stop_price,
            stop_order_amendable_in_place=self._stop_order_amendable_in_place,
            cancel_snapshotted_stops=self.cancel_snapshotted_stops,
            get_positions=self.get_positions,
            kill_switch_active=self._kill_switch_active,
            kill_switch_path=self._kill_switch_path, wait_for_order_terminal=self.wait_for_order_terminal,
            protective_stop_block_recorder=self.protective_stop_block_recorder,
            stop_limit_buffer_pct=self.STOP_LIMIT_BUFFER_PCT, window_log=self.__dict__.setdefault("_unprotected_windows", []),
            # Six collaborators below are themselves moved bodies, so the
            # placer already owns them. Passing this broker's same-named shim
            # would overwrite the placer's own method with a function that
            # calls straight back into the placer -- infinite recursion. Pass
            # one ONLY when it is NOT that shim: a replacement bound on this
            # instance, or a stand-in on a test host that is not a broker at
            # all. Those are exactly the cases the placer cannot see itself.
            # The shim is recognised through a bound method (`__func__`) AND
            # through a `functools.partial` (`func`): tests bind the class
            # function onto a non-broker host with partial, and that is still
            # the shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("submit_protective_stop_retrying", "_submit_protective_stop_retrying"),
                    ("submit_stop_leg_retrying", "_submit_stop_leg_retrying"),
                    ("existing_stop_covering_qty", "_existing_stop_covering_qty"),
                    ("submit_stop_limit_order", "_submit_stop_limit_order"),
                    ("submit_stop_legs", "_submit_stop_legs"),
                    ("restore_stop_orders", "_restore_stop_orders"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def _existing_stop_covering_qty(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._existing_stop_covering_qty(*args, **kwargs)

    def _submit_stop_leg_retrying(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_leg_retrying(*args, **kwargs)

    def _submit_protective_stop_retrying(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_protective_stop_retrying(*args, **kwargs)

    def close_position(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().close_position(*args, **kwargs)

    def _list_open_stop_orders_by_side(
        self, symbol: str, *, errors: list | None = None,
    ) -> tuple[list, list]:
        """Single order-book fetch for `symbol`, split into (sell_stops, buy_stops).

        A long's protective stop is a SELL stop; a short's is a BUY stop.
        `replace_stop_loss` needs to know WHICH side a symbol's live stop is
        on before it can decide what to list/cancel/ratchet-check — but it
        can't yet know the position's direction without a second API call.
        Fetching once and filtering both ways here answers "which side has a
        stop" from a single snapshot, rather than two separate fetches that
        could each see a different broker state.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
            record_guarded_pass(self, "replace_stop_loss.list_open_orders", context={"symbol": symbol})
        except Exception as exc:
            record_guarded_pass(self, "replace_stop_loss.list_open_orders", exc, log=logger,
                       context={"symbol": symbol, "effect": "no stop orders returned to the caller"})
            # Board item 172 — same contract as the sell-side lister above.
            if errors is not None:
                errors.append(f"open-order listing failed: {exc}")
            return [], []

        sell_orders: list = []
        buy_orders: list = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            if "stop" not in order_type:
                continue
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if order_side == "sell":
                sell_orders.append(order)
            elif order_side == "buy":
                buy_orders.append(order)
        return sell_orders, buy_orders

    def _list_open_protective_stop_orders(
        self, symbol: str, *, side: str = "sell", errors: list | None = None,
    ) -> list:
        """List open stop orders on `side` for `symbol`.

        `side="sell"` (default) finds the stops protecting a long — the only
        case that existed before shorts were countable, and delegates to
        `_list_open_sell_stop_orders` (the name a long list of tests and
        call sites pin) rather than duplicating it. `side="buy"` finds the
        stops protecting a short: a short's protective order is a BUY stop,
        so a filter hardcoded to "sell" made a short's live stop invisible
        to every caller (coverage reconcile would report a perfectly
        protected short as NAKED and try to "repair" over it).
        """
        if side.lower() == "buy":
            _, buy_orders = self._list_open_stop_orders_by_side(
                symbol, errors=errors,
            )
            return buy_orders
        return self._list_open_sell_stop_orders(symbol, errors=errors)

    def _list_open_sell_stop_orders(self, symbol: str, *, errors: list | None = None) -> list:
        """Board item 172: `errors`, when given, receives the listing
        failure instead of it being swallowed into an empty list.

        The empty-list return is UNCHANGED for every caller that does not
        pass `errors`, because `replace_stop_loss` and its tests depend on
        it. What changes is that a caller who needs to tell "no stops" from
        "could not ask" can now do so — and `snapshot_protective_stops` is
        exactly that caller.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
            record_guarded_pass(self, "replace_stop_loss.list_open_orders_buyside", context={"symbol": symbol})
        except Exception as exc:
            record_guarded_pass(self, "replace_stop_loss.list_open_orders_buyside", exc, log=logger,
                       context={"symbol": symbol, "effect": "no stop orders returned to the caller"})
            if errors is not None:
                errors.append(f"open-order listing failed: {exc}")
            return []

        stop_orders = []
        for order in orders or []:
            order_type = str(getattr(getattr(order, "order_type", None), "value",
                                    getattr(order, "order_type", ""))).lower()
            order_side = str(getattr(getattr(order, "side", None), "value",
                                    getattr(order, "side", ""))).lower()
            if "stop" in order_type and order_side == "sell":
                stop_orders.append(order)
        return stop_orders

    @staticmethod
    def _snapshot_stop_order(order) -> dict | None:
        try:
            qty = float(getattr(order, "qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            stop_price = float(getattr(order, "stop_price", 0) or 0)
        except (TypeError, ValueError):
            stop_price = 0.0
        try:
            limit_price = float(getattr(order, "limit_price", 0) or 0)
        except (TypeError, ValueError):
            limit_price = 0.0
        if qty <= 0 or stop_price <= 0:
            return None
        return {
            "id": str(order.id),
            "qty": qty,
            "stop_price": stop_price,
            "limit_price": limit_price or None,
        }

    def _submit_stop_limit_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_limit_order(*args, **kwargs)

    def _submit_stop_legs(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_legs(*args, **kwargs)

    def _restore_stop_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._restore_stop_orders(*args, **kwargs)

    def shift_stops_down(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer().shift_stops_down(*args, **kwargs)

    _AMEND_DEAD_STATES = StopAmender._AMEND_DEAD_STATES

    def _stop_amender(self) -> StopAmender:
        """Thin shim: builds the standalone amender from this broker's collaborators
        (bodies moved to src/execution/broker_parts/stop_amend.py)."""
        return StopAmender(
            client=self.client,
            list_open_stop_orders_by_side=self._list_open_stop_orders_by_side,
            snapshot_stop_order=self._snapshot_stop_order,
        )

    _failed_amend_payload = staticmethod(StopAmender._failed_amend_payload)

    def _classify_after_dead_replacement(self, *, symbol: str, spec: dict, new_price: float, leg: dict) -> str:
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._classify_after_dead_replacement(symbol=symbol, spec=spec, new_price=new_price, leg=leg)

    def _amend_one_stop_price(self, **kw) -> dict:
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._amend_one_stop_price(**kw)

    _stop_order_amendable_in_place = staticmethod(StopAmender._stop_order_amendable_in_place)

    def _amend_resting_stop_price(self, *, symbol: str, live_orders: list, stop_specs: list[dict], new_stop_price: float, position_qty: float):
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._amend_resting_stop_price(symbol=symbol, live_orders=live_orders, stop_specs=stop_specs, new_stop_price=new_stop_price, position_qty=position_qty)

    def replace_stop_loss(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer().replace_stop_loss(*args, **kwargs)


# ---------------------------------------------------------------------------
# Patch-target mirror (the ONE module-level __getattr__ and the ONE write-through
# __setattr__ of this file). Tests patch `src.execution.broker.<name>` for names
# whose bodies now live in broker_parts.trade_stream / broker_parts.market_data;
# those bodies read their OWN module globals, so a write here is mirrored into
# every part that defines the name. The two flags the stream code rebinds with
# `global` are not imported above (an imported copy would go stale); reads of
# them fall through to the part and writes go only there.
import sys as _sys
import types as _types
from src.execution.broker_parts import market_data as _market_data_part
from src.execution.broker_parts import trade_stream as _trade_stream_part

# Sector cluster: OWNED by `src.sector_reference` (L0). Its names stay reachable
# here so tests that patch `src.execution.broker.<name>` keep working; a write
# is mirrored into the owning module (via _MIRRORED_PARTS) and a read is always
# live from it. The copy kept in this module's dict exists so `unittest.mock.patch`
# sees the name as local and restores it with a plain setattr, which writes through.
_SECTOR_MIRROR_NAMES = frozenset({
    "_get_sector", "_sector_resolution_status_for", "_canonicalize_sector",
    "_sector_cache", "_sector_lock", "_sector_resolution_status",
    "_INDEX_ETFS", "_ETF_SECTORS", "_SECTOR_LOOKUP_TIMEOUT_S",
    "_ALLOWED_SECTORS", "_SECTOR_ALIASES",
})
_MIRRORED_PARTS = (_trade_stream_part, _market_data_part, _sector_reference)
_FORWARDED_GLOBALS = {
    "_stream_auth_deprecation_logged": _trade_stream_part,
    "_stream_current_auth_format_logged": _trade_stream_part,
}


def __getattr__(name):
    part = _FORWARDED_GLOBALS.get(name)
    if part is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(part, name)


class _MirrorModule(_types.ModuleType):
    def __setattr__(self, name, value):
        for part in _MIRRORED_PARTS:
            if name in vars(part):
                setattr(part, name, value)
        if name in _FORWARDED_GLOBALS:
            return
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _FORWARDED_GLOBALS:
            return
        super().__delattr__(name)

    def __getattribute__(self, name):
        if name in _SECTOR_MIRROR_NAMES:
            return getattr(_sector_reference, name)
        return _types.ModuleType.__getattribute__(self, name)


_sys.modules[__name__].__class__ = _MirrorModule
for _n in _SECTOR_MIRROR_NAMES:
    _types.ModuleType.__setattr__(_sys.modules[__name__], _n, getattr(_sector_reference, _n))
del _n
