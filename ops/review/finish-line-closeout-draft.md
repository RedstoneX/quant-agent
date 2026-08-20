# Finish-line closeout — prepared edits (NOT YET TRUE)

**This file is not authoritative and records no state.** It is a prepared patch
for `docs/STATE.md` and `docs/WORK.md`, written before the rollout ran so that
closeout is a mechanical step rather than a fresh drafting exercise.

Nothing here may be copied into `docs/` until the operator returns a transcript
showing `GATE E / FINISH LINE PASSED`. Every `<<PLACEHOLDER>>` must be replaced
with a value read from that transcript — never assumed, never predicted.

**At closeout: apply the edits below, then delete this file.** `STATE.md` owns
current authorization and `WORK.md` owns the current handoff; this must not
survive as a third status document.

---

## Placeholders — all read from the transcript

| Placeholder | Source line in the transcript |
|---|---|
| `<<RUN_UTC>>` | the `QAMC finish-line rollout  … UTC` header |
| `<<DEPLOYED_SHA>>` | `Production SHA` in the finish-line banner |
| `<<DEPLOYED_TREE>>` | `Production tree` in the finish-line banner |
| `<<API_PID>>` | PHASE 4 `restarted (pid … -> …)` |
| `<<TIMER_COUNT>>` | E6 `exactly N quant-agent timers` |
| `<<INTRA_SERVICE>>` / `<<INTRA_TIMER>>` | PHASE 1f cadence line |
| `<<GATE_C_TESTS>>` | C2 summary, or the C2-deferred note if pytest was absent |
| `<<PREFLIGHT_RESULT>>` | B2 live-preflight line |
| `<<LOG_PATH>>` | the `full transcript:` line |

If the transcript does not contain a value, the closeout says so explicitly
rather than filling it in.

---

## 1. `docs/STATE.md`

### Replace the section "## Production code position"

> ## Production code position
>
> Production is pinned at `<<DEPLOYED_SHA>>` (tree `<<DEPLOYED_TREE>>`),
> deployed `<<RUN_UTC>>`. This is the accepted PR #48 target; it carries the
> Telegram restoration already active before the rollout, and the checkout is
> intentionally detached at that exact SHA rather than following `main`.
>
> The checkout carries exactly **one** intended local delta:
> `config/settings.yaml`, `intraday_scan.enabled: false -> true`. That is the
> authorized Stage D enablement and the only difference between the deployed
> tree and the pinned commit. Nothing else in the working tree differs.
>
> Rollback point is `9c736c158fec84129765c25a9429254d3602ad6b`. Rolling back is
> one command for code plus config and a Mission Control restart; see
> `ops/review/README-finish-line-rollout.md`.

### Replace the section "## PR #48 — accepted, rollout authorized"

> ## PR #48 — deployed and verified
>
> PR #48 is deployed and its three changes are verified on the deployed tree,
> not merely on a reviewed commit:
>
> 1. **SGOV funding semantics** — deployable cash is owned raw cash plus
>    convertible sweep value; `CashSweeper.fund_buys()` reports only the
>    CONFIRMED raw-cash increase and fails closed otherwise; execution's final
>    raw-cash recheck remains authoritative. Verified live and read-only at
>    Gate C: liquidity reconciles to the cent, the parked balance is backed by
>    a real position row, and the sweep vehicle is labelled cash-equivalent in
>    every view.
> 2. **Tech batch-response completeness** — every submitted symbol reaches an
>    explicit terminal outcome, missing results get one bounded retry, and
>    partial/failed batches are surfaced rather than silently dropped.
> 3. **Intraday opportunity discovery** — enabled (see below).
>
> Deterministic evidence: <<GATE_C_TESTS>> on the deployed commit under the
> production interpreter, against a `git archive` export so the production
> checkout was never written to.

### Replace the section "## Current authorization — finish-line paper rollout"

> ## Intraday opportunity discovery — enabled
>
> `intraday_scan.enabled: true` since `<<RUN_UTC>>`, on the **existing**
> `intra_check` cadence (`<<INTRA_SERVICE>>`, scheduled by `<<INTRA_TIMER>>`).
> No timer, service, daemon, scheduler or other durable component was added;
> `<<TIMER_COUNT>>` `quant-agent` timers remain, all active, none failed, and
> the unit set is byte-identical to the pre-rollout capture.
>
> Accepted parameters, read back from the live runtime configuration: move
> threshold 3.0%, per-symbol cooldown 3.0h, cap 5 candidates per tick.
> Concurrency is guarded by a non-blocking process `flock` and a DB-row
> concurrent-session check, both fail-closed. Current-session evidence reaches
> the Tech analyst explicitly labelled INCOMPLETE, and macro/news/earnings are
> marked `not_run_intraday` so the Risk Manager's degraded-evidence advisory
> fires honestly. Candidates go through the same Specialists → Portfolio
> Manager → AI Risk Manager → deterministic gate → execution chain the morning
> run uses; bearish expression remains the approved inverse ETFs
> (`SH`/`SDS`/`PSQ`/`SQQQ`), all confirmed present in the live universe.
>
> The scanner's own market-data path was verified live and read-only before
> enablement (`AlpacaBroker.get_intraday_snapshots`, usable previous and
> current pricing, no order placed).
>
> ## Finish-line acceptance — complete
>
> Stage E passed in the same run (`GATE E / FINISH LINE PASSED`). Verified:
> exact deployed SHA and tree with only the one intraday config line differing;
> Alpaca Paper only, asserted from the runtime config, `/health` and the
> deployed config; no shorting, options or margin path in the deployed source;
> OneCLI healthy with no QAMC-facing port on a public address; per-seat model
> routing, OpenRouter, Alpaca trading, Alpaca market data and FRED all verified
> through the gateway, plus <<PREFLIGHT_RESULT>>; database reachable;
> `<<TIMER_COUNT>>` timers healthy; wrappers sourcing the runtime environment
> and the `intra_check` cadence intact; Telegram `getMe` 200 with the real token
> only in OneCLI and no token-shaped string in the runtime log; `/cockpit`,
> `/ui` and `/health` all 200 and Mission Control GET-only; the decision chain
> and every intraday guardrail present; account boundaries intact.
>
> Transcript: `<<LOG_PATH>>` on the VPS (root-only, 0600). No secret appears in
> it.
>
> **Not gated on, deliberately not manufactured:** the first live Tech batch log
> line and the first live intraday tick are post-rollout soak observations. The
> accepted completion condition is operational readiness and verified wiring,
> not a trade.

### Add to "## Not authorized" — no change needed

The existing list is still correct and complete.

---

## 2. `docs/WORK.md`

Replace the whole file. The finish-line contract is discharged; `WORK.md` owns
only the current/next work state.

> # QAMC Current Work
>
> Status: **FINISH LINE REACHED — PAPER SOAK RUNNING ON THE ACCEPTED TARGET**
>
> The stage-gated finish-line rollout is complete. Production runs
> `<<DEPLOYED_SHA>>` with intraday opportunity discovery enabled on the existing
> `intra_check` cadence, and Stage E acceptance passed in the same run. See
> `docs/STATE.md` for the accepted state and the rollback point.
>
> ## Current work
>
> Observe the soak. The two pieces of evidence the rollout deliberately did not
> manufacture are now simply awaited:
>
> 1. the first live Tech batch log line (`Batch: N/M symbols analyzed`) from the
>    next scheduled research run;
> 2. the first live `intra_check` tick with the scanner enabled, inside
>    09:30–16:00 ET on the next weekday.
>
> Neither is a gate. If either shows an unexpected shape, that is ordinary
> in-scope debugging against the accepted architecture.
>
> ## Carried forward — bounded, non-blocking
>
> **`deployable_cash` names two different quantities.** The read-only API's
> `LiquidityBreakdown.deployable_cash` is raw cash free of the reserve floor;
> the engine's `TradingPipeline._compute_deployable_cash` is raw cash plus the
> convertible sweep value. Both are individually correct and
> `total_liquidity` already carries the engine's figure, but an operator reading
> the API field alone understates QAMC's real capacity by the parked balance.
> `tests/test_api_contract.py` pins the API/engine equivalence so they cannot
> drift silently, and `LiquidityBreakdown`'s docstring states the collision.
>
> The preferred correction — renaming the field to `raw_cash_free_of_reserve`
> and/or adding an explicit `engine_deployable_cash` — is a public read-only API
> schema change consumed by the compiled cockpit bundle, and changing the
> field's value in place would break the liquidity donut's composition. It needs
> a coordinated frontend rebuild and browser re-verification, so it is a scoped
> product decision rather than a rollout fix. Not started.
>
> ## Hard boundaries
>
> Unchanged: Alpaca Paper only; no margin, options or direct stock shorting;
> bearish expression through the approved inverse ETFs; deterministic
> Python/broker protections final; Mission Control read-only and private;
> Telegram output-only; no new durable infrastructure; `dev`/`qamc`/`ubuntu`
> boundaries preserved; Claude does not merge its own work.

---

## 3. Housekeeping at closeout

- Delete this file.
- `ops/review/qamc-finish-line-rollout.sh` and
  `ops/review/README-finish-line-rollout.md` are spent artifacts once the
  rollout has run. Keep them until ChatGPT has integrated the branch — the
  transcript references them — then remove them in the integration commit. Git
  history preserves both.
- `tests/test_rollout_script.py` should be removed in the same commit that
  removes the script it tests; it skips cleanly if the script is absent, so
  ordering is not fragile.

---

## 4. Next product tranche — identified, NOT started

Recorded so the finish line does not silently roll into new work. Each needs
its own authorization in `docs/STATE.md` / `docs/WORK.md`.

1. **Soak-evidence review of the enabled scanner.** After a week of live
   `intra_check` ticks: how often did anything qualify, what did the cooldown
   and cap actually suppress, did any candidate reach PM, and what did the
   added LLM/market-data calls cost against the projected budget. This is the
   natural next tranche and the only one with a standing reason to be first.
2. **The `deployable_cash` rename plus cockpit rebuild** described above.
3. **Mission Control liquidity presentation.** Related but larger: the cockpit
   currently answers "how much raw cash is free" where the operator is asking
   "how much can QAMC put to work".
4. **The three model seats assigned by analogy** (`earnings_analyst`,
   `evening_analyst`, `meta_reflector`) remain unmeasured — a known limitation
   in `MODEL_ROUTING_POLICY.md`, not a defect.

Not recommended as next work: anything that widens trading authority. The
finish line was reached without a single new trading capability, and the soak
should produce evidence before the surface grows.
