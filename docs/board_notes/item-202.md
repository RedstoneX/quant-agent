## Item 202 — the rehearsal harness reaches the network

Found 2026-09-30 while closing a hole in the test suite's outbound-HTTP guard.

`tests/conftest.py` blocked `requests.get` only. A `requests.Session` bypassed
it, and yfinance does not use `requests` at all — it ships its own transport on
curl_cffi [measured: `yfinance.data` references `curl_cffi` and `session.get`,
and `requests.Session` zero times]. So the guard never applied to the one
library that actually reached the internet.

Closing both holes exposed five tests that silently depended on a live Yahoo
Finance response. Four were not about market data and now state their own
sectors. The fifth is this item: a test whose premise is replaying a RECORDED
session downloads SPY and per-symbol price history on every run, reports
`TECH DATA BLIND SPOT`, and never reaches the Portfolio Manager.

It **fails on `origin/main` today** with the network reachable, taking 196
seconds [measured 2026-09-30], so it is pre-existing rot rather than a
regression from the guard.

Do NOT fix it by loosening the guard, skipping the test, or marking it flaky.
That is the same error as raising a safety sweep's frequency instead of fixing
what the sweep is covering for.


### Item 202 update — the isolation was never real (2026-09-30)

`ops/rehearsal/broker.py::blocked_market_data` replaces the market-data
provider with one that fetches nothing, and `ops/rehearsal/isolation.py`
describes a socket wall covering "Anthropic, OpenAI, OpenRouter, Alpaca,
yfinance, FRED and RSS". Neither held: price data still reached the rehearsal
through **curl_cffi**, which is yfinance's own transport and which the test
suite's outbound-HTTP guard did not cover.

So the rehearsal has been validating against LIVE market data while claiming
to be offline, deterministic and free. With the hole closed, the session
degrades honestly to `status='no_data'` and never reaches the Portfolio
Manager, which is why `test_the_settled_cost_ceiling_still_suspends_paid_analysis`
cannot build its 'before' case.

That test is marked `xfail(strict=False)` with the reason above — NOT as a
flake. It flips to XPASS the moment this item serves recorded market data,
which is the signal that item 202 is done.

It also fails on `origin/main` today, taking ~196 seconds of live fetching
[measured 2026-09-30], so the defect predates the guard rather than being
caused by it.
## Item 202 — the rehearsal harness reaches the network

Found 2026-09-30 while closing a hole in the test suite's outbound-HTTP guard.

`tests/conftest.py` blocked `requests.get` only. A `requests.Session` bypassed
it, and yfinance does not use `requests` at all — it ships its own transport on
curl_cffi [measured: `yfinance.data` references `curl_cffi` and `session.get`,
and `requests.Session` zero times]. So the guard never applied to the one
library that actually reached the internet.

Closing both holes exposed five tests that silently depended on a live Yahoo
Finance response. Four were not about market data and now state their own
sectors. The fifth is this item: a test whose premise is replaying a RECORDED
session downloads SPY and per-symbol price history on every run, reports
`TECH DATA BLIND SPOT`, and never reaches the Portfolio Manager.

It **fails on `origin/main` today** with the network reachable, taking 196
seconds [measured 2026-09-30], so it is pre-existing rot rather than a
regression from the guard.

Do NOT fix it by loosening the guard, skipping the test, or marking it flaky.
That is the same error as raising a safety sweep's frequency instead of fixing
what the sweep is covering for.


### Item 202 update — the isolation was never real (2026-09-30)

`ops/rehearsal/broker.py::blocked_market_data` replaces the market-data
provider with one that fetches nothing, and `ops/rehearsal/isolation.py`
describes a socket wall covering "Anthropic, OpenAI, OpenRouter, Alpaca,
yfinance, FRED and RSS". Neither held: price data still reached the rehearsal
through **curl_cffi**, which is yfinance's own transport and which the test
suite's outbound-HTTP guard did not cover.

So the rehearsal has been validating against LIVE market data while claiming
to be offline, deterministic and free. With the hole closed, the session
degrades honestly to `status='no_data'` and never reaches the Portfolio
Manager, which is why `test_the_settled_cost_ceiling_still_suspends_paid_analysis`
cannot build its 'before' case.

That test is marked `xfail(strict=False)` with the reason above — NOT as a
flake. It flips to XPASS the moment this item serves recorded market data,
which is the signal that item 202 is done.

### Item 202 update 2 — the swap missed the stage that owns the provider (2026-10-01)

Serving recorded bars was not enough on its own. `TradingPipeline.__init__`
hands the SAME market-data object to the stages it builds
(`MorningResearchStage(market=self.market, ...)`), so replacing
`pipeline.market` afterwards left the technical read — the read a rehearsal
most needs served from the recording — still pointing at the LIVE provider.
Offline that read as "No data for SPY, skipping" for every symbol and the
session degraded to `status='no_data'`; online, before the curl_cffi hole was
closed, it is what actually downloaded the bars. `run_rehearsal` now rebinds
every holder and raises rather than starting if one still points at the live
provider.

Measured after the rebind (2026-10-01): `tech_analyst` runs offline and
appears in `agents_ran`, where before it did not. The test still XFAILs, now
for two different and named reasons: the run ends on
`APIConnectionError: Connection error.` before the Portfolio Manager, and
other components still construct their own `MarketDataProvider`, whose
blocked yfinance crumb fetches retry per symbol (~188s). Those two are what
is left of item 202; the xfail reason on the test says the same thing and
flips to XPASS when they are served.

Not found, and checked because the same class of bug bit elsewhere: nothing
under `ops/rehearsal/` dates anything by `date.today()` or `datetime.now()`.
The only `utcnow()` is the capture timestamp written into the recording's
metadata, which is a provenance stamp, not a trading date.

It also fails on `origin/main` today, taking ~196 seconds of live fetching
[measured 2026-09-30], so the defect predates the guard rather than being
caused by it.
