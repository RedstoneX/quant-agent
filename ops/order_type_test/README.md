# Go-live order-type fill test

Measures, per order type, how orders actually fill: market, limit at mid,
limit at bid, and marketable limit (0.5% under last), for whole and
fractional lots, across five liquid names.
- Records fill time and fill price in bps versus the quote midpoint.
- Paper fills are simulated against NBBO, so results mean something
  only on a LIVE account. Run it once when real money starts.
- It buys, sells, then liquidates everything; use a small funded account.
- Prints a CSV of every order and a per-method summary.

Run:

    ALPACA_API_KEY=... ALPACA_SECRET_KEY=... SANDBOX_ACCOUNT_SUFFIX=<last chars of account number> \
      python ops/order_type_test/order_type_test.py

Refuses to run if any variable is unset or the account number does not end with
SANDBOX_ACCOUNT_SUFFIX. Required: ALPACA_TRADING_URL, with no default, so the caller always names paper or live on purpose.
