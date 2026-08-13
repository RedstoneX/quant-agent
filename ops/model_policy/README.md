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
prompts (`config/prompts/*.md`) over frozen synthetic inputs, and grades
each result with deterministic Python assertions in `scenarios.py`.

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
trimming, `midday_exit` has one position pinned 0.25 ATRs from its stop.

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
