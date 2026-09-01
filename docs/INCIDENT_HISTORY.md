# QAMC Incident History

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

---

## Archive — work completed before 2026-09-01

Moved out of `docs/WORK.md` on 2026-09-01 under the rule at the top of that
file: **finished work is moved here, never deleted.** WORK.md is capped and
loaded into context every session, so it must hold only what is still to be
done.

Every claim below was checked against `gh`/`git` and against the production
checkout before being moved — all 20 PRs cited are genuinely merged AND
deployed (production HEAD matched `origin/main` at the time of the check).
Four claims did NOT survive that check and are corrected inline where they
appear; they are listed here so the corrections are not buried:

1. **Phase 9 (the research desk deliberates) was listed as the FIRST pending
   item.** It is done: §9.1/9.2 shipped as PR #153 and §9.3/9.4 as PR #160,
   both deployed. Only §9.5 (the conviction ledger) is partial, and that was
   never named in the item.
2. **"Insider filter — PR #133 not yet merged/deployed."** Merged 2026-08-29
   and deployed. WORK.md already contradicted itself on this two hundred
   lines further down.
3. **"Inverse-ETF retirement still outstanding."** Obsolete rather than
   undone — the owner reversed this on 2026-08-30 and the inverse ETFs stay.
   `SH`/`SDS`/`PSQ`/`SQQQ` remain in the universe deliberately.
4. **"26 unmerged branches await triage."** The remote now carries 5
   non-main branches. The VPS security branch named there no longer exists;
   its salvageable content was rescued as PR #143.

Text below is moved verbatim. Where an entry contains its own later
correction, both the original claim and the correction are preserved — that
pairing is the record.

**Landed (2026-08-31) — six PRs, all merged and deployed. Nine open defects closed, four more deleted, six new ones found — read this first**

Tonight's audit worked through the thirteen open defects recorded below on 2026-08-30. Full detail and re-check commands for every item are in `docs/phases.yaml`'s `open_defects` entry — this is the plain-language summary.

- The macro event calendar is now half-real. The desk fetches a genuine forward schedule of seven US macro releases (CPI, payrolls, PPI, PCE, GDP, retail sales, jobless claims) from a free government source and shows it to the Risk Manager and the Macro Analyst, and the earnings-date lookup that existed but was never called is now wired in. **Still missing: Fed meeting dates specifically.** No free source publishes those, so both prompts now say so outright instead of guessing. Whether to add the Fed's own free calendar page just for that has been put to the owner — **not yet decided.**
- The price-list refresh gap is closed. A scheduled job now refreshes it twice a day, every day including weekends, and pages over Telegram the moment it starts going stale rather than waiting for the hard cutoff. (Correction to an earlier note: the claim that the box had no scheduled jobs at all was a checking mistake, not a real finding — the box has always had them. The refresh gap itself was real and is now fixed.)
- The evening report's lessons and reminders now actually carry forward: they're saved and read back by tomorrow's trading decisions instead of being generated and thrown away. One of the four fields — the report grading its own prior forecast — is kept for the record but deliberately not fed back in, to avoid the same self-review loop that was cut once before.
- A smaller inconsistency is fixed: the parked cash-equivalent holding can no longer take up one of the limited slots meant for real positions' news coverage.
- Every trade's conviction, requested risk, allocated risk and which model decided it are now visible on the dashboard and through the API, not just recorded internally.
- The rehearsal tool used to test changes offline no longer depends on how stale the live price list happens to be at the moment someone runs it — that dependency mismatched what it was supposed to be testing and could produce a false failure.
- A safety-net test that was supposed to guarantee every outcome prints a plain explanation, but only ever checked itself against itself, has been rebuilt to check against the real code instead. It already found one genuine, small gap in the process (see below).
- Three previously-identical "nothing happened" outcomes inside the mid-day quick check (feature off, already running, nothing found) now report distinctly, so they can be told apart after the fact. All three are and remain harmless.
- Four pieces of dead code confirmed to have zero callers anywhere, including tests, were deleted: an unused local copy of the holdings list, a write-only bookkeeping field, a superseded internal data shape, and four small orphaned helper functions.

**Landed (2026-08-31, later) — two of the items below were fixed the same night**

- Fed meeting dates are no longer a gap. The Federal Reserve publishes its own
  meeting calendar free and the desk now reads it. A second source covers the
  years the machine-readable feed does not reach — without it the desk would
  have confidently reported "no meeting" for dates it simply could not see.
  "No meeting is scheduled" now prints only when a real schedule genuinely
  covers the window asked about; every other case says so in words.
- The alarm that tells the owner a change never reached the live server can
  now actually send. It never could: the credentials were never wired into it,
  so an alert would have gone to a log file and nobody. It has a probe that
  proves the channel still works rather than assuming it.
  **Not finished:** the accompanying scheduled jobs are written but were
  deliberately NOT switched on. They implement a weekly confirmation the owner
  rejected — a week of undetected silence is not monitoring. The replacement,
  where every trading session proves the alert path as part of its own run, was
  unfinished when this was written and has since merged — see below.

**Closed (2026-08-31, later still):**

- The mid-day failure outcome that would have shown a raw internal code now
  explains itself in plain English. The safety-net test that found it is back
  to tracking nothing, which is the state it is meant to be kept in — anything
  parked in it is a defect deferred in writing.
- The offline rehearsal tool no longer misreports how much slack the price-list
  safeguard has. **This was recorded as cosmetic and it was not.** It was
  checked before the setting was available, so it always read "none" — which
  meant the tool told the reader the desk would refuse to run any paid analysis
  in exactly the situation where the desk would in fact have run normally. The
  opposite verdict, not a wrong number. The silent guess that hid it is now a
  hard stop: asked before it can know, the tool refuses to answer rather than
  making something up.
- The scheduled job that pointed at a folder from the project's original owner
  is corrected and merged. It was never wrong on the live server — only in the
  repository's own copy, which meant installing that copy would have broken the
  daily report on contact. Confirmed against the running server, not just the
  merge: the job runs from the right folder and its last run sent the report
  as normal.
- The setup guide one internal file pointed readers at, and that never
  existed, is no longer promised. The reference was removed rather than
  writing a guide — the module it points at already documents itself in
  full, and a second document would only have restated it.
- Full wire-service news coverage is formally closed, not merely unchanged:
  the owner has declined the paid subscription it would need. Free coverage
  stays as already widened.

**Both pull requests noted here as unfinished have since merged:** every one of
the server's startup files is now under version control with an automatic daily
check that reports any difference between the server and the repository; and
the alert-path rework landed, making the trading sessions themselves the
alert-channel watchdog and retiring the weekly digest the owner rejected.

**Landed (2026-08-30, later) — two fixes plus a close call, all deployed**

- The macro data feed's retry policy has been rebuilt. The old one gave a failing economic-data series one quick second try and then gave up; that is exactly what let one bad three-minute stretch (2026-08-26) lose all nine numbers the macro seat reads, silently. It now tries harder, with a real time limit on the whole job — a minute and a half, not per series — so a slow patch can be ridden out without ever risking a session running long. Six new free indicators were added on top of what was already tracked — a real (inflation-adjusted) 10-year yield, the market's inflation expectation, a 3-month Treasury rate, the dollar's strength against other currencies, investment-grade borrowing costs, and weekly unemployment claims — each checked against the real data source before being wired in. And if any of these numbers fail to come back, the desk is now told so directly, the same way it is already told when the news feed is degraded, instead of quietly reasoning from nothing (PR #162).
- A second, smaller repair: if the mid-day opportunity scan crashed partway through, it used to look exactly like a normal quiet check that found nothing — no error, no signal, nothing for anyone to see. It now says plainly that it crashed, and the rehearsal report counts that as a failure instead of a pass (PR #163).
- A close call, caught in time: the price list the spending safeguard uses to know what each AI call costs is supposed to refresh itself, but only when a real trading session actually starts one — nothing refreshes it on a clock. Over the weekend it sat unrefreshed long enough to cross the point where the safeguard would have refused to run any paid analysis at all come Monday morning, meaning the desk would have opened and done nothing. It was noticed and refreshed by hand before that happened. (Closed: PR #168 added a systemd timer that refreshes the price list twice a day, seven days a week — see the 2026-08-31 entry above.)
- Still missing on the macro side: there is no calendar of upcoming Fed decisions or inflation reports. Asked whether one is coming up, the desk still answers from what the model remembers, not from a real schedule.

**Found (2026-08-30, documentation audit) — ten more open defects recorded, none fixed yet**

An audit raised eleven candidate defects beyond the two already tracked above; ten verified real, one turned out false. All ten are now recorded in `docs/phases.yaml`'s `open_defects` entry as items (c) through (m), ranked by how directly each touches a trading or risk decision — read that entry for the full detail and the exact re-check command for each. In order: (c) the earnings-date lookup meant to ground the risk manager's mandatory event-risk check is wired to nothing, so that check still runs on the model's memory instead of real data; (d) the evening report's discipline-notes / selection-rules / thesis-update / outlook-grading fields are generated by the LLM every night and dropped before they reach storage or any later decision, so the loop the evening report explicitly promises never closes; (e) the evening session excludes the parked cash-sweep vehicle from per-symbol news selection but the two same-day checks earlier in the day don't, so it can occupy one of the capped news slots that would otherwise go to a real position; (f) every trade's conviction / requested-risk / allocated-risk / deciding-model fields are persisted but not exposed anywhere a human can see them, dashboard or API; (g) the rehearsal harness's own test that proves it can reproduce last week's spending-limit failure is quietly coupled to the same OpenRouter pricing-cache staleness already recorded as defect (b) above, so it can fail for a reason unrelated to what it exists to test; (h) a test meant to guarantee every outcome of the mid-day quick check prints a plain explanation is not actually exhaustive — it only checks that its own checklist agrees with itself — though it does not currently fail; (i) several healthy outcomes of the intraday opportunity scan (feature off, lock contention, nothing found) are indistinguishable from each other after the fact (benign, a residual loose end from this month's crash-visibility fix, PR #163); (j)-(m) are lower-consequence dead code found in the same pass: an unused local positions table nothing in production reads, a run-context field written once and never read back, a superseded news-analysis model family kept alive only by tests, and four small helper functions with zero callers anywhere.

**Checked and found NOT to be a defect:** a claim that `docs/STATE.md` pins a specific production commit that is now several merges behind current `main`. Verified live 2026-08-30: production HEAD and `origin/main` are both `6a8694a` — zero merges of gap (`scripts/status_board.py`'s own live `undeployed_merges` reading is 0). Not recorded as a defect.

**Separately noticed while checking the above:** `docs/STATE.md`'s "Intraday opportunity discovery" section — the file was dated 2026-08-27 at the top at the time — still said the broker layer has no short-selling capability at all today, and that nothing in the codebase tells the Portfolio Manager the inverse ETFs are bearish instruments. Both were false: shorting went live 2026-08-29, and the inverse-ETF/Portfolio-Manager wiring landed 2026-08-30 (PR #158). Flagged here rather than fixed in the moment — and fixed twelve minutes later anyway, in commit `4fb02e47`, which corrected both claims in place with dated notes.

**Landed (2026-08-30 through ~15:00 UTC 2026-08-31) — all five items now deployed to production**

- All five ordered items from the 2026-08-29 backlog have shipped. (1) Inverse-ETF longs now count against the bearish exposure ceiling, with a second commit fixing a sign error: shorting an inverse ETF is bullish, not bearish (PR #158). (2) Free per-symbol news feeds are scoped to held positions and candidates instead of universally requested (PR #157). (3) Every trade carries its allocation, conviction, and deciding model pinned at entry; exits label whether they link to an originating decision (PR #159). (4) The rehearsal harness can now read the intraday scan's own outcome report instead of only the top-level status — at the time, one limitation was left in place on purpose: a crashed scan produced no marker, so the session status stayed 'ok' even on crash (production honesty gap, documented but not fixed by design) (PR #156). That gap is now closed too — see "Landed (2026-08-30, later)" above (PR #163): a crash now attaches its own status and reports as a failure. The other limitation from PR #156 still holds: the nested-outcome path is unit-tested but no production replay has actually contained an intraday scan yet (none in live history so far). (5) The desk can now formally argue out disagreements and size trade risk by the number of independent seats that agree: a target carrying an unadjudicated conflict is dropped before grounding (punishment fits offence, single-target drop not session-wide), and risk_allocation_pct is ceilinged by agreement count in the deterministic risk code (PR #160, merged during this audit window).
- To check the live state: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` should show PR #160 merged.

**Landed (2026-08-29) — read this first, supersedes most of what follows**

- Short selling is complete and live: the desk can now open a short and cover
  it, not merely hold one safely, on the same careful caps and gates a long
  trade gets.
- A tool to test a strategy change against real history now exists, though it
  can only check the mechanical trading rules, not what the AI agents
  themselves would have decided — that was never recorded, so it cannot be
  replayed.
- Any research seat, not only the chart analyst, can now bring a candidate to
  the desk's attention. Still missing: the desk formally arguing out a
  disagreement between seats, and sizing a trade bigger when more seats
  independently agree.
- The cost-circuit outage from two days ago is fully fixed, not just
  patched, and its safety backstop now recovers on its own instead of
  shutting a trading mode down for the rest of the day.
- Both defects logged below that were specific to short trades — blind
  performance stats and confused crash recovery — are verified fixed.
- The news desk's free source list was widened, and a feed that had quietly
  stopped publishing while still reporting success was found and removed.
  Real wire-service coverage still needs a paid subscription and an owner
  decision.
- All of the above is merged and confirmed deployed to production as of
  today. Most of "STILL OPEN — 2026-08-29" and "EXECUTION ORDER FOR THE NEXT
  SESSION" below is now done; see their own superseded-notices rather than
  reading them as current.

Five items, ordered by dependency then value:

1. **DONE (PR #158)** — Make the inverse funds coherent with real short selling. Since they stay, a
   long position in one is bearish exposure the short-side ceiling cannot
   currently see. That exposure now counts against the same ceiling, and the
   Portfolio Manager knows these are bearish instruments. Correction 2026-08-30:
   a second commit in the same PR fixed a sign error: shorting an inverse ETF is
   bullish (betting the underlying index rises), not bearish, and must not consume
   the bearish budget. The setting was renamed from `risk.max_short_gross_pct` to
   `risk.max_gross_bearish_pct` to reflect the widened scope.
2. **DONE (PR #157)** — Widen the free news sources further. Per-company
   coverage now exists but is scoped to the names the desk holds or is watching
   that day instead of universally, capped so it does not explode to ~100 requests.
   Yahoo per-symbol only; Seeking Alpha was verified working and deliberately not
   enabled due to cost constraints.
3. **DONE (PR #160, merged during audit)** — The unbuilt half of the research-desk work: seats formally arguing out a
   disagreement, and a name more independent seats agree on earning a larger
   share of the risk budget. A target carrying an unadjudicated conflict is now dropped
   before grounding (punishment fits offence — single-target drop, not session-wide).
   Sizing by agreement is now in the deterministic risk code (not a model instruction):
   risk_allocation_pct is ceilinged by how many independent seats are directionally aligned,
   indexed by agreement count. Default schedule is [3.0, 4.0, 5.0, 5.0, 5.0]%, keeping
   1-source trades at 60% of the 5% envelope, 2-source at 80%, 3+ at full 100%.
4. **DONE (PR #159)** — Log every trade's allocated risk against how it actually turned out, so
   conviction can be judged from data. Each trade now carries its allocated risk
   percentage, stated conviction, and deciding model pinned at entry. Exit rows
   label whether they link to an originating decision or have none. The grouping
   of outcome-by-conviction exists but is gated: below 20 per bucket it reaches
   the human operator only and is kept out of every agent prompt.
5. **DONE (PR #156, with one gap since closed)** — The intra-session scan result that never
   reaches the session report. The rehearsal harness can now read the intraday
   scan's nested outcome from its own report instead of only the top-level status.
   At the time, two limitations were recorded and not fixed by design: the path is
   unit-tested but no current production replay actually contains an intraday_scan
   key (still true — none exist in live history), and a crashed scan produced no
   marker — the session read healthy and the operator could not see the crash.
   **Correction 2026-08-30 (PR #163): the crash gap is now closed** — a crashed
   scan attaches its own status and the session reports it as a failure.

**Next, in order**

1. **Phase 9 — the research desk deliberates.** Every seat may nominate a
   candidate; Technical becomes a responder rather than the gatekeeper on
   candidacy; material disagreements must be adjudicated, not just logged;
   conviction follows multi-source agreement. Full design in
   `docs/QAMC_REMEDIATION_SPEC.md` Phase 9. Depended on Phase 2b — now
   committed (`75c0233`, `feat/pm-flex-routing`) — because "agreement earns
   size" is meaningless until size is expressed as risk.

3. **Earnings filing extraction fix — PR #115 (`009ab78`, branch
   `feat/shorts-visible`, misnamed — it carries the earnings fix, not
   shorting), merged into `main`.** Deploy status is not tracked here — check
   `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` against
   `git log origin/main` to see whether it has shipped yet. Corrects the diagnosis
   previously recorded here, which was wrong: the class is
   `EarningsDataProvider` (`src/data/earnings.py`), not `EarningsProvider`,
   and it was never doing a naive first-30,000-characters slice — structured
   section extraction and a density-seeking fallback both already existed.
   The real defect: `_extract_key_sections` matches the phrase "financial
   statements", which also appears verbatim inside the auditor's opinion
   letter ("...the related notes (collectively referred to as the financial
   statements)"). The acceptance test measured only LENGTH (≥3,000 chars), so
   that prose comfortably cleared the bar and suppressed the density-seeking
   fallback that would have found the real tables. Measured over the 68
   filings cached on the production box: 17 reached the earnings analyst
   starved (<40 financial figures), 12 of those with ZERO — MSFT, AAPL,
   GOOGL, BAC, CVX, NFLX among them. Fix: require ≥40 financial figures
   (dollar amounts, comma-grouped thousands, parenthesized negatives — the
   same pattern `_find_financial_dense_region` already scored by, now a
   shared module constant) in addition to length; failing the content check
   falls through to the fallback instead of returning. Re-measured across all
   68: 17 improved, 51 unchanged, 0 regressed, 0 starved.

4. **Insider routine/opportunistic filter — PR #133 opened against `main`, not yet
   merged/deployed.** See the "Landed" entry above (`feat/insider-signal-filter`)
   for what it does and the measured routine split with its caveat.

6. **Phase 4.2 — repair the data feeds.** News-feed half **DONE** (branch
   `fix/news-feeds-and-coverage`, 2026-08-28): Reuters/AP investigated live —
   neither is fixable for free (Reuters retired public RSS in 2020; AP's own
   feed requires a paid OAuth2 API, and the free third-party proxy is
   Cloudflare-walled) — both removed from `RSS_FEEDS`, Yahoo Finance News
   added as a partial free substitute, and `NewsCoverage` (`src/data/news.py`)
   now makes a dead feed impossible to miss: it's in the analyst's own prompt
   and in `data_status["news"]` (`ok`/`partial`/`failed`), which is what
   `trader_feed.py`/`notifier.py` already render as the operator-facing
   `⚠️ Data degraded` banner. **FRED half also DONE (2026-08-30, PR #162)** —
   see the "Landed (2026-08-30, later)" entry above; this line was left
   "still open" for two days after that stopped being true.
   (4.1, un-blindfolding the intraday buy path, is done — `fb88e08`,
   `feat/pm-flex-routing`, see the landed section above.)

7. **Phase 5 — short selling, now a three-stage plan.** The prior estimate
   recorded here — "bounded and additive, roughly a day, NOT a rewrite" — was
   **wrong**. A survey for Stage 1 found roughly 50 long-only assumptions
   across the money path, several failing silently: the constructor would
   re-open a held short every session (a short's weight was absent from
   `_current_weights`, so `.get(sym, 0.0)` read an already-held short as
   unheld); short orders bypass the risk engine entirely via an early
   `return []` on SELL; and shorts counted as zero portfolio risk in
   `portfolio_heat` (`qty <= 0` was excluded). Discovery still ALREADY WORKS —
   `TechAnalysisResult.rating` emits `sell` / `strong_sell` today. **Inverse
   ETFs are explicitly NOT the answer** — the owner rejected that workaround;
   he wants real short selling. Split into:

   1. **Make shorts countable — PR #116 (`feat/shorts-countable`, two commits
      `71325b1` + `a81bfde`), merged into `main`.** Deploy status is not
      tracked here — check `sudo -n -u qamc git -C /home/qamc/quant-agent log
      --oneline -1` against `git log origin/main`. Signed weights
      in `_current_weights`, side-aware `r_multiple`/`position_risk`/
      `portfolio_heat` in `src/risk/metrics.py`, and `qty != 0` (not `qty >
      0`) in every reporting filter (`src/storage/db.py`,
      `src/notifier.py`, `src/trader_feed.py`, `src/pipeline.py`). No order
      path is touched — the constructor emits nothing for a held short
      (`current_pct < 0`) rather than routing a cover or an add-to-short
      through paths that don't yet handle direction. 40 tests added
      (`tests/test_shorts_countable.py`), 21 of which failed pre-merge on
      `main` without this fix; the rest are a no-op wall proving long
      arithmetic is unperturbed.

   2. **Make shorts safe — landed 2026-08-28 (commit `10e0f10`, "Stage 2 of
      short selling: make shorts safe"; `tests/test_shorts_safe.py`, 28
      tests), merged to `main`.** Correction 2026-08-29: this line
      previously read "(not yet started)" — that was wrong as of today's
      check against origin/main; stage 2 is done, stage 3 below is what
      remains. Risk-engine routing so a SELL on an unheld symbol doesn't
      skip the deterministic gate via the early `return []`; stop direction
      (above entry) and trailing direction inverted for shorts; unbounded-loss
      margin accounting.

   3. **Turn it on (not yet started).** Order placement in the broker layer,
      then retire the inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`) as the
      bearish-expression mechanism. **Correction 2026-08-29: the first half
      is done — stage 3 (PR #150) landed and is merged and deployed, so
      shorts can be opened and covered.** The inverse-ETF retirement is
      still outstanding: `SH`/`SDS`/`PSQ`/`SQQQ` remain in the trading
      universe, now redundant rather than necessary.
   Alpaca is ready: `shorting_enabled: true`, `no_shorting: false`,
   `max_margin_multiplier: 4`, assets `shortable` with `borrow_status:
   easy_to_borrow`. The Alpaca paper account was verified on 2026-08-28 as
   already margin-enabled (`shorting_enabled: True`, `multiplier: 4`, equity
   $9,871.87) — no owner action is outstanding. (`docs/QAMC_REMEDIATION_SPEC.md`
   Phase 5 previously recorded an owner action to switch the account to
   margin; that is stale and has been corrected there.)

   **Known residual, needs a schema migration.** `pending_protection_restores`
   WAL rows predate shorts and carry no side column, so a row for a short's
   cancelled BUY stops looks byte-identical to one for a long's cancelled
   SELL stops. `_derive_close_side_for_drain` (`src/pipeline.py`) reads the
   broker's live signed position to tell them apart, but when that read
   itself fails, `_drain_pending_protection_restores` deliberately degrades
   to the pre-existing `sell` default rather than stalling the row —
   verified in the code and comments as of 2026-08-29. Unreachable today
   because shorts cannot yet be opened; becomes reachable, and wrong, the
   day they can. Close it as part of stage 3, not after. Current behaviour
   is pinned by `tests/test_shorts_emergency_close.py`'s crash-recovery
   drain-path coverage (added `e9851ea`, flagged by PR #135's own coverage
   audit as the one corner with zero tests) — read it before changing it.
   **Correction 2026-08-29: closed as part of stage 3, as planned.**
   `pending_protection_restores` now persists a `side` column, written at
   row-creation time by whoever is closing the position; the drain path
   prefers that persisted value and only falls back to the live-broker
   derivation (never a blind `sell` default) for a legacy row written before
   the migration. See `tests/test_wal_protection_side.py`.

8. **Phase 6 — cost circuit and transparency.** Dollar-based cap with an
   afternoon reserve; `position_id` linking a buy to the sell that closed it;
   surface the reasoning already stored but never displayed. **Correction
   2026-08-29: done** — see "Landed (2026-08-29)" at the top of this backlog.

#### THE REHEARSAL HARNESS — built, acceptance test PASSING (corrected 2026-08-29)
Merged to `main` as PR #122 (`feat/session-rehearsal`). Runs a full session offline against a snapshot of production, replaying recorded model responses. Free, deterministic, about 50 seconds. Blocks outbound network at the process level and proves the production database is byte-identical afterwards. Operator alerts are suppressed via `QAMC_REHEARSAL=1`.

**This section previously said the acceptance test did not pass. That was stale by the time it was read on 2026-08-29** — the fix landed the same day it was written, inside the same PR (`ee6f671`, "un-merge chunked agent rows so replay stops running dry"), and nothing after that commit ever came back to correct this text. Verified again on 2026-08-29 by re-running both acceptance tests from a fresh worktree against the live production snapshot: `pytest tests/test_rehearsal_replay.py tests/test_rehearsal_reproduces_cost_ceiling.py` — **11 passed**.

What was wrong and the fix: `tech_analyst.analyze_batch` auto-chunks a large symbol batch into several real provider calls (3 chunks + 1 missing-symbol recovery for the 2026-08-28 incident run, `agent_logs.provider_requests = 4`), then merges them into ONE `agent_logs` row before logging. Replay patches the provider transport, invoked once per real call, so it needed 4 recorded answers for that row and found 1 — the first chunk consumed it, every later chunk raised `MissingRecordedResponse`, and the resulting failure cascade masked the actual incident behind an unrelated `failed_call_unknown_cost` trip on `tech_analyst`. Fix (`ops/rehearsal/replay.py::_unmerge_chunked_call`): `analyze_batch` already joins each real call's text behind `"--- chunk i/N ---"` / `"--- missing-symbol recovery ---"` markers, in call order, in both `input_message` and `full_response` — a complete ordered record of the real calls a merged row represents. Replay now splits one row back into one `RecordedCall` per real call before matching, prorating merged-only token/cost figures by each part's share of the row text (last part takes the remainder, so parts always sum to exactly the recorded total).

With the chunk defect fixed, the harness reproduces the 2026-08-28 incident timeline exactly (`llm_circuit_events` on the live box: 09:32 defect 1 — projected session cost, `portfolio_manager`, session $0.0461 / day $0.0476; 11:15 operator reset; 11:30 defect 4 — paid-session count cap, `tech_analyst`, day $0.1765). `test_the_pre_fix_estimator_still_reproduces_the_2026_08_28_block` forces the old byte-as-a-token estimator back on through config (demanding more measured history than the ledger holds) and confirms the reserved-exposure ceiling still blocks the Portfolio Manager exactly as it did that morning, zero trades proposed or executed. `test_rehearsal_reproduces_2026_08_28_pm_cost_ceiling_failure` runs the same incident under the four cost-circuit fixes (PR #126, merged same day) and confirms the ceiling no longer fires and the Portfolio Manager is reached — the correct post-fix outcome. Both tests require `sudo -n -u qamc` read access to the production database and skip cleanly where that access is unavailable.

**Two more rig-only defects found 2026-08-29 by actually running the harness (not just its acceptance test) across `morning`/`midday`/`close`/`evening`/`intra_check` against the live snapshot, both fixed in the same pass:**

1. **`ResponseLibrary.match()` could crash on an ordinary, unpinned rehearsal.** Reproduced live: a plain `morning` rehearsal (no incident pinning, matching against full history) hit `TypeError: '<' not supported between instances of 'RecordedCall' and 'RecordedCall'` inside `scored.sort(reverse=True)`. Cause: `_unmerge_chunked_call` gives every part of one merged `agent_logs` row the same `row_id`, so two un-merged parts of one chunked row tie exactly on `(score, -row_id)` whenever they also tie on Jaccard score (trivially true when neither shares a word with the live prompt), forcing Python to compare the un-orderable `RecordedCall` objects to break the tie. In production this cascaded: tech_analyst's retry logic caught it as a call failure, exhausted retries, failed over to a second provider, hit the identical crash on the identical tied candidates, burned through the cost circuit's `provider_attempt_limit`, and suspended paid analysis for the rest of the session — a rig-only bug that looked exactly like a production incident. Fixed in `ops/rehearsal/replay.py` by ranking candidates by index instead of by object; `tests/test_rehearsal_replay.py::test_match_does_not_crash_when_two_unmerged_parts_of_one_row_tie` pins it.
2. **The verdict didn't know its own pipeline's status vocabulary.** `midday`, `close` and `intra_check` rehearsals that ran perfectly normally — no crash, no missing recording, no blocked agent — all came back `VERDICT: FAIL`, because `_verdict`'s healthy-status set only recognized `executed`/`no_orders`/`no_trades`/`market_holiday`. `run_position_review` (shared by midday/close) returns `"reviewed"` on a normal completion, `run_intra_check` returns `"ok"` when there is no loss violation (the common case on a 30-minute cadence), and `run_evening` returns `"analyzed"`. Production's own `src/trader_feed.py` and `src/notifier.py` already group these with the statuses the rig did recognize as healthy — the rig disagreeing with production about what counts as "this worked" is exactly the dishonest-output failure mode this harness exists to catch in the trading system, reproduced in the harness itself. Fixed in `ops/rehearsal/report.py`; `tests/test_rehearsal_report_verdict.py` pins it (new file, 6 tests).

Full suite: 2892 tests passed before this pass; +7 net new (1 in `test_rehearsal_replay.py`, 6 in new `tests/test_rehearsal_report_verdict.py`). Now lives at `ops/rehearsal/` on `origin/main`, not on a standalone branch/worktree; see "Session start" above for the owner's 2026-08-29 instruction to run it routinely.

**Hardening pass 2026-08-29 (second): verified the five just-added healthy statuses against `src/pipeline.py` directly rather than trusting the comments above, and audited every `run_*` session function for other gaps.** Found and fixed three more:

1. Three genuine *failure* statuses — `position_review_parse_error` (`run_position_review`/midday+close), `evening_analysis_error` and `evening_parse_error` (`run_evening`) — were already asserted as FAIL by this pass's own tests but had no `STATUS_PLAIN` entry at all, so each would have printed the generic "ended with status 'X'" fallback instead of a real explanation. Added.
2. `early_close` (`run_position_review`/midday+close, `src/pipeline.py:7806`) — a deliberate skip on half-day-holiday sessions, the same shape as `market_holiday` — was missing from both `STATUS_PLAIN` and the healthy set. Added to both.
3. `run_morning`'s PM-failure family — `pm_parse_error`, `pm_schema_error`, `pm_grounding_error`, `pm_repair_changed_decision` (`src/agents/portfolio_manager.py`, surfaced via `ctx.analysis_failure_status`) — were real, reachable statuses with no `STATUS_PLAIN` entry. Production's own `src/notifier.py`/`src/trader_feed.py` already match on `status.startswith("pm_")` as a PM-decision failure; the rig's vocabulary had not caught up. Added as failures (not healthy).

Also added `run_earnings_preprocess`'s statuses (`fetch_error`, `nothing_new`, `analysis_error`, `preprocessed`) pre-emptively — that session is real and scheduled but the rig still cannot invoke it (unchanged, separate gap, see below) — so the vocabulary is already correct on the day that gap closes.

One nuance worth recording: `intraday_no_trades`/`intraday_executed` (from the first hardening pass above) are correct in meaning but were found to be currently **unreachable** as `report.status` — they only ever appear nested at `result["intraday_scan"]["status"]` (`src/pipeline.py:8497-8498`), which `ops/rehearsal/report.py`'s `collect()` never reads; production's own `src/trader_feed.py` reads that nesting explicitly (`nested = result.get("intraday_scan")`, line 54) rather than trusting `result["status"]` for intra_check. Left in `STATUS_PLAIN`/the healthy set (harmless, correct-if-ever-reached) but the rig having no visibility into the intraday scan's own outcome is a real, separate gap — reported, not fixed here.

New guard test `tests/test_rehearsal_report_verdict.py::test_every_known_pipeline_terminal_status_is_classified` pins the full status vocabulary against a hardcoded, file:line-cited list (dynamic AST discovery was tried and rejected — the PM-failure family lives on `AgentResult.semantic_status`, set in a different file, not a string literal at the `"status"` key's return site, so a literal-string walk would silently miss exactly the drift this test exists to catch) — a future undocumented pipeline status now fails CI instead of printing raw.

Full suite: 2900 passed (2899 after the first hardening pass + this test).

#### BRANCHES READY, NO PR YET

- `feat/insider-signal-filter` — merged as PR #133, no longer pending
- `fix/news-feeds-and-coverage` — merged as PR #132

- `fix/dollar-based-session-cap` — its first commit, `766a35d`, added
  `afternoon_reserve_pct` (40) and `afternoon_reserve_release_et_hour` (12)
  plus a `_morning_spend_ceiling()` helper that was defined and never
  called. **Correction, 2026-08-29: superseded, not still open.**
  `_morning_spend_ceiling()` is called from `begin_call` in current `main`
  (`src/cost_circuit.py`), with dedicated passing tests
  (`tests/test_cost_circuit.py::test_morning_spend_ceiling_pure_computation`,
  `test_afternoon_reserve_blocks_morning_spend_above_the_ceiling`,
  `test_afternoon_reserve_recovers_the_same_day_without_a_rollover`) —
  landed via PR #126 (`fix/cost-circuit-four`) and PR #131
  (`fix/pricing-staleness`), both already merged. See "STILL OPEN —
  2026-08-29" item 5 below.

- `feat/bounded-repeg` — PR #144 opened 2026-08-29. Agent decision: merge it
  rather than leave it to rot, shipping the re-peg disabled by default. Check
  `gh pr list` for current status before treating this as landed.

- `MarketDataProvider.get_next_earnings_date()` is implemented but **unwired**;
  the Tech Analyst accepts a `days_to_earnings` kwarg that nothing supplies.
  **Correction: no longer true.** A real caller now exists
  (`src/data/event_calendar.py`, submitted through a bounded
  `ThreadPoolExecutor`), landed as part of closing defect (c) in
  `docs/phases.yaml`'s `open_defects` entry.

- Nothing tells the Portfolio Manager that `SH`, `SDS`, `PSQ` and `SQQQ` are
  bearish instruments, so even the sanctioned bearish expression is unwired.
  **Correction: no longer true.** `config/prompts/portfolio_manager.md` now
  carries a dedicated "Inverse ETFs are bearish, not a hedge-flavoured long"
  section (PR #158; also confirmed in `config/prompts/risk_manager.md`'s
  "Short discipline" section).

- 26 unmerged branches await triage, including two abandoned VPS security
  branches (`claude/vps-security-hardening-t8m3qz`,
  `claude/vps-deployment-hardening-q3f7k2`) worth rescuing before deletion.
