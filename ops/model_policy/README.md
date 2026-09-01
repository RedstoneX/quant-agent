# Model policy tooling

Three commands. Everything here is read-only with respect to trading: no
order is placed, no broker state is touched, no credential is held.

| Command | What it answers |
|---|---|
| `benchmark_models.py` | which model each agent seat should use |
| `verify_pricing.py` | are the pinned OpenRouter rates still correct |
| `../commissioning/verify_commissioning.py` | is the deployed config actually on the accepted policy |

The resulting policy and its evidence live in
[`docs/architecture/MODEL_ROUTING_POLICY.md`](../../docs/architecture/MODEL_ROUTING_POLICY.md).

## Benchmarking

```bash
.venv/bin/python ops/model_policy/benchmark_models.py --from-onecli --repeats 2
```

This drives the **real** agent classes (`src/agents/*`) with the **real**
prompts (`config/prompts/*.md`) over frozen inputs — synthetic for every
scenario except `pm_selection`, which replays a real recorded session — and
grades each result with deterministic Python assertions in `scenarios.py`.

That choice is the point of the harness. The question is not "can this
model write JSON" — it is "does its output survive `analyze_batch` /
`decide` / `review`, including `parse_json()`'s candidate scan, the
per-entry isolation that drops malformed rows, and every Pydantic
validator in `src/models.py`". Only the production call path answers that.
The sweep has already caught a model whose only flaw was emitting
`market_sentiment="neutral_to_defensive"` — plausible prose, and the entire
news report is discarded before PM ever sees it.

Grading never asks whether a model shares an opinion about the market.
Every scenario is built so the correct answer follows from arithmetic the
prompt already states: `risk_rr_breach` contains a BUY at 0.42R against a
documented 1.5 floor, `pm_constrained` cannot fund new weight without
trimming, `midday_exit` has one position pinned 0.25 ATRs from its stop,
`risk_drawdown_discipline` has a BUY sized at the full base while
`in_drawdown=true` requires it halved.

### Scoping a re-run to one seat

A prompt or input change to a single agent invalidates that agent's rows
and nothing else. Re-run the seat rather than the sweep:

```bash
.venv/bin/python ops/model_policy/benchmark_models.py --from-onecli \
  --models google/gemini-2.5-flash-lite deepseek/deepseek-v4-pro-0813 \
  --scenario risk_rr_breach --scenario risk_drawdown_discipline \
  --repeats 3 --out results/rm-rerun-<date>.json
```

`tests/test_model_routing_policy.py` reads every file in `results/` and
requires each decision seat's configured model to carry a `quality_min` of
1.00 at its own scenario, so a seat whose evidence has gone stale fails the
suite rather than drifting unnoticed.

`risk_drawdown_discipline` is `default=False` — it informs the risk seat
only and would otherwise be paid for on every candidate in a full sweep.

### `pm_selection` — the one scenario built from real data

Every other scenario asks whether a model applies a rule. `pm_selection`
asks a different question: **given a real day's evidence, does it pick the
candidates the evidence supports, or the tickers it already knows?**

```bash
.venv/bin/python ops/model_policy/benchmark_models.py --from-onecli \
  --scenario pm_selection --models openai/gpt-5.5 --repeats 2 \
  --out results/pm-selection-<date>.json
```

`pm_production_scale` cannot answer that, and it was never meant to:
`_PM_PRODUCTION_ANALYSES` loops over a ticker list and hands every one of
its 30 candidates the same `buy`/`medium` analysis at entry 100 / stop 94 /
target 112, so any five of them score identically. Its checks count targets
and never record which symbols were chosen, so the committed results cannot
tell a considered selection from an arbitrary one. That scenario is a valid
robustness test and is left untouched; this is the missing measurement, not
a replacement.

The fixture (`fixtures/run_64290730_pm_input.json`) is a verbatim pull of
production run `run-64290730` from the read-only Mission Control API: 59
technical reads, 5 held positions, the session's macro, news and earnings
evidence, and the real BUY-eligibility universe. Nothing is rounded or
tidied, and the candidates are in the run's own presentation order rather
than sorted. Its `_provenance.fidelity` block is measured, not asserted:
rendering the fixture through `build_user_message` and diffing it against
the recorded prompt gives 18 of 22 shared sections byte-identical. It lists
what is absent (PMFacts, portfolio heat, company profiles — all computed
live from the production DB; the smart-money findings, which would have
required inventing SEC source URLs) and what that changes for a model —
notably that Energy reads as macro-bullish here where the live session had
it macro-neutral.

That day is the desk's own documented failure — 38 actionable signals, zero
trades, `bearish_hedge_considered=false` — which is why matching what the
live PM did is graded as failure, not success. The evidence-versus-
familiarity contrast is in the real numbers and was not planted: the day's
two highest-conviction calls are unglamorous and both below the reward/risk
floor (SLB `strong_buy` at 1.28, AGX `sell` at 0.84), five of the eight
candidates clearing the floor are shorts, and every mega-cap that got a
read is weak (NVDA 1.03, AAPL 1.02, MSFT 0.85, GOOGL 0.59).

**It measures quality of selection. It does not measure profitability** —
nobody knows which of these picks would have made money, and no check here
pretends otherwise. `familiarity_bias` reports the share of picks that are
famous-and-weak as a number on every run, passing or failing; read it as a
rate across models and repeats, not as a verdict on one run.

`default=False`: the rendered prompt is 194,173 characters, 91.4% of the
live session's, which billed 61,557 input tokens and cost $0.24 on
`openai/gpt-5.5` — so budget roughly that per call. Opt-in like
`pm_production_scale`.

`tests/test_pm_selection_scenario.py` drives the grader with hand-built
decisions and keeps it honest without spending anything.

Useful flags:

```bash
# one model, one scenario — the fast loop when adding a scenario
... --models qwen/qwen3.7-flash --scenario risk_rr_breach

# add a scenario without re-paying for the whole sweep
... --scenario midday_exit --out results/sweep-midday.json
... --report results/sweep.json results/sweep-midday.json --merge-out results/merged.json
```

Cost: a full sweep is `models x scenarios x repeats` real LLM calls. The
2026-08-12 slate (12 models, 6 scenarios, 2 repeats) came to a few dollars,
almost all of it the `openai/gpt-5.5` baseline rows.

`--repeats 2` is the minimum worth trusting. LLM output varies run to run,
and the aggregate reports `quality_min` alongside the mean precisely so a
model that averages well by alternating between excellent and unusable is
visible as such.

### Credentials

Nothing here holds a real key. The benchmark sends the same
`placeholder-managed-by-onecli` stand-in the commissioning preflight uses
and lets the OneCLI gateway substitute the real value server-side, so a
results file or a benchmark log cannot contain a credential.
`--from-onecli` resolves the gateway wiring the way
`ops/commissioning/verify_commissioning.py` does; on the runtime account
the process environment already carries it and the flag is unnecessary.

### Adding a scenario

Add a `Scenario` to `SCENARIOS` in `scenarios.py` with an `invoke` that
calls the agent's real public entry point and a `grade` that returns
weighted `Check`s. Two rules learned the hard way:

- **Calibrate before trusting it.** Run the new scenario against the
  baseline AND against a deliberately weak model. If both score 1.00 the
  scenario measures nothing; if both score 0.00 the bug is in the grader.
  Both happened on the first pass here.
- **Grade the instruction, not your own preference.** An early check
  required `thesis_invalid_if` on every symbol and marked both models
  down — but `config/prompts/tech_analyst.md` explicitly says to leave it
  empty on `neutral`. The models were right and the rubric was wrong.

## Pricing provenance

```bash
.venv/bin/python ops/model_policy/verify_pricing.py
```

`src/cost_table.py:_PRICING_OPENROUTER` is hand-copied from OpenRouter's
catalog, and a hand-copied rate goes stale silently — producing a cost
report that looks confident and is wrong, which is worse than `$?.??`.
This re-reads the live catalog and exits non-zero on any drift.

Why a separate table at all, rather than LiteLLM: LiteLLM keys models by
bare vendor id (`gpt-5.5`), so an OpenRouter id (`openai/gpt-5.5`) missed
every lookup — under the commissioned baseline **every agent call logged
`$?.??`**, i.e. the deployment had no cost telemetry. And where LiteLLM
does carry a match, its rate is the vendor's *direct* API price, which is
not what routed traffic costs.
