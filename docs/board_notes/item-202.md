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

### Item 202 update 4 — the settling run, and the first measured network list (2026-10-01)

**The settling run was performed and it VOIDED.** One morning session, real
production snapshot, replay pinned automatically, taken as the owning POSIX
account read-only; the sandbox report records the production database as
byte-identical afterwards, broker credentials as non-functional sentinels, and
no order of any kind was submitted anywhere. The session ran END TO END — every
stage, including the Portfolio Manager, which is new: the previous pass could
not get past `MissingRecordedResponse`. It then ended on
`HermeticBreach: 11 outbound connection attempt(s) were blocked`, which is the
mandated outcome, not a new defect. **A voided run is not a pass**, so this
settles that the harness runs a complete session offline and leaves production
untouched, and settles NOTHING about the verdict the rig would give.

The eleven blocked endpoints, measured from the breach journal and not from a
grep: `api.stlouisfed.org` (FRED, 15 series), and ten news/reference hosts —
`search.cnbc.com`, `feeds.marketwatch.com`, `finance.yahoo.com` (the RSS feed,
not yfinance), `seekingalpha.com`, `www.investing.com`, `www.nasdaq.com`,
`feeds.bbci.co.uk`, `feeds.npr.org`, `www.federalreserve.gov`, `www.sec.gov`
(press releases AND the company-ticker map, which retried three times). The
count matches the 11 predicted from the test harness, so nothing new leaks; what
is needed is a recording for these, which remains a separate build.

**Two things the settling run found that the test harness could not.** The
pinned market recording holds bars for 33 symbols and sectors for ZERO, so
every sector lookup in the session was reported as a missing recorded input —
`market_recording.capture` learned to record sectors only after that recording
was taken, and a fresh capture is required before a sector-clean run is
possible. Separately the Alpaca asset directory is unavailable offline and
eligibility fails closed for every candidate, which is correct behaviour but
means a hermetic run can never clear the eligibility gate until that directory
is recorded too. Neither is a regression; both are unrecorded inputs on the
same list as FRED and the feeds.

### Item 202 update 5 — criterion one is now MEASURED (2026-10-01)

The outbound-HTTP guard in `tests/conftest.py` raised without recording, so a
test that CAUGHT the error was indistinguishable from a test that never called
out, and "no test reaches the network" could only ever be assumed. The guard now
appends `nodeid<TAB>url` to the file named by `QAMC_NETWORK_JOURNAL` before it
raises; unset — which is CI and every ordinary run — it costs one environment
lookup and changes nothing. Verified against a deliberate probe test that
swallows the error: the attempt still lands in the journal.

**Measured over one full-suite run with the journal on: 9317 passed, 6 skipped,
1 xfailed, and 1139 blocked outbound attempts from 247 tests across 45 files.**
Every one was swallowed by the test rather than failing it, which is exactly the
population the old guard could not see. By host: 1034 to Yahoo Finance
(`query1`/`query2`, the sector and price lookups), 99 to
`raw.githubusercontent.com` (the LiteLLM price table), 6 to `openrouter.ai`.
None of them fail the suite, so none is a blocker; the list is the criterion's
answer, and the Yahoo bulk is the same sector lookup the rehearsal needs
recorded.


### Item 202 update 6 — FRED and the news feeds are now recorded (2026-10-01)

The settling run voided on 11 blocked endpoints and every one was FRED
(`api.stlouisfed.org`, 15 series) or one of the ten news/reference hosts.
Both are now served from `ops/rehearsal/recordings/feeds.json.gz` by
`ops/rehearsal/feed_recording.py`, which copies the pattern the bars and the
sector lookup already use rather than adding a second mechanism: capture once
as an operator command, then patch the name in the module that BUILDS the
client, so everything above the transport stays real.

Two transports, because the dependency set has two. `src.data.macro` builds
`fredapi.Fred` (a bare `urlopen` of its own, so patching `urlopen` would not
have caught it), and `src.data.news`, `src.data.event_calendar` and
`src.data.earnings` each import `urlopen` into their own namespace — the FRED
release-dates call, the Fed/SEC pages and the ~20 RSS feeds all go through
that one name. The retry, budget, coverage-accounting and honest-degradation
logic above both is untouched, and nothing changes what any provider is asked
for.

**Failures are recorded as failures.** Every FRED failure in the retained log
is `fetch_deadline_exceeded`; a recording that only replayed successes could
not reproduce the thing the log is full of. A recorded failure is re-raised at
the same place the live one was raised.

**A gap raises.** An unrecorded series or URL is named in the `unavailable`
list — which `assert_hermetic` turns into a loud `MissingRecordedInput` — and
the call itself raises rather than returning a default, an empty body or a
computed stand-in. Credentials are stripped before a URL becomes a recording
key, so no API key is written to disk and the recording replays under any key.

**Measured 2026-10-01:** `tests/test_rehearsal_feed_replay.py`, 7 tests, each
run inside the rehearsal's own `no_network` wall — the wall journals any
outbound attempt, and the journal is empty for every served call, which is the
same discriminator the curl_cffi hole needed. Not claimed: the settling run was
NOT repeated here, so no verdict follows from this; and two unrecorded inputs
named in update 4 remain — the pinned market recording holds zero sectors, and
the Alpaca asset directory is unavailable offline.

### Board text moved here (2026-10-01, per-item byte budget)

The item block below is the full prose that stood in `docs/WORK.md` before the
budget forced it down to a title, short checkboxes and this pointer. Nothing is
deleted; it is verbatim.

```
**202. The rehearsal harness is not hermetic — a replay of a RECORDED session still reaches live providers — filed 2026-09-30.** Closed so far: the curl_cffi hole, recorded daily bars, and (2026-10-01) rebinding the market provider on the morning-research stage, which held its own reference and so kept the live one after the swap — tech_analyst now runs offline [measured 2026-10-01]. `tests/test_rehearsal_reproduces_cost_ceiling.py::test_the_settled_cost_ceiling_still_suspends_paid_analysis` still XFAILs. 2026-10-01: the replay now covers the FOURTH transport (`_openai_wire_call`, which the failover and tertiary routes called directly) so the Portfolio Manager runs offline, and sector lookups no longer build their own yfinance client; the run now stops loudly on a missing recorded PM response instead of `APIConnectionError`, and FRED plus 20 news feeds are still not recorded.  2026-10-01: the settling run against the production snapshot was performed and VOIDED on the hermetic wall as expected — the full session now completes end to end including the Portfolio Manager, production was byte-identical afterwards and no order was placed, and the 11 blocked endpoints are FRED plus ten news/reference hosts; two further unrecorded inputs surfaced (the pinned recording holds zero sectors, and the asset directory fails closed offline). Criterion one is settled: the guard now journals each blocked attempt, and a full-suite run measured 1139 blocked attempts from 247 tests, all swallowed, none failing. detail: docs/board_notes/item-202.md

DONE WHEN:
  - [x] 2026-10-01 the run reaches the Portfolio Manager OFFLINE: `replay_provider_calls` patched only three primary-path transports, so `_try_failover` and `_try_tertiary` went straight to the live `_openai_wire_call`; that fourth transport is now replayed too and the run ends on a loud `MissingRecordedResponse` naming the agent and run instead of `APIConnectionError` [measured 2026-10-01]
  - [x] 2026-10-01 no component builds its own live market-data client: `broker._get_sector` was calling `yf.Ticker(symbol).info` per symbol behind the wall; it is now served from the recording (sectors are captured too) or recorded as a missing input, and zero yfinance/curl_cffi attempts remain in the journal [measured 2026-10-01: 12 blocked attempts before, 11 after, all of them FRED or news feeds]
  - [ ] STILL OPEN, not done in this pass: any OTHER test that still reaches the network is named, because the conftest guard now makes such a dependency fail loudly instead of silently
  - [x] 2026-10-01, THE ENFORCEMENT IS BUILT AND IS NOW MECHANICAL, which is what made every earlier fix rot: `no_network` already journalled each blocked outbound attempt, but the journal was printed in the report as narrative while the run still returned a verdict, so a replay that reached a live provider could still read PASS. `ops/rehearsal/isolation.assert_hermetic` now turns a non-empty journal into a raised `HermeticBreach` with NO opt-out of any kind, `run_rehearsal` calls it after every session and re-raises carrying the report, and `ops/rehearsal/run.py` exits 2 (void, "the rig could not judge it") instead of 0. This matters because every HTTP client in this dependency set catches broadly and retries, so a blocked call used to end as an empty result and a green run.
  - [x] 2026-10-01, A MISSING RECORDED INPUT NOW STOPS THE REPLAY instead of being filled in or quietly degraded: `assert_hermetic` raises `MissingRecordedInput` naming exactly what the recording could not supply. Nothing is invented — the only escape is the explicit operator flag `--allow-degraded`, which is stamped into the isolation checks as an accepted-degraded run and still cannot relax the network wall.
  - [x] 2026-10-01, THE ROT GUARD: `tests/test_rehearsal_hermetic.py` exercises every HTTP transport installed in this environment against TEST-NET-1 (RFC 5737, a literal address so the attempt reaches the wall rather than failing at DNS) and fails unless the rehearsal wall JOURNALLED the attempt — the discriminator the curl_cffi hole needed, since that transport succeeded rather than erroring. A new dependency with its own C transport breaks this test the day it is added. 12 passed, 2 skipped (aiohttp and pycurl, neither installed/synchronous here) [measured 2026-10-01].
  - [ ] STILL OPEN, and deliberately not claimed: the three criteria above this line all need a REAL rehearsal run against the production snapshot to settle, which was not performed in this pass. What is now true is that such a run can no longer pass while reaching a provider — it raises instead. Grepped on 2026-10-01: the only `MarketDataProvider()` constructions in the tree are `src/pipeline.py` (swapped and every holder rebound, with an existing assertion that no holder keeps the live one), `src/api/routes_live.py` and `src/backtest/data.py` (neither on a session path) and `ops/rehearsal/market_recording.capture` (the online recorder, never called from a rehearsal).


```
