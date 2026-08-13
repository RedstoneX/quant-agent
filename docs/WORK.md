# QAMC Current Work

Status: **AT THE EXTERNAL GATE — model routing reconciled onto `main`, deferred agent-audit findings closed, one operator item open**

Branch: `claude/qamc-routing-and-agent-audit-reconcile`. Claude does not merge its own work.

## What this branch contains

Two tranches. The first was written against `64cb43c` and never merged while
`main` moved on; the second is new.

### 1. Cost-optimized model routing (reconciled, previously unmerged)

`claude/cost-optimized-model-routing-h4k2vn` is merged in whole. It had
diverged five commits from `main` — four governance/doc, one prompt-audit fix
— and only `docs/WORK.md` conflicted, in four places, all of them its
rewritten handoff meeting fragments of the older "required sequence" document
it replaced. `main`'s newer position on timer activation is preserved
verbatim; the routing branch's re-statement of the older framing was dropped.
No code conflicted.

Every agent seat moves from the `openai/gpt-5.5` commissioning baseline to
`google/gemini-2.5-flash-lite`, expressed entirely through the existing
`config/settings.yaml` provider/model seam. No routing service, no new
dependency, no change to `_execute`.

Full contract, evidence and limitations:
`docs/architecture/MODEL_ROUTING_POLICY.md`.

Selected from 148 graded trials driven through the real agent classes and
prompts. Three models scored a perfect mean **and** a perfect worst run; the
selected one won on the two axes left after quality tied — 83x cheaper than
the baseline and 5-10x faster per call than the other perfect scorer, which
matters because sessions are wall-clock bounded.

**The tranche was authorized to reserve stronger models for seats that
demonstrably benefit. No seat did.** Every more expensive candidate scored
the same or worse. The per-seat structure is retained so a future seat can
diverge as a config edit.

It also fixed cost telemetry, which did not previously work: `estimate_cost`
could not price an OpenRouter `vendor/model` id — LiteLLM keys models by bare
vendor id, so `"openai/gpt-5.5"` matched nothing, `cost_usd` persisted as NULL
on every call, and every session pushed `$?.??`.

| | baseline | policy | cut |
|---|---|---|---|
| per trading day | $3.4334 | $0.0539 | 98.4% |
| per month (21 trading days) | **$72.10** | **$1.13** | **63.7x** |

Re-derive: `python ops/model_policy/project_session_cost.py`.

### 2. The deferred agent-audit findings (new)

The 2026-08-13 adversarial audit landed its text-only findings in `57f5b2d`
and held five back as "behaviour or data flow, needs external architectural
review". All five are now resolved — three as fixes, two as recorded
decisions to retain.

Full record — finding, why it mattered, decision, change, evidence, remaining
uncertainty: **`docs/architecture/DECISION_CHAIN_AUDIT.md`**.

In one line each:

- **F6 risk evidence completeness** — RM's prompt made it the enforcer of
  PM's holding-discipline and drawdown-halve rules while it received neither
  `days_held` nor `in_drawdown`; both now reach it, from the same snapshot PM
  sized from.
- **F5 PM/RM independence** — PM's self-justification was the *first* block
  in RM's message; it now sits after the primary evidence and is labelled as
  claims, and RM is told that PM calibrates against its own verdict history
  and that the two share a model. The veto hierarchy is unchanged.
- **F4 premortem/observability** — a mandatory audit step could be skipped
  with no parse error, no log line and no reader; it is now rendered,
  logged and raised as a non-blocking engine advisory.
- **F7b earnings valuation data** — decided **not** to route price data into
  a filing read that is cached for the life of the filing; a detector now
  flags valuation claims that need a share price.
- **F8 inherited priors** — three rules fitted to the predecessor account's
  Apr–Jul 2026 window now carry their provenance and lose to this account's
  own measured facts once those exist.

**No deterministic risk or execution semantics changed in either tranche.**
No threshold moved, no gate was added or removed, no schema became stricter,
and every failure path added degrades to "evidence unavailable" rather than
to a benign-looking value.

## Operator item — one, and it is no longer blocking

### Run the `qamc`-account half of commissioning acceptance

Acceptance is the union of two account runs (`ops/onecli/README.md` step 4e).
The `dev` half is green. `dev` is in the sudo group but sudo requires a
password, so Claude cannot open a `qamc` session. Three checks can only be
evaluated there: config startup validation with real credentials, the runtime
CA environment variables, and trading-timer state.

```bash
sudo -u qamc -i
```

```bash
cd /home/qamc/quant-agent && git fetch && git checkout claude/qamc-routing-and-agent-audit-reconcile && git pull && python3 ops/commissioning/verify_commissioning.py --live
```

Expect `COMMISSIONING ACCEPTANCE: PASS`, exit 0, and
`ACCOUNT COVERAGE: complete`. The `--live` preflight makes one real call per
distinct policy model (one model, a fraction of a cent).

Note the checkout step: the runtime must be on this branch for the routing
check to pass, since the expected per-seat map names the new model.

### Resolved since the routing branch was written: OpenRouter credit

That branch recorded a hard blocker — $10 granted, $7.96 used, $2.04 left,
against a baseline model that reserves $3.84 per call at `max_tokens: 128000`,
meaning **as commissioned no agent call could have started** (402,
non-retryable, no failover, session dead at the first agent). Commissioning
missed it because the preflight uses `max_tokens=512`.

`/api/v1/credits` now reports **$25 granted, $8.02 used — $16.98 remaining**.
The account has been topped up and the blocker is cleared on both policies.
The routing policy still reduces the reservation per call from $3.84 to $0.05
and projected spend 63x, so the economics argument is unchanged; it is simply
no longer an availability argument.

## Verification status

| Check | Result |
|---|---|
| Full test suite | **1821 passed, 1 skipped** |
| `verify_commissioning.py --from-onecli` (dev) | **PASS**, 24 passed / 0 failed / 4 skipped |
| Agent routing check | PASS — all 9 seats on openrouter, per-seat map matches the accepted policy |
| Alpaca | Paper only; `paper=true` asserted in config and preflight |
| Trading timers | remain disabled |
| Mission Control | remains read-only |
| Secrets | none in tracked files |
| Model spend this tranche | **$0.00** — no model was called; findings were resolved from the repository and its history |

## For the reviewer — the open design question

**The Portfolio Manager and the AI Risk Manager run the same model.** The
decision chain exists so RM independently checks PM; a shared model shares
blind spots. Splitting them would trade measured quality for hypothetical
independence — every alternative scored worse at the RM seat — so the
evidence was followed and the question surfaced rather than settled quietly.
A split is a one-line config change.

The F5 work above is the cheap half of the answer: RM now reads the primary
evidence before PM's narrative, knows PM calibrates against it, and knows it
shares PM's model. None of that makes the gate *structurally* independent.
That trade remains the reviewer's call.

Other limitations are listed in `MODEL_ROUTING_POLICY.md` (three seats
assigned by role analogy rather than direct measurement; a benchmark-local
`max_tokens` cap that distorted 3 trials before it was found and corrected)
and in `DECISION_CHAIN_AUDIT.md` (what stays unproven until paper trading).

## Timer handling

The trading timers are only the scheduler for autonomous Alpaca Paper runs. **Do not treat timer activation as a separate design, architecture, or research decision.**

Keep scheduled trading inactive while this work is incomplete. After ChatGPT's external review accepts the final tranche and the operator authorizes paper-soak start, enabling the timers is a routine deployment action. Do not spend engineering time repeatedly revisiting this point.

## Hard boundaries (unchanged)

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign.
- No broker-write Mission Control controls.
- No public services.
- No collapse of `dev` / `qamc` / `ubuntu` isolation.
- No replacement for upstream OneCLI.
- No new durable routing infrastructure.
- No secrets in Git/chat/logs/screenshots/client evidence.
- Claude does not merge its own PR or push implementation to `main`.
