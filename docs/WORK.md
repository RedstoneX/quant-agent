# QAMC Current Work

Status: **AT THE EXTERNAL RE-REVIEW GATE — PR #30's three blockers resolved; one operator item open**

Branch: `claude/qamc-routing-and-agent-audit-reconcile`, PR #30. Claude does not merge its own work.

## PR #30 review — what changed since

The external gate accepted the audit implementation in principle and raised
three bounded blockers. All three are resolved.

### Blocker 1 — the F5/independence rationale was factually wrong and stale

It was. The docs said "every alternative scored worse at the RM seat"; the
committed 2026-08-12 results show **10 of 12 candidates scoring 1.00 on both
runs** at `risk_rr_breach`. The claim had read the whole-sweep aggregate as
if it were the seat's result — a model mediocre across six roles can be
perfect at one, and the RM seat only ever runs RM.

Re-measured on this branch (2026-08-14, 5 models x 2 RM scenarios x 3
repeats, 30 trials, **$0.157**). Four candidates tie at 1.00 mean / 1.00
worst; a deliberately-included weak control still scores 0.00, so the
scenarios discriminate on the current prompts.

Applying the review's stated ordering — quality, then independence, then
latency/cost — produced a policy change:

**`risk_manager` now runs `qwen/qwen3-235b-a22b-2507`; PM stays on
`google/gemini-2.5-flash-lite`.** Quality tied, so independence decided;
among the three independent candidates, latency and cost picked the winner
decisively (10.4s vs 61.4s and 113.0s, at ~1/14th the cost).

Independence turned out to be **free**: $0.00162 vs $0.00163 per two RM
calls — the input rate is actually lower — and +7.6s on a seat that makes
one call per 1200s session. That last point is why the original analysis
does not transfer: `deepseek-v4-pro` was disqualified by `tech_analyst`'s
five sequential calls, a constraint the RM seat does not have.

`MODEL_ROUTING_POLICY.md` and `DECISION_CHAIN_AUDIT.md` are corrected, and
both now state plainly what the split does **not** establish: it removes a
correlated failure mode; it does not demonstrate a better verdict.

Two harness gaps were closed to make this measurement honest:
`risk_rr_breach`'s `invoke` was not passing `position_history` /
`recent_performance`, so it measured the seat in a configuration production
never runs; and nothing exercised the two rules F6 gave RM evidence for. A
second scenario `risk_drawdown_discipline` (opt-in) now forces both breaches
arithmetically. Its grader was calibrated at zero cost first — rubber-stamp
0.30, acts-but-silent 0.60, full catch 1.00.

### Blocker 2 — the model-price quality proxy

`test_decision_seats_are_not_on_the_cheapest_tier` is gone. Price is not a
quality or safety property, and the proxy was actively backwards: it would
have **failed** the accepted policy (both routed models sit at or below
$0.10/M input) while passing any expensive model nobody had measured.

Replaced with the invariant the tranche's own method implies —
`test_decision_seats_run_a_model_measured_at_that_seat`: every decision
seat's configured model must carry a committed benchmark result **at its own
scenario** with `quality_min == 1.00`, at least 2 runs, and no errors. Plus
`test_risk_seat_evidence_covers_the_rules_the_audit_gave_it`, which requires
the risk seat to additionally be measured on `risk_drawdown_discipline`.
Mapping, provider-pinning, offline priceability, fail-closed behaviour and
the cost-reduction assertion were already covered and are unchanged.

### Blocker 3 — the OpenRouter pricing fall-through

`_resolve_unknown_model()` tried OpenRouter for a `vendor/model` id and, on
a miss, fell through to the LiteLLM path — which probes `<id>`,
`openai/<id>` and `anthropic/<id>`, any of which can hit an unrelated row.
`openai/gpt-5.5` is the live example: LiteLLM carries that exact key for
OpenAI's **direct** API, at a rate that is not what routed traffic costs.

An OpenRouter id is now resolved against OpenRouter's catalog and only that
catalog. A new `saw_catalog` signal distinguishes "the catalog does not list
this model" (permanent — memoised) from "the catalog was unreachable"
(transient — retried), mirroring the `saw_dataset` discipline the LiteLLM
path already used.

Six regression tests. Four of them were verified to **fail against the
pre-fix code**, including the `openai/<id>` probe collision the old
docstring warned about but did not prevent, and one asserting the LiteLLM
lookup is not merely wrong-free but never reached.

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

Every agent seat moves off the `openai/gpt-5.5` commissioning baseline —
eight to `google/gemini-2.5-flash-lite`, `risk_manager` to
`qwen/qwen3-235b-a22b-2507` (see Blocker 1 above) — expressed entirely
through the existing `config/settings.yaml` provider/model seam. No routing
service, no new dependency, no change to `_execute`.

Full contract, evidence and limitations:
`docs/architecture/MODEL_ROUTING_POLICY.md`.

Selected from 148 graded trials driven through the real agent classes and
prompts, plus the 30-trial RM re-run. Three models scored a perfect mean
**and** a perfect worst run across all six roles; for the eight shared seats
the selected one won on the two axes left after quality tied — 83x cheaper
than the baseline and 5-10x faster per call than the other perfect scorer,
which matters because sessions are wall-clock bounded.

**The tranche was authorized to reserve stronger models for seats that
demonstrably benefit. On measured quality, no measured seat did** — no
candidate outscored the selected model on a measured seat. Three seats are
assigned by analogy rather than direct measurement and remain an explicit
known limitation. `risk_manager` diverges for a different reason entirely,
and only after quality had already tied: it is the gate over PM, and the tie
was spent on not sharing PM's blind spots.

It also fixed cost telemetry, which did not previously work: `estimate_cost`
could not price an OpenRouter `vendor/model` id — LiteLLM keys models by bare
vendor id, so `"openai/gpt-5.5"` matched nothing, `cost_usd` persisted as NULL
on every call, and every session pushed `$?.??`.

| | baseline | policy | cut |
|---|---|---|---|
| per trading day | $3.4334 | $0.0543 | 98.4% |
| per month (21 trading days) | **$72.10** | **$1.14** | **63.2x** |

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
  claims, and RM is told that PM calibrates against its own verdict history.
  Following PR #30's review the seats also now run **different models**. The
  veto hierarchy is unchanged.
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

## Operator item — post-merge deployment acceptance, not a PR blocker

### Run the `qamc`-account half of commissioning acceptance

Acceptance is the union of two account runs (`ops/onecli/README.md` step 4e).
The `dev` half is green. `dev` is in the sudo group but sudo requires a
password, so Claude cannot open a `qamc` session. Three checks can only be
evaluated there: config startup validation with real credentials, the runtime
CA environment variables, and trading-timer state.

**Timing matters:** do not switch the runtime checkout to an unmerged review
branch. This acceptance run happens only after PR #30 is externally accepted
and merged. Then synchronize the runtime checkout to accepted `main` and run
the canonical verifier as `qamc`:

```bash
sudo -u qamc -i
```

```bash
cd /home/qamc/quant-agent
git fetch origin
git checkout main
git pull --ff-only origin main
python3 ops/commissioning/verify_commissioning.py --live
```

Expect `COMMISSIONING ACCEPTANCE: PASS`, exit 0, and
`ACCOUNT COVERAGE: complete`. The `--live` preflight makes one real call per
distinct policy model — now **two** models, still a fraction of a cent.

The `dev` half remains the second half of the canonical step 4e acceptance
and uses `--from-onecli`; do not copy the verifier across accounts by hand.

### Resolved since the routing branch was written: OpenRouter credit

That branch recorded a hard blocker — $10 granted, $7.96 used, $2.04 left,
against a baseline model that reserves $3.84 per call at `max_tokens: 128000`,
meaning **at that time the commissioned baseline could not have started an
agent call** (402, non-retryable, no failover, session dead at the first
agent). Commissioning missed it because the preflight uses `max_tokens=512`.

The account was subsequently topped up and the blocker is cleared. The
routing policy still reduces the reservation per call from $3.84 to about
$0.05 and projected spend 63x, so the economics argument is unchanged; the
architecture docs intentionally avoid treating a transient balance as a
standing design fact.

## Verification status

| Check | Result |
|---|---|
| Full test suite | **1829 passed, 0 skipped** |
| `verify_commissioning.py --live --from-onecli` (dev) | **PASS**, exit 0 — 33 passed / 0 failed / 0 warned / 3 skipped |
| Agent routing check | PASS — all 9 seats on openrouter, per-seat map matches the accepted policy (2 distinct: `google/gemini-2.5-flash-lite`, `qwen/qwen3-235b-a22b-2507`) |
| Live provider preflight | PASS — OpenRouter served the **exact** model requested for both policy models, `finish_reason='stop'` |
| `verify_pricing.py` | 3/3 pinned rates match the live catalog |
| Alpaca | Paper only; `paper=true` asserted in config and preflight |
| Trading timers | remain disabled |
| Mission Control | remains read-only |
| Secrets | none in tracked files |
| Model spend, this round | **$0.2168** — 30-trial RM re-run ($0.1570), one smoke trial ($0.0008), and the `--live` acceptance preflights |
| OpenRouter balance at the last review-time check | **$16.76 remaining** |

The zero-skip line is itself a signal:
`test_routing_fails_when_a_seat_runs_another_seats_model` had been skipping
with "policy currently routes every seat to one model" and now runs.

## For the reviewer — what to check

**The one substantive policy change is the RM seat.** PR #30 supplied the
decision rule (quality, then independence, then latency/cost) and asked for
the measurement; the measurement produced a four-way quality tie, which
makes independence the deciding criterion and the change the rule's
consequence rather than a separate proposal. It is a one-line config edit to
reverse, and `EXPECTED_ROUTING` in `verify_commissioning.py` is the second
line if you do.

Reasons you might still reverse it, stated so they are not buried:

- The tie is thin — four models each went 6-for-6 at this seat. That is
  enough to say nothing measurable was given up; not enough to say the four
  are equivalent.
- `qwen3-235b-a22b-2507` scored 0.95 mean / 0.85 worst across the **full**
  six-scenario sweep. Irrelevant to a seat that only ever runs RM, and worth
  knowing before anyone promotes it elsewhere. The new decision-seat test
  would block that promotion until it is measured.
- Independence removes a correlated failure mode. It does not demonstrate a
  better verdict, and nothing here can.

Other limitations are in `MODEL_ROUTING_POLICY.md` — three seats assigned by
role analogy rather than direct measurement; the eight non-RM seats not
re-measured after the 2026-08-13 prompt cleanup; a benchmark-local
`max_tokens` cap that distorted 3 trials before it was found — and in
`DECISION_CHAIN_AUDIT.md` (what stays unproven until paper trading).

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
