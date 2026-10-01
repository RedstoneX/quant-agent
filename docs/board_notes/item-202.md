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

### Item 202 update 3 — the fourth transport, and the sector lookup (2026-10-01)

**The Portfolio Manager now runs offline.** `replay_provider_calls` replaced
three transports — `_anthropic_call`, `_call_openai`, `_call_deepseek` — all of
them PRIMARY-path entry points. `_try_failover` and `_try_tertiary` do not go
through any of them: both build their own client and call
`_openai_wire_call` directly. So the moment a replayed primary raised, the
Portfolio Manager failed over to a REAL provider and the session ended on
`openai.APIConnectionError: Connection error.` [measured 2026-10-01]. The
shared wire call is now replayed as well, which closes both failover routes at
once and leaves the retry, failover, cost-accounting and circuit logic above it
untouched and real.

What the run ends on now is `MissingRecordedResponse: no recorded response for
agent 'portfolio_manager' (run run-574fda72): all 1 recorded response(s) were
already replayed` [measured 2026-10-01]. That is the mandated behaviour, not a
new defect: the session asks the PM more than once and the pinned recorded run
holds one PM answer, so the replay stops and names exactly what the recording
could not supply rather than inventing an answer or going out to buy one.

**Nothing in the rehearsal builds a live market-data client any more.**
`broker._get_sector` reads `yf.Ticker(symbol).info`, which is a second live
fetch the `pipeline.market` swap never touched, and behind the wall yfinance
retried its cookie/crumb handshake once per symbol. `recorded_sector_lookup`
replaces the `yf` name inside `src.execution.broker` for the duration of the
session — the single place that client is built, so no importer of
`_get_sector` (`src.pipeline` binds it at import time) can route around it —
and serves the sector from the recording when it is there, or records a missing
recorded input when it is not. Nothing is substituted: an unrecorded symbol
takes the same path a yfinance outage takes, which the sector gate already
surfaces to the owner as "Unknown". `market_recording.capture` now records
sectors alongside the bars so a freshly captured recording can serve them.

**Measured:** blocked outbound attempts fell from 12 to 11, and the single
`curl_cffi` entry — the yfinance one — is gone. Every attempt that remains is
either FRED (`api.stlouisfed.org`, 15 series) or one of the 20 news feeds
(CNBC, MarketWatch, Yahoo, Seeking Alpha, Investing.com, Nasdaq, BBC, NPR, Fed,
SEC). Those have no recording of any kind yet, so the run is still correctly
voided as non-hermetic. Recording them is a separate build and was not
attempted here.

**Not done in this pass, and deliberately not claimed:** the third criterion —
naming every OTHER test that still reaches the network — needs a full-suite run
that was not performed, so it stays open with no list attached. A grep produces
candidates, not a measurement, and a candidate list posted as a finding is the
kind of thing this item exists to stop.

**The settling run is still outstanding.** No real rehearsal against the
production snapshot was performed here, and none should be read into these
numbers: everything above was measured through
`tests/test_rehearsal_reproduces_cost_ceiling.py`. What a settling run would
prove, and nothing else can, is that a full session over the production
snapshot completes with an EMPTY breach journal and a verdict the rig is
entitled to give.

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
