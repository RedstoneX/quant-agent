# Model Routing Policy — Accepted Contract

Status: **accepted — externally reviewed and merged via PR #30 on 2026-08-14**.

Supersedes the commissioning baseline recorded in `docs/STATE.md` ("every
agent on `openai/gpt-5.5`"), which was a deliberate single-variable posture
for proving the OpenRouter transport, never the intended production policy.

Read alongside `MODEL_PROVIDER_ARCHITECTURE.md`, which owns the seam this
policy is expressed through and is unchanged by it.

## The policy

Every agent seat runs `provider: openrouter`. The Portfolio Manager runs
**`openai/gpt-5.5`**, the AI Risk Manager runs
**`qwen/qwen3-235b-a22b-2507`**, and the remaining seats run
**`google/gemini-2.5-flash-lite`**.

| Seat | Model | Basis |
|---|---|---|
| `tech_analyst` | `google/gemini-2.5-flash-lite` | measured — `tech_batch`, `tech_batch_full` |
| `news_analyst` | `google/gemini-2.5-flash-lite` | measured — `news_intel` |
| `macro_analyst` | `google/gemini-2.5-flash-lite` | measured — `macro_stress` |
| `portfolio_manager` | **`openai/gpt-5.5`** | measured 2026-08-25 — `pm_constrained`, `pm_production_scale` |
| `risk_manager` | **`qwen/qwen3-235b-a22b-2507`** | measured — `risk_rr_breach`, `risk_drawdown_discipline`; **held apart from PM** |
| `position_reviewer` | `google/gemini-2.5-flash-lite` | measured — `midday_exit` |
| `earnings_analyst` | `google/gemini-2.5-flash-lite` | **by analogy** to `news_analyst` |
| `evening_analyst` | `google/gemini-2.5-flash-lite` | **by analogy** to `portfolio_manager` |
| `meta_reflector` | `google/gemini-2.5-flash-lite` | **by analogy** to `portfolio_manager` |

### Cost is not a quality signal — and was never used as one

Worth stating because a test in this repo briefly implied otherwise (an
input-price >= $0.10/M floor on the decision seats, removed at PR #30
review). Nothing in this policy infers quality from price. Every **decision
seat** assignment traces to a graded run at that seat, and the invariant the
tests now enforce is exactly that: a decision seat's model must carry a
committed `quality_min` of 1.00 at its own scenario. The three seats marked
**by analogy** above are explicit exceptions and are not represented as
directly measured. The price floor would have failed this policy — both
routed models sit at or below it — while passing any expensive model nobody
had measured.

### PM recovery re-measurement (2026-08-25)

The original PM scenario did not enforce grounded specialist provenance or
actual holdings. Once those deterministic checks were added, the configured
Gemini PM scored **0.00 on both runs**: it omitted provenance and proposed
exits for names not held. GPT-5.6 Luna was not stable under the final stricter
contract. GPT-5.5 scored **1.00/1.00 on two `pm_constrained` runs and two
`pm_production_scale` runs** (30 candidates, 15 holdings, full memory context;
43–109s, including bounded provenance-only repairs). Raw final evidence is
committed in `ops/model_policy/results/zz-pm-luna-disqualified-final-2026-08-25.json`
and `ops/model_policy/results/zz-pm-gpt55-qualified-final-2026-08-25.json`.

This changes only the PM seat. Risk routing and deterministic Python/broker
authority are unchanged.

### Shared specialist model; decision seats diverge where measured

The tranche reserves a different model for a seat only where current
measurements demonstrate the benefit. The PM now does; the other seat
assignments retain their prior evidence.
Three unmeasured seats are assigned by analogy and remain an explicit known
limitation rather than evidence of equivalence.

`risk_manager` retains its independently measured Qwen route. See
"Why `risk_manager` is not on PM's model" below.

The per-seat *structure* is what made that a one-line config edit rather
than a plumbing change, and it is what `verify_commissioning.py` pins
against.

## Shape

Explicit per-agent model mapping in `config/settings.yaml`. Nothing else.

- No router, no scoring service, no dynamic selection, no new dependency.
  The mapping is YAML read by machinery that already existed.
- No silent fallback — see "Failure behaviour", where the decision *not* to
  add one is part of this contract.
- Attribution needed no work: `AgentResult` already carries
  `requested_model` / `requested_provider` / `model` / `actual_provider` /
  `used_fallback`, and every `insert_agent_log` call site persists the
  model that actually answered. The Stage 1 seam was already built for
  per-agent divergence.
- `provider` stays explicit on every seat. OpenRouter's `vendor/model` ids
  collide with native prefixes, so `resolve_provider()` cannot infer them;
  an unset provider routes to Anthropic and fails on the session's first
  call.

## Evidence

`ops/model_policy/benchmark_models.py`, 148 graded trials on 2026-08-12:
12 models x 6 scenarios x 2 repeats, plus production-scale tech and PM cases and
two corrective re-runs, driven through the **real** agent classes with the
**real** prompts and graded by deterministic assertions. Raw results in
`ops/model_policy/results/`; reproduce the table with:

```
python ops/model_policy/benchmark_models.py --report \
  ops/model_policy/results/sweep-a.json \
  ops/model_policy/results/sweep-b.json \
  ops/model_policy/results/rerun-capfix-flash.json \
  ops/model_policy/results/rerun-capfix-pro.json \
  ops/model_policy/results/sweep-tech-full.json \
  ops/model_policy/results/rm-rerun-2026-08-14.json
```

Quality is the weighted mean of a scenario's graded checks (0–1).

The table below is the **whole-sweep** aggregate across six roles. Read it
as what it is: a summary of general fitness, useful for choosing one model
for the originally shared seats. It is **not** evidence about any individual seat, and
treating it as such is the error PR #30's review caught — see "Why
`risk_manager` is not on PM's model".

| model | mean | worst run | $/sweep |
|---|---|---|---|
| **`google/gemini-2.5-flash-lite`** | **1.00** | **1.00** | **$0.0152** † |
| `deepseek/deepseek-v4-pro-0813` | 1.00 | 1.00 | $0.0646 |
| `openai/gpt-5.5` *(baseline)* | 1.00 | 1.00 | $0.6547 |
| `z-ai/glm-5.2` | 0.97 | 0.65 | $0.0605 |
| `qwen/qwen3.7-max` | 0.97 | 0.65 | $0.2029 |
| `qwen/qwen3-235b-a22b-2507` | 0.95 | 0.85 | $0.0064 |
| `deepseek/deepseek-v4-flash-0731` | 0.91 | 0.45 | $0.0193 † |
| `qwen/qwen3.7-plus` | 0.85 | 0.20 | $0.0760 |
| `openai/gpt-5.6-luna` | 0.80 | 0.00 | $0.0102 |
| `minimax/minimax-m3` | 0.78 | 0.00 | $0.0492 |
| `qwen/qwen3.7-flash` | 0.74 | 0.00 | $0.0055 |
| `openai/gpt-5-nano` | 0.63 | 0.00 | $0.0242 |

† These two rows include the expensive 25-symbol `tech_batch_full`
scenario, which the other ten did not run. On the common six scenarios
`gemini-2.5-flash-lite` cost **$0.0079** — the figure comparable to the
baseline's $0.6547, i.e. **83x cheaper**.

Three models scored a perfect mean **and** a perfect worst run across all
six roles. For the eight seats that share a model, the choice between them
was made on latency and cost, both of which favour the selected model
decisively — see the next section. (The RM seat was decided separately and
on its own measurements; a model can be imperfect across six roles and
flawless at one.)

**`worst run` is the column that matters.** A mean hides the failure mode
that actually hurts: a model that alternates between excellent and
unparseable averages respectably and silences a session every other day.
Four of twelve candidates have a 0.00 in there — a run that produced
nothing the pipeline could use.

### Why not `deepseek-v4-pro-0813`, which also scored 1.00/1.00

Latency, and cost — **at the seats that issue many calls**. It answered in
104–246s per call across the scenarios, against 3–22s for the selected
model. `tech_analyst` alone issues five sequential calls per morning
against a 1200s session kill (see below), and PM and RM still have to run
after them. It is also 4x more expensive on the common scenarios.

That reasoning does **not** transfer to `risk_manager`, which issues
exactly one call per session — see the next section, where it was
re-evaluated on its own terms.

## Why `risk_manager` is not on PM's model

### The claim this replaces was wrong

Earlier revisions of this document said splitting PM and RM "would trade
measured quality for hypothetical independence — every alternative scored
worse at the RM seat". **That was false**, and PR #30's review caught it.
It read the whole-sweep aggregate as if it were the seat's result. The
committed 2026-08-12 raw results say otherwise: at `risk_rr_breach`,
**10 of 12 candidates scored 1.00 on both runs**. Only `gpt-5-nano`
(0.00/0.00) and `gpt-5.6-luna` (0.00/1.00) failed. There was never a
quality argument against splitting; there was a quality argument against
those two models.

A model that is mediocre *across six roles* can be perfect at *one*, and
the RM seat only ever runs RM. Aggregating across seats and then reasoning
about a single seat is the error.

### Re-measured on the current branch

Those results also predated the 2026-08-13 prompt cleanup and this branch's
F5/F6 changes to RM's prompt and inputs, so they were stale as well as
misread. Re-run 2026-08-14 — 5 models x 2 RM scenarios x 3 repeats, 30
trials, $0.157:

| model | quality mean | worst run | latency | cost / 2 calls | independent of PM |
|---|---|---|---|---|---|
| **`qwen/qwen3-235b-a22b-2507`** | **1.00** | **1.00** | **10.4s** | **$0.00162** | **yes** |
| `google/gemini-2.5-flash-lite` | 1.00 | 1.00 | 2.8s | $0.00163 | no — PM model at measurement time |
| `z-ai/glm-5.2` | 1.00 | 1.00 | 61.4s | $0.02355 | yes |
| `deepseek/deepseek-v4-pro-0813` | 1.00 | 1.00 | 113.0s | $0.02035 | yes |
| `openai/gpt-5-nano` *(control)* | 0.50 | 0.00 | 42.0s | $0.00517 | — |

```
python ops/model_policy/benchmark_models.py --report \
  ops/model_policy/results/rm-rerun-2026-08-14.json
```

`gpt-5-nano` was included deliberately as a weak control, and it failed the
same way it failed in August — omitting `RiskVerdict.reasoning`, so
`review()` returns `None` and the session has no risk verdict at all. A
four-way 1.00 tie is only meaningful if the scenarios can still produce a
0.00, and on the current branch they can.

### How the tie was broken

Quality first, then independence, then latency and cost:

1. **Quality** — four-way tie at 1.00 mean and 1.00 worst run. No candidate
   is better at this seat, so nothing is given up by choosing on another
   axis.
2. **Independence** — eliminates `gemini-2.5-flash-lite`, which is PM's
   model. The decision chain exists so that RM checks PM; running both on
   one model means a flaw in PM's reasoning is one the gate is
   systematically likely to reproduce. Three candidates remain.
3. **Latency and cost** — `qwen3-235b-a22b-2507` wins decisively among
   them: 10.4s against 61.4s and 113.0s, at roughly 1/14th the cost.

**Independence here is effectively free.** $0.00162 versus gemini's
$0.00163 per two RM calls — the input rate is actually *lower* ($0.09/M vs
$0.10/M) — and +7.6s on a session bounded at 1200s in which this seat makes
exactly one call. The projected monthly total is unchanged at the reported
precision.

### What this is not

Not a general promotion of `qwen3-235b-a22b-2507`. It scored 0.95 mean /
0.85 worst across the full six-scenario sweep and is measured **only at
this seat**. Moving it anywhere else needs its own measurement, which
`tests/test_model_routing_policy.py` now enforces for every decision seat.

Not a claim that RM will catch more. Independence removes a correlated
failure mode; it does not demonstrate a better verdict. That is a
paper-trading question — see `DECISION_CHAIN_AUDIT.md` (F5).

Not a change to what RM may DO. Veto authority, the modification hierarchy,
and every deterministic risk and execution semantic are untouched.

### What the failures actually were

Not stylistic. `gpt-5-nano` omitted `RiskVerdict.reasoning` entirely, on
both runs, so the AI Risk Manager returns `None`. `qwen3.7-flash` emitted
`market_sentiment: "neutral_to_defensive"` — plausible prose, outside the
`Literal`, and the whole news report is discarded before PM sees it.
`minimax-m3` exceeded the trial deadline on `pm_constrained`.

This is why the harness drives the real agent classes. Every one of these
survives a "does it write good JSON" review and fails in production.

### Latency decided `tech_analyst`, not quality or cost

Sessions are wrapped in `timeout --kill-after=30 1200`
(`scripts/run_if_et_window.sh:225`). `tech_analyst` issues **five
sequential** calls per morning (101 symbols at `_CHUNK_SIZE = 25`), and
`pipeline_stages.py` fans macro/news/tech/earnings across four threads —
so that chain is the morning's longest pole, with PM and RM still to run
after it.

`tech_batch_full` measures one real 25-symbol chunk:

| model | quality | cost/chunk | latency/chunk | x5 chunks | fits 1200s? |
|---|---|---|---|---|---|
| `google/gemini-2.5-flash-lite` | 1.00 | $0.0072 | **22s** | ~110s | yes |
| `deepseek/deepseek-v4-flash-0731` | 1.00 | $0.0084 | **294s**, then a >420s timeout | ~1470s | **no** |
| `openai/gpt-5.5` | not measurable — see below | | | | |

Both produce perfect output at production scale. They differ by 13x in
latency, and that alone settles the seat. The 3-symbol `tech_batch` had
`deepseek-v4-flash` at parity; only the real chunk size exposed it.

## Expected cost reduction

`python ops/model_policy/project_session_cost.py` (re-derivable, and
explicit about which inputs are measured and which are structural):

| | baseline (all `gpt-5.5`) | policy | cut |
|---|---|---|---|
| per trading day | $3.4334 | $0.2707 | 92.1% |
| per month (21 days) | **$72.10** | **$5.68** | **12.7x cheaper** |

`tech_analyst` is over half of it: five calls a day at 33,328 input tokens

> **Stale as of 2026-08-27.** The Tech Analyst prompt grew when structural
> levels (`src/data/levels.py`) and market context (`src/data/context.py`) were
> added and the raw-bar window went from 20 to 40 sessions. Measured on real
> five-year data, the per-symbol payload rose from roughly 480 to roughly 1,030
> tokens — about 2.2x — so a 30-symbol call is now in the region of 50,000
> input tokens rather than 33,328. At the gemini-2.5-flash-lite input rate that
> is approximately $0.005 per call instead of $0.003: immaterial against the
> $1.50 daily circuit limit. The figure above has NOT been re-derived with
> `ops/model_policy/project_session_cost.py`; the numbers in this note are an
> estimate from measured block sizes, not a benchmark run.
each, measured at real chunk size rather than extrapolated.

Treat the ratio as the load-bearing number. It is dominated by published
per-token rates, which are exact, and by a token profile applied
identically to both sides. The absolute dollars carry a real error bar:
QAMC has run no live sessions, so there is no `agent_logs` history to
average, and three seats' token profiles are structural estimates.

## An operational fault this exposed

**At the time of the commissioning-baseline check, QAMC could not have
completed a single `gpt-5.5` agent call at the configured ceiling.**

OpenRouter pre-authorizes worst-case output spend before starting a
request. At `max_tokens: 128000`, `gpt-5.5` reserves
`128000 x $30/M = $3.84` per call. The account then held **$2.04**
(`/api/v1/credits`: $10 granted, $7.96 used). Every call therefore returned
HTTP 402 — which `_is_retryable` correctly classifies as non-retryable, so
`_execute` failed immediately, and with no Anthropic key there was no
failover. The first agent of the first session would have failed closed.

Commissioning did not catch this because the preflight calls with
`max_tokens=512`, reserving about a cent.

The account was subsequently topped up; this is **not a current credit
blocker**. `docs/WORK.md` records the latest observed balance during the
review cycle. The finding remains relevant because it explains why the
baseline was operationally unusable at that moment and why preflight must
not be mistaken for affordability at production token ceilings.

The policy also reduces the reservation by model. Same 128k ceiling:

| model | reserved per call |
|---|---|
| `openai/gpt-5.5` | $3.8400 |
| `google/gemini-2.5-flash-lite` | $0.0512 |

That is a 75x reduction in credit reserved per call, independent of tokens
actually spent.

## Failure behaviour — and why no fallback was added

`BaseAgent._execute` retries with jittered backoff under a 480s deadline,
then attempts ONE cross-provider failover to Anthropic
(`_FALLBACK_MODEL = claude-opus-4-7`) **only if an Anthropic key exists**.
QAMC holds four credentials in OneCLI — OpenRouter, Alpaca key/secret,
FRED — and no Anthropic key. So the failover is disabled and the primary
error is re-raised.

**That is correct, and this tranche deliberately leaves it alone.** The
authorization permitted bounded escalation/fallback; adding one was
rejected because:

- a same-provider fallback answers an OpenRouter outage with another
  OpenRouter call — no independence, no benefit;
- a cross-model fallback would let a session's decisions come from a model
  the policy never selected for that seat, which is exactly the
  "unrecorded model choice" the authorization forbids;
- failing closed is the accepted deterministic-safety posture, and cost
  optimization is not a reason to soften it.

If continuity through an OpenRouter outage is wanted later, the honest
form is a second *provider*, not a second model — a separate architectural
decision with its own credential and review.

## Cost telemetry (fixed as part of this tranche)

The commissioned baseline had **no working cost telemetry**.
`src/cost_table.py` resolved prices from LiteLLM, which keys models by bare
vendor id (`gpt-5.5`); the configured OpenRouter id (`openai/gpt-5.5`)
matched nothing, `estimate_cost` returned `None`, every call persisted
`cost_usd = NULL`, and `notifier._session_cost_line` renders exactly
`"cost: $?.?? (N calls — see cost_table.py)"` in that state — against
`docs/OUTCOME.md`'s requirement that the operator see cost per call.

Three changes, all inside the existing pricing module:

1. `_PRICING_OPENROUTER` — OpenRouter's own rate for every model the policy
   routes to, so cost resolves offline and never depends on a mid-session
   network call.
2. An on-demand OpenRouter catalog resolver for ids outside that table.
   Cached on the same 24h discipline; a stale entry loses to a live fetch
   and survives only as a logged last resort.
3. `ops/model_policy/verify_pricing.py`, which re-reads the live catalog and
   exits non-zero on drift — a hand-copied rate that goes stale silently
   produces a confident wrong number, worse than `$?.??`.

LiteLLM remains the source for native vendor ids. It is not used for
OpenRouter-routed traffic, because its rates are the vendor's *direct*
prices.

## What holds this in place

| Check | Guards |
|---|---|
| `tests/test_model_routing_policy.py` | every seat explicit, every model priceable offline, **every decision seat's model carries a committed `quality_min` of 1.00 at its own scenario**, the risk seat additionally measured on `risk_drawdown_discipline`, policy materially cheaper than baseline, fail-closed with no model substitution |
| `verify_commissioning.py` (`config`) | the DEPLOYED config matches the reviewed per-seat map — the expected map is a separate copy on purpose, so editing `settings.yaml` on the runtime host fails the check |
| `verify_commissioning.py` (`preflight`) | every distinct policy model is in the catalog AND completes a real call; WARNs if OpenRouter serves a different model than requested |
| `ops/model_policy/verify_pricing.py` | pinned rates still match the live catalog |
| `ops/model_policy/benchmark_models.py` | re-derives the whole decision from scratch |

## Known limitations

1. **Shared specialist concentration.** Seven of nine seats run one model.
   `portfolio_manager` and `risk_manager` are independently measured routes, which
   addresses the case that mattered most — the gate sharing the reviewed
   party's blind spots — but the specialist seats still fail together if
   that model degrades or is withdrawn. There is no fallback, deliberately;
   the posture is fail-closed, not fail-over.

   The behavioural half of the same problem was handled separately and does
   not depend on the model split: RM reads the primary evidence before PM's
   narrative and is told that PM pre-calibrates against RM's own past
   verdicts. See `DECISION_CHAIN_AUDIT.md` (F5).

   What the split does **not** establish: that RM now catches more. It
   removes a correlated failure mode. Whether that changes verdicts is a
   paper-trading question, and the observable is the `reason_category`
   distribution.
2. **Three seats are assigned by analogy**, not measurement:
   `earnings_analyst`, `evening_analyst`, `meta_reflector`. Their nearest
   measured analogues both scored 1.00/1.00, but that is an inference.
3. **The sample is thin.** 1.00/1.00 means "no failure observed", not
   "cannot fail" — 12 runs per model in the August sweep, 6 per model in
   the RM re-run. That is thinnest exactly where it matters most: the RM
   seat's four-way tie is four models that each went 6-for-6, and a tie at
   n=6 could hide a real ordering. It is enough to establish that no
   candidate is *clearly* better and therefore that nothing measurable was
   given up by choosing on independence; it is not enough to prove the four
   are equivalent. `--repeats` deepens it when a decision needs more.
4. **The specialist/review seats other than `portfolio_manager` and
   `risk_manager` were not re-measured after the
   2026-08-13 prompt cleanup and this branch's prompt edits.** Their rows
   come from the August sweep against slightly different prompt text. The
   PM and RM seats were re-run because their prompts/inputs changed materially
   and because a decision turned on them; the specialist seats changed less and
   no decision turns on them, so re-running the full sweep was not worth
   the spend. This is a real gap, and it is the first thing to close if a
   specialist seat starts behaving oddly in paper trading.
5. **`gpt-5.5`'s production-scale tech latency was never measured** — the
   402 above made it unrunnable at 128k `max_tokens`. Its baseline column
   comes from the 3-symbol scenario.
6. **Scenarios are synthetic**, chosen so the correct answer follows from
   arithmetic the prompt already states. They test rule application, not
   market judgement, and no benchmark of this kind predicts P&L.
7. **A benchmark-local `max_tokens` cap distorted 3 trials** before it was
   found and fixed; scenarios now read the production value from
   `settings.yaml` (see `scenarios.py:production_max_tokens`). The affected
   pairs were re-run and fold in via the report merge, which supersedes an
   earlier file's pair with a later one. The correction mattered:
   `deepseek-v4-flash-0731` went 0.00 → 1.00 at the risk seat and
   `deepseek-v4-pro-0813` went 0.50 → 1.00 at the midday seat, the latter
   moving it into the perfect-score group. Neither changed the selection,
   but a table that had kept the original numbers would have been wrong
   about both.
8. **`position_reviewer`'s `midday_exit` scenario ties five candidates at
   1.00 quality** — `gemini-2.5-flash-lite`, `gpt-5.5`, `deepseek-v4-pro-0813`,
   `qwen3.7-flash` and `qwen3-235b-a22b-2507` — and so does not discriminate
   between them. `QAMC_REMEDIATION_SPEC.md` §3.5 previously stated
   `gemini-2.5-flash-lite` as "the weakest model in the stack" at this seat
   as though that were a quality finding; it is corrected there. The
   measurement above supports "passed", not "best" or "worst".

---

## Model-selection strategy (2026-08-27)

Where the money actually goes, measured over the 10 days to 2026-08-27:
**$6.73 total, $5.84 of it the Portfolio Manager — 87%.** Every other seat
combined is 13%. A production PM run costs about **$0.22** (not the $0.46 the
`pm_production_scale` benchmark shows; that scenario runs 30 candidates and 15
holdings, heavier than a real session). Any model-cost work that is not about
the PM seat is rounding error.

### Keep OpenRouter. Change the models.

OpenRouter passes provider inference prices through without markup; the cost
is a ~5.5% fee on credit purchases. In exchange: one API, per-seat model
switching, provider failover, and usage telemetry. Building direct
Qwen/DeepSeek/Z.ai integrations to save ~5% would be false economy at this
scale, and a self-hosted gateway makes no sense until model spend is in the
thousands per month. The cheap models worth testing are already reachable
through it — usually a one-line model-id change, which is exactly what the
per-seat structure was built for.

**Do NOT use OpenRouter's Auto Router for the decision chain.** It selects
models dynamically, which destroys the property this policy exists to
guarantee: knowing which exact model made which trading decision.

### 1. GPT-5.5 Flex — DONE (2026-08-27)

OpenRouter lists OpenAI Flex as a GPT-5.5 provider at exactly half price
($2.50/$15 input/output vs $5/$30). It is **the same model**, not a cheaper
substitute, so there is no quality question to answer: PM cost halves to
~$0.11/run against the seat that is 87% of spend. The only exposure is added
latency, and the session wrapper already enforces a 1200s kill.

**Implemented** as `llm.<agent>_provider_order` in `config/settings.yaml`,
set to `["openai/flex"]` on `portfolio_manager` and unset everywhere else.
Verified against the live catalog on 2026-08-27: all three endpoints
(`openai/flex`, `openai`, `azure`) serve the identical
`openai/gpt-5.5-20260423`, and flex reported 100% uptime over the prior day.

Three properties of the implementation are load-bearing:

- **It is an endpoint choice, not a model choice.** Nothing about which model
  answers changes, so this is outside the benchmark-and-qualify discipline the
  rest of this document describes. `LLMConfig` rejects the field on any seat
  not routed through OpenRouter, because there it would be silently ignored —
  which is how an operator comes to believe a seat is on a tier it never
  reached.
- **Fallbacks stay enabled** (`allow_fallbacks: true`, not `only`). Pinning
  `only` would fail the seat closed when the flex tier is saturated. That is
  the correct posture for a *model* substitution — an unreviewed model must
  never answer — but wrong for a price tier: the fallback endpoint serves the
  same weights, so the exposure is money, and losing a trading session to save
  $0.11 is a bad trade.
- **Cost is now taken from the provider, not the table.** Once one model id is
  served at two prices, `_PRICING_OPENROUTER` cannot be right for both — it
  over-reports on flex and under-reports on the full-price endpoint. OpenRouter
  traffic therefore sends `usage: {include: true}` and records what OpenRouter
  says it charged, falling back to the pinned estimate when no figure comes
  back. This matters beyond accuracy: the daily cost circuit spends against
  these numbers, so a 2x over-report would have starved the budget of exactly
  the headroom this change was made to free. A reported figure that diverges
  from the pinned estimate by more than 1.5x is logged — that is what a
  half-price endpoint looks like, but it also means projections built on the
  pinned table (`project_session_cost.py`, the circuit's worst-case
  reservation) no longer describe what this seat pays.

`EXPECTED_PROVIDER_ORDER` in `ops/commissioning/verify_commissioning.py` pins
the preference the same way `EXPECTED_ROUTING` pins the model: a seat that
loses it on the runtime host still runs the right model on the right provider
and looks entirely normal, while quietly costing twice as much.

### 2. Build a DISCRIMINATING PM scenario before any shootout

This is the part external analysis keeps getting wrong, and this repo has
direct evidence for it. The recommendation "require 1.00 mean and 1.00 worst
run" sounds rigorous and is not: on `midday_exit`, **five of twelve candidates
score exactly 1.00** — `gemini-2.5-flash-lite`, `gpt-5.5`,
`deepseek-v4-pro-0813`, `qwen3.7-flash` and `qwen3-235b-a22b-2507`. That is
not five equally excellent models; that is a scenario with a low ceiling.
`pm_constrained` is likely the same.

**Running a shootout against a test everybody passes is theatre.** Build a
scenario that separates candidates first — the natural material is the failure
modes this system has actually produced: a deterioration claim contradicted by
its own improving metrics, a target citing provenance that does not exist, a
macro conflict that must be adjudicated rather than logged.

### 3. Then the challengers, against the strict contract

The 2026-08-25 strict PM grounding rerun covers `gpt-5.5`, `gemini-2.5-flash`
and `gpt-5.6-luna` only. **DeepSeek V4 Pro, GLM-5.3-Flash and Qwen3.8 Flash
have never been run against it** — DeepSeek's 1.00 predates the stricter
contract entirely. That gap is the actual research to do.

- **Qwen3.8 Flash** — ~$0.15/$0.47, 1M context, and **proper JSON-schema
  structured output**. For a seat whose contract is this strict, schema
  enforcement likely matters more than the last fraction of a cent.
- **GLM-5.3-Flash** — cheapest by a wide margin, but OpenRouter reports it
  supports JSON output **without enforcing JSON Schema**. Against the PM's
  provenance and grounding contract that is a plausible disqualifier, and it
  is exactly the kind of thing generic intelligence benchmarks miss.
- **DeepSeek V4 Pro 0813** — already 1.00 on the older sweep, passed over then
  for latency, not quality. Cheap to re-run.

Require the winner to shadow-run against GPT-5.5 on live paper sessions before
it is given authority. A benchmark is evidence; production agreement is proof.

### What NOT to spend on yet

SIP market data (owner declined 2026-08-27 for the paper phase — see
`docs/WORK.md`), Alpaca Elite smart routing, and a wider universe. None of
them is the binding constraint.
