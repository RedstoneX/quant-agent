# QAMC Defect Log

**What this file is:** a permanent, plain-language record of things that broke
and what was done about them. Newest first.

**Why it exists separately from `docs/WORK.md`.** WORK.md is the active
backlog and it has a hard 100,000-byte cap, so finished incidents get trimmed
out of it as new work arrives — which means the history of what went wrong
was being deleted by design. Git kept it, but git is not readable by the
owner. Rex asked for a record that does not disappear. This is it.

**Rules for this file.** Append, never trim. Never delete an entry to save
space; if it gets long, split by year. Every entry leads with one
non-technical line stating what actually broke in ordinary words, because the
person who most needs to read this is not a developer. Detail goes underneath
for whoever has to fix it again.

**Never record here what the repo already records** — no restating code, no
commit IDs as the substance of an entry. Record the *reasoning*: what the
symptom was, what the real cause turned out to be, what was ruled out, and
what would catch it next time.

---

### 2026-09-01 — the benchmark rig could not spend money; the trading desk was never affected

**In plain words:** three separate things stopped a model test from running.
None of them touched live trading. All three were in the test setup, and all
three were fixed the same afternoon.

**1. The working copy had no credentials file at all.** `AppConfig` refuses to
load when a provider named in `settings.yaml` has no key, so the benchmark
died at startup before any model call. Same failure family as the
`GOOGLE_API_KEY` blocker the day before (below) — and the same fix: a `.env`
holding only `placeholder-managed-by-onecli` values, with the real secrets
injected by the gateway on the way out. **The lesson repeats: a green CI run
says nothing about whether a config will load in the environment that has to
run it.**

**2. The benchmark billed against production's live-trading spend caps.**
`benchmark_models.py` runs inside the real LLM cost circuit, whose limits are
`session_cost_limit_usd: 0.90` and `daily_cost_limit_usd: 2.75`. Those exist to
stop a runaway *trading* session; a deliberate 23-trial benchmark is not that,
and would have tripped the session cap partway through and reported a
misleading partial result. Fixed by running against a benchmark-only config
with raised ceilings and a scratch database, so production's ledger, database
and daily budget are untouched. **Do not raise the production limits to make a
benchmark fit.**

**3. Muting Telegram latched the cost circuit closed.** The circuit treats a
reachable operator as a mandatory precondition for spending anything —
`require_telegram_alerts`, which `config.py` explicitly refuses to let anyone
set false. Setting `TELEGRAM_DISABLED=1` (to stop a benchmark alerting Rex's
phone about a circuit that is not his desk) therefore disabled the notifier,
which marked the circuit unavailable, which blocked all paid analysis. The
correct lever already existed: `QAMC_REHEARSAL=1` keeps the notifier *enabled*
so the precondition is honestly satisfied, while suppressing and logging every
send. Blast radius verified as `src/notifier.py` alone. **This is a good
design working as intended — the fix was to use it correctly, not to weaken
it.**

**Also cleared:** the failed attempt left an emergency-latch sidecar
(`bench.db.llm-circuit-unavailable`) that would have kept the next run blocked.
A latch is sticky by design; clearing it is an explicit operator step.

### 2026-09-01 — GLM 5.2 fails outright roughly one run in three at the PM seat

**In plain words:** a cheap model that looked perfect turned out to produce
nothing usable on one run out of three. When it fails, the portfolio manager
returns no decision at all, so that session places no trades.

Two trials on 2026-08-31 scored 1.00 at $0.055/run and made GLM 5.2 look like
an ~8x cost saving at the most expensive seat. A third trial on 2026-09-01
scored **0.00**: the model ran to `output_tokens: 16000`, hit the `max_tokens`
ceiling exactly, was truncated mid-JSON, and `finish_reason=length`. The
portfolio manager returned a non-JSON response and the whole review was
discarded.

**Ruled out before believing it was the model.** The scenario is a frozen
fixture, not live market data — input was byte-identical across all three runs
(21,033 tokens). `max_tokens: 16000` is a static config value, not fitted from
history, so the empty scratch database did not change it. GLM's own output
length is what varied: 7,219 / 9,071 / 16,000+. The settings comment notes the
16K ceiling was sized to cover "the observed production PM payload (~11K output
tokens)" — GLM runs well past that envelope.

**Why this matters more than the price.** A seat that silently produces nothing
one session in three is worse than an expensive seat that always answers. Cost
per run is the wrong metric on its own; cost per *usable* run is the number.
Two samples said "perfect" and were wrong — this is the argument for repeats.

Status at time of writing: a 10-repeat run is in flight to establish the real
truncation rate. **The seat was NOT moved.** GPT-5.5 remains the portfolio
manager.

---

### DEPLOYED 2026-08-31 21:20 UTC — and one blocker that tests could not catch

Running at `b88b836`. PRs #202 and #203 both merged, API restarted, served
cockpit bundle matches disk, rehearsal PASS against the deployed code with the
production database byte-identical.

**The blocker worth remembering: `GOOGLE_API_KEY` was not on the box.** Seven
seats now declare `provider: google`, and `_check_llm_provider_keys` refuses to
load a config whose in-use provider has no key. Not degraded — the desk would
not have STARTED, and the first symptom at 09:30 would have been silence. Every
test passed and both PRs were green; the thing that would have broken lived
entirely outside the repo. Fixed by adding
`GOOGLE_API_KEY=placeholder-managed-by-onecli` (same convention as
OPENROUTER_API_KEY — the real credential is injected by the gateway on the way
out; .env only needs it non-empty). Original .env backed up to
`/home/qamc/.env.bak-2026-08-31`, OUTSIDE the repo so it cannot read as drift.
**Generalise this: after any change to which providers the seats use, load the
config ON THE BOX before trusting a green CI run.**

**Proven live, not assumed.** A forced evening session ran on the new routing at
21:29 UTC: `evening_analyst` on `gemini-3.5-flash-lite`, 20,642 tokens, **cost
$0.00**, status `analyzed`. The free tier works end to end through the gateway,
and the $0.00 pricing row settles correctly rather than reading as unknown. The
two new alert lines were also confirmed rendering from the box.

**Runway.** OpenRouter is prepaid: $32.09 left, per-key cap removed. A CLEAN
day costs $1.02 (2026-08-27 — the only day that week where all six sessions ran
and the morning completed first time). Never estimate from an average of recent
days: they are cheap precisely because the desk kept crashing early.

### 2026-08-31 evening — the reward:risk gate was being narrated, not computed

**Two forced sessions rejected every trade. Neither rejection was a judgement
call; both were the deterministic constructor and the LLM Risk Manager
disagreeing about facts the RM should never have been asked to derive.**

- **The RM was doing the arithmetic its own gate is judged on.** It receives
  the constructed order as bare Entry/Stop/Target text with no ratio. For a
  BUY on RSG it computed the ratio TWICE IN ONE RESPONSE — `rr_audit` said
  "R/R = 1.65 ... above 1.5, so compliant", `reasoning` said "R/R = 1.31,
  which is below the 1.5 floor" and rejected. The pipeline acts on
  `reasoning`. 1.65 is right; 1.31 matches no combination of the inputs.
  `TradeDecision.reward_risk` is now a Python computed field, mirroring
  `TechAnalysisResult.risk_reward` and its "not trusted to the LLM" rule,
  rendered into the prompt and declared authoritative there. PR #202.
- **The constructor removed trades without telling anyone.** It struck NVDA on
  the reward:risk floor; PM's narrative — written BEFORE construction and
  rendered verbatim — still argued for it, so the RM vetoed the whole plan
  ("While COP and V are valid, the plan as presented is not internally
  consistent"). Two valid trades died for a bookkeeping mismatch.
  `PortfolioDecision.constructor_dropped` now carries removals into the
  prompt. Same pattern as the existing `cap_note`, whose own comment records
  the identical failure from 2026-08-20 — solved once for allocation caps,
  never extended to removals. PR #202.
- **Ten new tests over the constructor→RM handoff, which had none.** No test
  anywhere built a widened-stop order and asserted what the RM prompt shows
  for it. That is exactly the seam both defects lived in.

**Three grandfathered stops widened to the noise-band floor.** V, DIS and
CMCSA were opened 2026-08-27 13:36 UTC; `min_stop_atr_multiple = 3.0` was
committed the same day at 22:28 UTC, after the close, so they were never
subject to it. Measured 2026-08-31 they sat at 1.02x / 1.03x / 1.62x ATR —
inside a single ordinary day's range for the first two. Widened via
`broker.replace_stop_loss(..., allow_lowering=True)`, the same supported path
the ex-dividend adjustment uses, NOT a hand edit: it snapshots and rolls back
on failure and leaves no unprotected window for the ~30-min coverage
reconciler to "repair" by reinstating the original tight stop. MSFT was left
alone — its live stop had already trailed up to 485.10 and is correct against
current price, not the 480.30 the entry row still records.

**The ledger was corrected, on owner instruction, and the tool has no path for
it.** Recorded spend went 2.1741 -> 0.2494, the exact sum of `agent_logs`
provider-reported costs for the ET day. `scripts/cost_circuit.py` only ever
clears latches and promises never to erase settled spend, so this was a direct
row edit. **Editing `llm_budget_days` alone raises an emergency latch** — a
hard check joins it to `llm_budget_sessions` and `llm_budget_reservations`
(`cost_circuit.py:1216-1247`) and both ledgers must move in one transaction.
Learned by tripping it. Prior row backed up to
`data/ledger_backup_2026-08-31.json`; caps unchanged.

**STILL UNRESOLVED — do not let anyone tell you this is closed.** Whether a
real OpenRouter rate-limit reaches `_is_known_zero_cost_failure` carrying
`status_code = 429` is UNVERIFIED. Reading the allow-list is not proof. The
rehearsal rig cannot settle it either: `ops/rehearsal/faults.py` builds its
`RateLimited` fault with a hard-coded `status_code = 429`, so it encodes the
assumption in doubt and can only ever confirm it. Settling this needs a
captured real rate-limit, or a classifier robust to both shapes.

### DONE 2026-08-31 — primary moved to Gemini direct, OpenRouter to backup

**Shipped as PR #203.** Seven specialist seats run `provider: google` /
`gemini-3.5-flash-lite` on the AI Studio free tier; OpenRouter carries the
same model as a paid backup, so a failover changes the road and not the
reasoning. The failover target is configuration now, not the hard-coded
`claude-opus-4-7` — an inherited remnant at ~50x the primary's price that had
never once completed, because no Anthropic credential was ever configured.
Measured limits, endpoint verification and rates are in the code comments and
PR #203; not restated here.

**Three things that are still open, and only these:**

- **`position_reviewer` was deliberately NOT migrated.** It is a decision seat
  and `test_decision_seats_run_a_model_measured_at_that_seat` demands a model
  measured at its own scenario. No benchmark exists for 3.5 at `midday_exit`,
  and none can be produced against the 2.5 incumbent because Google refuses
  2.5 to new keys. It moves after
  `ops/model_policy/benchmark_models.py --scenario midday_exit --models gemini-3.5-flash-lite`
  is run and committed.
- **`ops/commissioning/verify_commissioning.py` carries stale
  `EXPECTED_PROVIDER` / `EXPECTED_ROUTING` constants** and will report FAIL
  against the new routing. Its own tests do not catch this because they
  self-reference the same constants instead of reading `settings.yaml`.
- **This saves ~7% of spend, not the bill.** Measured from `agent_logs` over
  7 days: `openai/gpt-5.5` (portfolio_manager) is $6.64 of a ~$7.16 week — 93%
  of spend on 21% of calls. The eight Gemini seats are $0.51 between them. The
  cost ledger remains, almost entirely, a guard on the PM seat. The real lever
  there is input size and prompt caching, NOT a weaker model — the owner has
  ruled that out and is right to.
### 2026-08-31, afternoon — charged $0.62 for calls that cost nothing

**The rate limit did not stop trading. The phantom bill for it did.**

A logical call can attempt several DIFFERENT providers, and `BaseAgent.run()`
re-raises the PRIMARY error while discarding whatever the failover hit. The
cost circuit judged "did this cost anything" from that single exception — so a
failover rejected **401** (which by definition billed nothing, and which the
zero-cost allow-list already covers) was invisible to it, and the whole call
was charged its conservative reserve at the **failover model's** dearer price.

Observed live at 12:36 ET: `news_analyst` refused 429 upstream, refused again,
then 401 from the failover for want of an Anthropic credential. Real cost
**$0.00**. Charged **$0.6159**. The unexplained spend tripped
`failed_call_unknown_cost` and latched the desk — for the third time that day,
and on the same underlying pattern as the 09:32 shutdown.

**Fixed:** every attempt's failure is now carried to the circuit, and a call
is treated as free only when **every** attempt is provably free. The rule gets
STRICTER, not looser — one ambiguous attempt (a cut stream, an unclassified
error, a 5xx after generation may have started) and the whole reservation is
charged exactly as before. A call is only known to have cost nothing when
nothing it did could have cost anything. Callers that cannot enumerate their
attempts keep the old single-exception behaviour, so this can only ever
recognise more genuinely-free failures, never fewer.

**And an unknown turned into a measurement.** When a call IS charged, the
shapes of every attempt are now logged — exception type, status code, and
which one made it chargeable. The remaining open question is whether
OpenRouter's "temporarily rate-limited upstream" arrives with a status the
allow-list carries; it could not be settled by reasoning and deliberately was
not settled by hammering the provider to reproduce it. The next occurrence
will say.

### 2026-08-31, afternoon — why the provider was refusing us, and the fix

**The owner's framing, and it was the right one: never discover a limit by
hitting it.** Every request costs money, so the work of sizing one belongs on
our side of the wire. That rules out both obvious designs — a rate ceiling
that backs off when it trips, and a ceiling that learns from refusals — because
each pays the provider for the lesson.

**What was actually happening.** Every one of the eleven rate limits in three
weeks of logs hit the **same agent**, the Technical Analyst, and no other agent
was declined once. It is the only one that bursts: ~314,000 tokens inside 80
seconds, ~252,000 per minute, against 20-30k for everything else. Not market-open
congestion — the refusals land at 10:01, 10:30, 11:31 on other days. And
OpenRouter's own message says why: *"add your own key to accumulate your rate
limits"* — we share its pooled Google credentials, so other customers' traffic
counts against us.

Nothing bounded the burst. Concurrency was capped at three requests and context
was capped per call, but a rate limit counts **tokens per minute**, and three
concurrent 80,000-token requests clear both caps while being exactly what gets
refused.

**Three things were wrong, all measurable:**

1. **72% of the payload was raw price bars, and half of that was noise.** Bars
   were serialised at full float64 precision — `O=14.920000076293945` for a
   stock that traded at $14.92. Those digits are float32-to-float64 conversion
   artefacts, not price data; the source could not represent them. Rendering at
   six significant figures cuts the payload 30% and loses nothing.
2. **The batch split was a fixed count calibrated against a stale assumption.**
   `_CHUNK_SIZE = 25` was set against "~300 input tokens per symbol". The bar
   window grew from 20 to 40, per-symbol context was added, and the real figure
   became ~3,600. Nothing failed loudly, because context length was never the
   binding constraint — the model takes a million tokens. **A count cannot bound
   tokens.**
3. **Relative-strength context depended on how the batch was split.** The index
   ETFs sit at the top of the universe, so only the first chunk ever contained
   SPY; every symbol after it silently lost its benchmark. Which symbols kept it
   was a function of the chunk size.

**The fix — build to a budget, not to a count.** Requests are packed until the
next symbol would exceed a token budget, then a new request starts. An oversized
request is not unlikely, it is unreachable: nothing is added to a full batch.
The bytes-to-tokens conversion comes from a model fitted to each agent's own
past calls — data already paid for, so measuring costs nothing and improves as
the desk runs. It never learns from a refusal.

The model is two parameters, and both are physically real:

    tokens = fixed_tokens + tokens_per_byte x content_bytes

    tech_analyst        4,127 tok + 0.939 tok/byte
    news_analyst        4,200 tok + 0.227 tok/byte
    portfolio_manager  12,483 tok + 0.214 tok/byte

The intercepts recover each agent's system prompt (tech_analyst's is 19,792
bytes of prose; 19,792/4,127 = 4.8 bytes per token, exactly what English
tokenizes at). The slopes recover the content: ~1.07 bytes/token for dense OHLCV
digits, ~4.4 for prose. Median prediction error 0.9-4.3%. A single
bytes-per-token ratio cannot express this — it folds a per-request constant into
a per-byte rate, so it is right at only one message size, and tech_analyst's
messages span 6KB to 379KB.

**Measured on 53 real production symbol sections:**

| | requests | peak request | total |
| --- | --- | --- | --- |
| before | 3 | 136,449 tok | 290,329 tok |
| trim only | 3 | 96,135 tok | 205,833 tok |
| **trim + 45k budget** | **5** | **44,654 tok** | **214,087 tok** |

**Peak down 67%, total down 26%** — both directions improve, because the trim
more than pays for the extra repeated system prompts. Worst case with all three
concurrent slots full is ~134k/min, against the ~252k/min that was being refused.

**And the repeated system prompts may be free anyway.** This seat's route bills
a cached prompt token at $0.01/M against $0.10/M — a 10x discount that decides
the trade-off outright. Nobody knew whether it was happening because nothing
read the field. Cache hits are now logged; the answer arrives with the next
session's logs.

**Why 45,000 and not something else.** It is where the curve knees: 60k saves 2%
of total tokens for a 33% higher peak, 100k saves 4% for a peak nearly three
times larger. Overridable with `QUANT_AGENT_TECH_REQUEST_TOKENS`.

**A tokens-per-minute governor exists but is a BACKSTOP, not the control.** It
cannot fire in normal operation now — the packer cannot emit a burst that large.
It is there for what the budget cannot see (a new agent, a prompt that grows, a
retry storm) and it reports at CRITICAL if it ever engages, because that would
mean something grew.

**Failure posture, deliberately.** Nothing in the sizing path may take a session
down: every entry point catches broadly and degrades to the old fixed split. An
efficiency measure that can stop a trading desk is a bad trade — which is the
whole lesson of the morning recorded below.

**Left open, owner's call:** our own Google key (a free tier exists, and Google
serves the *same* Gemini model directly, so it is a same-model backup with no
consistency problem at all); and whether to pin a cheaper route — the identical
model is served by five endpoints, one at half our current price.

**Not the cause, and a correction to an earlier note in this file:** the
reservation estimator's conservatism was reported here as 6.8x. That was an
apples-to-oranges comparison of morning estimates against intra-day actuals.
Measured like-for-like across ten runs it is **1.49x**, which is roughly what a
deliberately conservative reserve should look like. It contributed to the
2026-08-28 ceiling trip; it is not the pattern, and it has not been changed.

### 2026-08-31, market open — the desk went dark two minutes after the bell

**Closed by `fix/provider-failover-attempt-budget`. Read this before touching
the retry, failover or cost-circuit code.**

What happened: at 09:32 ET, two minutes after the open, the morning session
stopped with `paid_analysis_suspended` and every session after it no-opped.
Spend at the moment it stopped: **$0.05 of a $2.75 day.** It required a
manual operator reset, so the desk stayed dark until a human noticed.

The cause was an arithmetic contradiction between two settings that lived in
different files and were never compared:

| | value | where |
| --- | --- | --- |
| attempts the retry loop can spend on one call | 3 (two primary + one failover) | `_max_retries()` in code, env-overridable |
| attempts the cost circuit permitted per call | 2 | `config/settings.yaml` |

So **cross-provider failover could never once complete.** Every failover was
attempt three against a ceiling of two. It only ever mattered when the
primary provider failed — which is the one situation failover exists for —
and on 2026-08-31 an upstream rate-limit on the cheap primary finally
produced it. The same family of limit had tripped on 08-26, 08-27 and twice
on 08-28; each time a different number was raised and this one was not
touched.

Making it worse, `provider_attempt_limit` was the only trigger of its family
still wired to the durable operator-reset latch. Its strictly WIDER sibling,
`session_retry_attempt_limit`, was already correctly scoped to the session.
Nothing chose that: unrecognised codes default to a hard latch.

What changed:

1. **The ceiling is derived, not typed.** `provider_attempt_budget()` in
   `src/agents/base.py` owns the arithmetic, next to the loop that actually
   spends the attempts. `config/settings.yaml` no longer pins it.
2. **Disagreement is now a startup failure.** `AppConfig` refuses to load a
   ceiling below the loop's worst case, naming both settings. It fails on
   the ground instead of at 09:32 on a Monday.
3. **`provider_attempt_limit` holds the session instead of latching the
   desk**, matching its sibling.

**Why a weekend of testing and auditing missed it, and the thing actually
worth remembering:** the suite had thorough failover tests AND thorough
circuit tests, and every one of them passed throughout. `tests/conftest.py`
sets `_allow_unmetered_for_tests = True` for the whole suite, so the failover
tests ran with **no cost circuit attached at all**, and the circuit tests ran
with no failover. Nothing anywhere ran the two together — which is precisely
where they contradicted each other. Two well-tested halves, an untested seam.
`tests/test_provider_attempt_budget.py` now attaches a real breaker to a real
agent and fails the primary for real reasons.

**And the rehearsal rig could not have caught it either**, which is why it
now can — see the fault-injection note above. Every recorded response it
replays is a response that succeeded, so the failure branch was unreachable
offline. It was reachable only by waiting for the market.

**A second defect this one was hiding, found by deploying it (same day):**
`fail_call` stamps the ET day's accounting inexact when it charges a
conservative reserve for a request whose true cost it never learned, and
nothing clears that flag within the day — it lifts only when the next ET day
seeds a fresh row. The quota reconciler refuses to rearm over an inexact day,
by design. Those two facts had never met, because an inexact day always came
with a hard latch and the reconciler returns early whenever one is set. **The
latch was masking the refusal.** Scoping `provider_attempt_limit` to the
session removed the mask, and the live desk went straight from "suspended" to
"every session start raises, and no operator action can clear it until
midnight". A crash loop is a worse failure than the suspension it replaced.

Fixed by letting an operator reset clear an inexact day, with the same
mandatory audited reason. It clears only the *we-could-not-prove-this-figure*
flag; the recorded amount is left exactly as it stands, conservative reserve
included, because that over-states cost rather than under-stating it and
`scripts/cost_circuit.py` promises reset never erases settled spend. Judging a
conservative figure good enough to continue on is an operator's call;
recomputing what the provider really charged is not something the code can
honestly do.

**The one underneath both of those — the real cause of the pattern (found
10:36 ET on the live desk, while verifying the fix):** the quota reconciler
runs on **every** paid call, and it refused to proceed whenever the current
ET day's accounting was inexact — even when it had nothing to do. Its
exactness precondition guards exactly one operation: releasing a hold carried
over from an *earlier* day. The check sat before the query that finds those
holds, so it fired when there were none.

`fail_call` stamps the day inexact whenever it charges a conservative reserve
for a request whose true cost it never learned. So **the first failed request
of any day poisoned every paid call after it** — the refusal reads as the
circuit's own infrastructure failing, which writes the emergency latch and
stops the desk until an operator clears it.

One rate-limited request, and the trading day was over. **That is the
2026-08-26 / 08-27 / 08-28 / 08-31 pattern**, and it survived four rounds of
raising limits because nobody was looking at the reconciler: the hard latches
masked it, since it returns early whenever one is set. Removing the last mask
is what finally showed it — as a crash rather than a suspension, which is
worse, and which is why it was found within minutes of deploying instead of
next Monday.

Fixed by restoring the check to what it protects: no cross-day hold, nothing
to rearm, nothing to be exact about. The safety property is unchanged and
pinned by its own test — rearming yesterday's stop on unproven books is still
refused.

**Two things deliberately left for the owner, not fixed here:**

- **The backup model costs ~50x the primary.** Primary is
  `google/gemini-2.5-flash-lite` at $0.10/$0.40 per million tokens; the
  failover target is hard-coded to `claude-opus-4-7` at $5.00/$25.00. For
  the Technical Analyst's ~150k-token prompt that is ~$0.02 versus ~$0.95 —
  against a $0.90 per-session cap. So a failover on the biggest agent now
  completes and delivers its analysis, then immediately holds the session on
  cost, which can starve the Portfolio Manager that runs after it. Better
  than a dead desk, but not a full rescue. A backup priced near the primary
  would be; `_FALLBACK_MODEL` is a module constant with no config knob.
  Model strategy is TABLED pending a clean spend re-measure, so this is
  recorded, not changed.
- **`fail_call` still charges for a request the circuit itself refused to
  send.** A blocked attempt is provably $0 — no bytes left the process — but
  the reservation covers the whole call, and earlier attempts on that same
  call may have burned tokens nobody was told about. Marking it zero-cost
  would risk under-counting real spend, which on a system that trades money
  is the worse error. It no longer fires on this failure mode (with the
  ceiling correct the failover simply succeeds), so what reaches it is a
  genuine attempt runaway — arguably a thing an operator should look at.
  Asserted explicitly in `test_cost_circuit.py` rather than glossed.
