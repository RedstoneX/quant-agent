# QAMC Current Work

Status: **POST-MERGE RUNTIME ACCEPTANCE**

Accepted implementation: PR #30 merged to `main` as `7b78f72ecfc900e166af2207a6f2a8473c277131`.

## Goal

Close the one remaining privileged runtime acceptance step against accepted `main`. Do not reopen model-routing or decision-chain architecture unless the runtime evidence exposes a real defect.

## What is already closed

- Cost-optimized OpenRouter routing is accepted: eight seats on `google/gemini-2.5-flash-lite`, `risk_manager` on `qwen/qwen3-235b-a22b-2507`.
- The RM split was re-measured on the current prompt/input path: four candidates tied at 1.00 mean / 1.00 worst; independence then latency/cost selected Qwen for RM.
- The price-as-quality proxy is removed and replaced with seat-specific benchmark evidence for decision seats.
- OpenRouter `vendor/model` pricing no longer falls through to LiteLLM direct-provider rates.
- The deferred agent-audit findings F4/F5/F6/F7b/F8 are accepted.
- Full suite at the reviewed implementation head: **1829 passed, 0 skipped**.
- `dev` commissioning acceptance passed with live OpenRouter preflight for both policy models.
- `verify_pricing.py` passed for all pinned OpenRouter rates.
- Alpaca remains Paper-only; Mission Control remains read-only; trading timers remain disabled.
- No deterministic risk or execution semantics changed.

Architecture/evidence:
- `docs/architecture/MODEL_ROUTING_POLICY.md`
- `docs/architecture/DECISION_CHAIN_AUDIT.md`

## Remaining operator-only step

The `qamc` runtime account must be updated to accepted `main` and run its half of commissioning acceptance. `dev` cannot do this because sudo requires the operator password.

Run as the operator:

```bash
sudo -u qamc -i
```

Then, as `qamc`:

```bash
cd /home/qamc/quant-agent
git fetch origin
git checkout main
git pull --ff-only origin main
systemctl --user restart quant-agent-api.service
python3 ops/commissioning/verify_commissioning.py --live
```

Accept only if the verifier ends with:

```text
COMMISSIONING ACCEPTANCE: PASS
ACCOUNT COVERAGE: complete
```

and exits `0`.

The live preflight makes one real call per distinct policy model — currently two. Do not switch the runtime checkout to an old Claude/review branch and do not copy the verifier across accounts by hand.

If the verifier fails, capture the failing check names and output only; do not redesign architecture in response to an operational failure.

## After runtime acceptance

1. Record the accepted runtime evidence in `docs/STATE.md` / this file.
2. Keep trading timers disabled until the operator explicitly authorizes the Alpaca Paper soak.
3. Once authorized, timer activation is routine deployment, not a new architecture decision.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign.
- No broker-write Mission Control controls.
- No public services.
- No collapse of `dev` / `qamc` / `ubuntu` isolation.
- No replacement for upstream OneCLI.
- No new durable routing infrastructure.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.
