# QAMC Current Work

Status: **AT THE EXTERNAL GATE — cost-optimized model routing complete, two operator items open**

Branch: `claude/cost-optimized-model-routing-h4k2vn`. Claude does not merge its own work.

## What was delivered

### 1. Per-seat model routing policy

Every agent seat moves from the `openai/gpt-5.5` commissioning baseline to
`google/gemini-2.5-flash-lite`, expressed entirely through the existing
`config/settings.yaml` provider/model seam. No routing service, no new
dependency, no change to `_execute`.

Full contract, evidence and limitations:
`docs/architecture/MODEL_ROUTING_POLICY.md`.

Selected from 148 graded trials driven through the real agent classes and
prompts. Three models scored a perfect mean **and** a perfect worst run;
the selected one won on the two axes left after quality tied — it is 83x
cheaper than the baseline and 5-10x faster per call than the other perfect
scorer, which matters because sessions are wall-clock bounded.

**The tranche was authorized to reserve stronger models for seats that
demonstrably benefit. No seat did.** Every more expensive candidate scored
the same or worse. The per-seat structure is retained so a future seat can
diverge as a config edit.

### 2. Cost telemetry, which did not previously work

`estimate_cost` could not price an OpenRouter `vendor/model` id — LiteLLM
keys models by bare vendor id, so `"openai/gpt-5.5"` matched nothing,
`cost_usd` persisted as NULL on every call, and every session pushed
`$?.??`. Fixed inside `src/cost_table.py` with OpenRouter's own rates plus
a catalog resolver and a drift check (`ops/model_policy/verify_pricing.py`).

### 3. Expected cost reduction

| | baseline | policy | cut |
|---|---|---|---|
| per trading day | $3.4334 | $0.0539 | 98.4% |
| per month (21 trading days) | **$72.10** | **$1.13** | **63.7x** |

Re-derive: `python ops/model_policy/project_session_cost.py`.

## Operator items — both required before any live session

### A. Top up OpenRouter credit

`/api/v1/credits` reports **$10 granted, $7.96 used, $2.04 remaining**.
Benchmarking this tranche consumed a significant share.

This is not only a budget note. OpenRouter pre-authorizes worst-case output
spend before starting a request. At the configured `max_tokens: 128000`:

| model | reserved per call | at $2.04 |
|---|---|---|
| `openai/gpt-5.5` (baseline) | $3.84 | **402 — call cannot start** |
| `google/gemini-2.5-flash-lite` (policy) | $0.05 | fine |

So **as commissioned, QAMC could not have completed a single agent call.**
402 is non-retryable and there is no failover, so the first agent of the
first session would have failed the session closed. Commissioning missed it
because the preflight uses `max_tokens=512`.

The new policy makes calls affordable at the current balance, but the
account should still be funded before trading is enabled.

### B. Run the `qamc`-account half of commissioning acceptance

Acceptance is the union of two account runs (`ops/onecli/README.md` step 4e).
The `dev` half is green:

```
32 passed, 0 failed, 0 warned, 3 skipped
COMMISSIONING ACCEPTANCE: PASS
```

`dev` is in the sudo group but sudo requires a password, so Claude cannot
open a `qamc` session. Three checks can only be evaluated there: config
startup validation with real credentials, the runtime CA environment
variables, and trading-timer state.

```bash
sudo -u qamc -i
cd /home/qamc/quant-agent && git fetch && git checkout claude/cost-optimized-model-routing-h4k2vn && git pull
python3 ops/commissioning/verify_commissioning.py --live
```

Expect `COMMISSIONING ACCEPTANCE: PASS`, exit 0, and
`ACCOUNT COVERAGE: complete`. The `--live` preflight makes one real call per
distinct policy model (one model, a fraction of a cent).

Note the checkout step: the runtime must be on this branch for the routing
check to pass, since the expected per-seat map now names the new model.

## Verification status

| Check | Result |
|---|---|
| Full test suite | 1738 passed, 1 skipped |
| `verify_commissioning.py --live --from-onecli` (dev) | PASS, exit 0 |
| `verify_pricing.py` | 2/2 pinned rates match the live catalog |
| Alpaca | Paper only; `paper=true` asserted in config and preflight |
| Trading timers | remain disabled |
| Mission Control | remains read-only |
| Secrets | none in tracked files; benchmark uses the OneCLI placeholder |

## For the reviewer — the one open design question

**The Portfolio Manager and the AI Risk Manager now run the same model.**
The decision chain exists so the RM independently checks the PM; a shared
model shares blind spots. Splitting them would trade measured quality for
hypothetical independence — every alternative scored worse at the RM seat —
so the evidence was followed and the question surfaced rather than settled
quietly. A split is a one-line config change.

Other limitations are listed in `MODEL_ROUTING_POLICY.md`, including three
seats assigned by role analogy rather than direct measurement, and a
benchmark-local `max_tokens` cap that distorted 3 trials before it was
found and corrected.

## Hard boundaries (unchanged)

- Alpaca **Paper only**; no live trading.
- Trading timers stay disabled until external review approves activation.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign.
- No broker-write Mission Control controls.
- No public services.
- No collapse of `dev` / `qamc` / `ubuntu` isolation.
- No replacement for upstream OneCLI.
- No new durable routing infrastructure.
- No secrets in Git/chat/logs/screenshots/client evidence.
- Claude does not merge its own PR or push implementation to `main`.
