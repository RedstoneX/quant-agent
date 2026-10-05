# Broker conformance: rehearsal stand-in vs the real Alpaca API

Produced by `ops/rehearsal/conformance.py --live` on 2026-10-02 (after the
close, 20:10 UTC) against the disposable sandbox paper account, reached at
that time through the OneCLI `rehearsal` grant. That is historical evidence,
not the current credential path. Live reruns now use the same systemd
credential-file boundary as production and connect directly to Alpaca. Re-run:

    scripts/run_rehearsal_conformance.sh

Account assertion output (id redacted by the script itself):

    account assertion PASSED: account_number == <redacted>, status=AccountStatus.ACTIVE, equity=9999.61

Account left behind: 0 open orders, 0 positions (verified by a second read
after the run; one cancel was still `pending_cancel` at the instant the
script's own final read ran, see finding 6).

## Disagreements, most dangerous first

1. **`replace_order_by_id` is missing from the stand-in** (AttributeError).
   This is the amend path for every protective-stop move. Offline, every
   stop shift has raised inside the amend `try`, been classified "unknown",
   and the rehearsal reported the session as if the stop had moved.
2. **The real broker REFUSES an amend while the order is `accepted`**
   (HTTP 422 `42210000 cannot replace order in accepted status`). After the
   close every new order, stops included, sits in `accepted`, not `new`.
   The desk's amend-first stop shift classifies 422 as "refused" and does not
   fall back to cancel-and-resubmit, so a ratchet attempted outside the
   session (or on any stop the broker has not yet moved to `new`) leaves
   the stop exactly where it was. The stand-in can never show this because
   it reports stops as `new` on submission.
3. **`client.get(...)` (raw REST) is missing from the stand-in**: the desk's
   asset lookup, activity ledger and margin-interest reads all raise
   AttributeError offline.
4. **Order status on submission: real `accepted`, stand-in `filled`** for a
   limit entry (stand-in fills immediately; the real one rests). Any decision
   that reads the submit answer sees a filled trade offline that would be
   resting live.
5. **`get_order_by_id` lacks `order_type`, `stop_price`, `limit_price`**
   offline (real answer carries 35 fields); the stop-read path keys on these.
6. **Cancel is asynchronous live, instantaneous offline**: a just-cancelled
   order still appears in `get_orders(OPEN)` as `pending_cancel` for a
   moment; the stand-in flips it to `canceled` in the same call. Offline
   cancel-then-resubmit sequences never meet this window. Related: the
   stand-in `get_orders` ignores the `status` filter entirely (it filters on
   symbols and side only).
7. **Error shapes differ**: real `APIError(status_code=404/422)` vs stand-in
   `BrokerReachAttempted`/`AttributeError`. Code that inspects
   `exc.status_code` (the terminal-rejection classifier does) sees `None`
   offline, so every offline refusal is classified "unknown", never "refused".
8. **`get_asset` answers live, raises offline** (documented fail-closed
   choice; recorded so the divergence is explicit, not a hole).
9. **`get_account` lacks `account_number` offline** (cosmetic; only this
   script reads it).
10. **The `rehearsal` OneCLI grant does not cover `data.alpaca.markets`**
    (HTTP 401 `access_restricted` on every market-data call). The data-client
    half of the conformance check is UNMEASURABLE through this grant until
    the grant is extended; the four data calls below are reported as such.

Untested this run: `close_position` and a SELL-side protective stop (the
sandbox holds no position and nothing fills after the close). The amend
answer shape was exercised on a BUY stop instead — same request type, same
endpoint. Re-run during market hours with a held position to close both.

## Table: real answer vs stand-in answer vs verdict

| call | real (sandbox) | stand-in | verdict | detail |
|---|---|---|---|---|
| get_account | TradeAccount status=ACTIVE fields=34 | SimpleNamespace fields=4 | **FIELDS** | stand-in lacks ['account_number'] |
| get_all_positions | list fields=0 | list fields=0 | **MATCH** |  |
| get_portfolio_history | PortfolioHistory fields=7 | SimpleNamespace fields=3 | **MATCH** |  |
| get_calendar | list fields=3 | list fields=3 | **MATCH** |  |
| get_asset | Asset status=active fields=16 | RAISES BrokerReachAttempted: asset directory is unavailable offline; eligibility fails closed | **DIVERGES** | real answers, stand-in raises BrokerReachAttempted |
| get(/assets/{sym}) | dict status=active fields=16 | RAISES AttributeError: 'RehearsalTradingClient' object has no attribute 'get' | **HOLE** | real answers, stand-in raises AttributeError |
| get(/account/activities) | list fields=12 | RAISES AttributeError: 'RehearsalTradingClient' object has no attribute 'get' | **HOLE** | real answers, stand-in raises AttributeError |
| get(/account/activities/INT) | list fields=0 | RAISES AttributeError: 'RehearsalTradingClient' object has no attribute 'get' | **HOLE** | real answers, stand-in raises AttributeError |
| submit_order | Order status=accepted fields=35 | SimpleNamespace status=filled fields=6 | **STATUS** | status real=accepted stub=filled |
| get_order_by_id | Order status=accepted fields=35 | SimpleNamespace status=filled fields=7 | **FIELDS+STATUS** | stand-in lacks ['order_type', 'stop_price', 'limit_price'] status real=accepted stub=filled |
| get_orders | list fields=35 | list fields=11 | **MATCH** |  |
| replace_order_by_id | RAISES APIError http=422: {"code":42210000,"message":"cannot replace order in accepted status"} | RAISES AttributeError: 'RehearsalTradingClient' object has no attribute 'replace_order_by_id' | **ERROR-SHAPE** | real APIError vs stand-in AttributeError |
| get_order_by_id (after replace) | Order status=accepted fields=35 | SimpleNamespace status=filled fields=7 | **FIELDS+STATUS** | stand-in lacks ['order_type', 'stop_price', 'limit_price'] status real=accepted stub=filled |
| cancel_order_by_id | NoneType fields=0 | NoneType fields=0 | **MATCH** |  |
| get_order_by_id (after cancel) | Order status=canceled fields=35 | SimpleNamespace status=canceled fields=7 | **FIELDS** | stand-in lacks ['order_type', 'stop_price', 'limit_price'] |
| replace_order_by_id (on cancelled) | RAISES APIError http=422: {"code":42210000,"message":"order is not open"} | RAISES AttributeError: 'RehearsalTradingClient' object has no attribute 'replace_order_by_id' | **ERROR-SHAPE** | real APIError vs stand-in AttributeError |
| cancel_order_by_id (already cancelled) | NoneType fields=0 | NoneType fields=0 | **MATCH** |  |
| get_order_by_id (unknown id) | RAISES APIError http=404: {"code":40410000,"message":"order not found for <uuid> | RAISES BrokerReachAttempted: rehearsal has no record of order <uuid> | **ERROR-SHAPE** | real APIError vs stand-in BrokerReachAttempted |
| submit_order (protective stop) | Order status=accepted fields=35 | SimpleNamespace status=new fields=6 | **STATUS** | status real=accepted stub=new |
| replace_order_by_id (stop_price) | RAISES APIError http=422: {"code":42210000,"message":"cannot replace order in accepted status"} | RAISES AttributeError: 'RehearsalTradingClient' object has no attribute 'replace_order_by_id' | **ERROR-SHAPE** | real APIError vs stand-in AttributeError |
| get_orders (stops after amend) | list fields=35 | list fields=11 | **MATCH** |  |
| get_order_by_id (old stop id after amend) | Order status=accepted fields=35 | SimpleNamespace status=new fields=7 | **FIELDS+STATUS** | stand-in lacks ['order_type', 'stop_price', 'limit_price'] status real=accepted stub=new |
| cancel_order_by_id (stop) | NoneType fields=0 | NoneType fields=0 | **MATCH** |  |
| close_position | RAISES UNTESTED: no held position | RAISES UNTESTED: no held position | **MATCH** |  |
| cancel_orders | list fields=0 | list fields=0 | **MATCH** |  |
| get_orders (after cancel_orders) | list fields=35 | list fields=11 | **MATCH** |  |
| get_stock_latest_trade | RAISES APIError http=401: {"error":"access_restricted","manage_url":"https://ovh-vps.wallaby-bow | dict fields=1 | **DIVERGES** | real raises, stand-in answers |
| get_stock_latest_quote | RAISES APIError http=401: {"error":"access_restricted","manage_url":"https://ovh-vps.wallaby-bow | dict fields=2 | **DIVERGES** | real raises, stand-in answers |
| get_stock_snapshot | RAISES APIError http=401: {"error":"access_restricted","manage_url":"https://ovh-vps.wallaby-bow | dict fields=3 | **DIVERGES** | real raises, stand-in answers |
| get_stock_bars | RAISES APIError http=401: {"error":"access_restricted","manage_url":"https://ovh-vps.wallaby-bow | SimpleNamespace fields=1 | **DIVERGES** | real raises, stand-in answers |
