# Model Routing Policy — Accepted Contract

Status: **proposed by the cost-optimized routing tranche — pending external review**.

Supersedes the commissioning baseline recorded in `docs/STATE.md` ("every
agent on `openai/gpt-5.5`"), which was a deliberate single-variable posture
for proving the OpenRouter transport, never the intended production policy.

Read alongside `MODEL_PROVIDER_ARCHITECTURE.md`, which owns the seam this
policy is expressed through and is unchanged by it.

## The policy

Every agent seat runs `provider: openrouter` on
**`google/gemini-2.5-flash-lite`**.

| Seat | Model | Basis |
|---|---|---|
| `tech_analyst` | `google/gemini-2.5-flash-lite` | measured — `tech_batch`, `tech_batch_full` |
| `news_analyst` | `google/gemini-2.5-flash-lite` | measured — `news_intel` |
| `macro_analyst` | `google/gemini-2.5-flash-lite` | measured — `macro_stress` |
| `portfolio_manager` | `google/gemini-2.5-flash-lite` | measured — `pm_constrained` |
| `risk_manager` | `google/gemini-2.5-flash-lite` | measured — `risk_rr_breach` |
| `position_reviewer` | `google/gemini-2.5-flash-lite` | measured — `midday_exit` |
| `earnings_analyst` | `google/gemini-2.5-flash-lite` | **by analogy** to `news_analyst` |
| `evening_analyst` | `google/gemini-2.5-flash-lite` | **by analogy** to `portfolio_manager` |
| `meta_reflector` | `google/gemini-2.5-flash-lite` | **by analogy** to `portfolio_manager` |

### One model is the finding, not a shortcut

The tranche was authorized to reserve stronger models for seats that
demonstrably benefit. **No seat did.** Every model more expensive than the
selected one scored the same or worse on the seats it would have occupied,
so assigning one would have bought cost and complexity for no measured
quality. Saying so plainly is more useful than manufacturing tiers.

The per-seat *structure* is retained even though all nine values match. It
is what lets a single seat diverge later as a config edit rather than a
plumbing change, and it is what `verify_commissioning.py` pins against.

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

`ops/model_policy/benchmark_models.py`, 144 graded trials on 2026-08-12:
12 models x 6 scenarios x 2 repeats, driven through the **real** agent
classes with the **real** prompts, graded by deterministic assertions.
Raw results in `ops/model_policy/results/`.

Quality is the weighted mean of a scenario's graded checks (0–1). `$/sweep`
is the measured token cost of one pass over all six scenarios.

| model | mean | worst run | $/sweep |
|---|---|---|---|
| **`google/gemini-2.5-flash-lite`** | **1.00** | **1.00** | **$0.0079** |
| `openai/gpt-5.5` *(baseline)* | 1.00 | 1.00 | $0.6547 |
| `z-ai/glm-5.2` | 0.97 | 0.65 | $0.0605 |
| `qwen/qwen3.7-max` | 0.97 | 0.65 | $0.2029 |
| `qwen/qwen3-235b-a22b-2507` | 0.95 | 0.85 | $0.0064 |
| `deepseek/deepseek-v4-pro-0813` | 0.92 | 0.00 | $0.0661 |
| `qwen/qwen3.7-plus` | 0.85 | 0.20 | $0.0760 |
| `openai/gpt-5.6-luna` | 0.80 | 0.00 | $0.0102 |
| `minimax/minimax-m3` | 0.78 | 0.00 | $0.0492 |
| `deepseek/deepseek-v4-flash-0731` | 0.78 | 0.00 | $0.0120 |
| `qwen/qwen3.7-flash` | 0.74 | 0.00 | $0.0055 |
| `openai/gpt-5-nano` | 0.63 | 0.00 | $0.0242 |

Exactly two models scored a perfect mean **and** a perfect worst run. One
costs 83x the other.

**`worst run` is the column that matters.** A mean hides the failure mode
that actually hurts: a model that alternates between excellent and
unparseable averages respectably and silences a session every other day.
Six of twelve candidates have a 0.00 in there — a run that produced nothing
the pipeline could use.

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
| per trading day | $3.4334 | $0.0539 | 98.4% |
| per month (21 days) | **$72.10** | **$1.13** | **63.7x cheaper** |

`tech_analyst` is over half of it: five calls a day at 33,328 input tokens
each, measured at real chunk size rather than extrapolated.

Treat the ratio as the load-bearing number. It is dominated by published
per-token rates, which are exact, and by a token profile applied
identically to both sides. The absolute dollars carry a real error bar:
QAMC has run no live sessions, so there is no `agent_logs` history to
average, and three seats' token profiles are structural estimates.

## An operational fault this exposed

**As commissioned, QAMC could not have completed a single agent call.**

OpenRouter pre-authorizes worst-case output spend before starting a
request. At the configured `max_tokens: 128000`, `gpt-5.5` reserves
`128000 x $30/M = $3.84` per call. The account holds **$2.04**
(`/api/v1/credits`: $10 granted, $7.96 used). Every call therefore returns
HTTP 402 — which `_is_retryable` correctly classifies as non-retryable, so
`_execute` fails immediately, and with no Anthropic key there is no
failover. The first agent of the first session dies and the session fails
closed.

Commissioning did not catch this because the preflight calls with
`max_tokens=512`, reserving about a cent.

The policy fixes it as a side effect. Same 128k ceiling, per-call
reservation by model:

| model | reserved per call | affordable at $2.04 |
|---|---|---|
| `openai/gpt-5.5` | $3.8400 | **no — 402** |
| `google/gemini-2.5-flash-lite` | $0.0512 | yes |

A 75x reduction in credit reserved per call, independent of tokens
actually spent. **The operator must still top up OpenRouter credit before
any live session** — see `docs/WORK.md`.

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
| `tests/test_model_routing_policy.py` | every seat explicit, every model priceable offline, decision seats off the cheapest tier, policy materially cheaper than baseline, fail-closed with no model substitution |
| `verify_commissioning.py` (`config`) | the DEPLOYED config matches the reviewed per-seat map — the expected map is a separate copy on purpose, so editing `settings.yaml` on the runtime host fails the check |
| `verify_commissioning.py` (`preflight`) | every distinct policy model is in the catalog AND completes a real call; WARNs if OpenRouter serves a different model than requested |
| `ops/model_policy/verify_pricing.py` | pinned rates still match the live catalog |
| `ops/model_policy/benchmark_models.py` | re-derives the whole decision from scratch |

## Known limitations

1. **Monoculture, and PM/RM share a model.** The decision chain exists so
   the Risk Manager independently checks the Portfolio Manager; one model
   means shared blind spots. Splitting them would trade *measured* quality
   for *hypothetical* independence — every alternative scored worse at the
   RM seat — so the evidence was followed. **This is the top item for
   external review**, and the per-seat structure makes a split a one-line
   change.
2. **Three seats are assigned by analogy**, not measurement:
   `earnings_analyst`, `evening_analyst`, `meta_reflector`. Their nearest
   measured analogues both scored 1.00/1.00, but that is an inference.
3. **Twelve trials per model is thin.** 1.00/1.00 means "no failure
   observed in 12 runs", not "cannot fail". `--repeats` exists to deepen
   this when it matters.
4. **`gpt-5.5`'s production-scale tech latency was never measured** — the
   402 above made it unrunnable at 128k `max_tokens`. Its baseline column
   comes from the 3-symbol scenario.
5. **Scenarios are synthetic**, chosen so the correct answer follows from
   arithmetic the prompt already states. They test rule application, not
   market judgement, and no benchmark of this kind predicts P&L.
6. **A benchmark-local `max_tokens` cap distorted 3 of 144 trials** before
   it was found and fixed; scenarios now read the production value from
   `settings.yaml`. See the note in `scenarios.py:production_max_tokens`.
