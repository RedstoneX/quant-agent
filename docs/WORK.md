# QAMC Current Work

Status: **TRADING-UTILITY RECOVERY — FIXES IMPLEMENTED, AWAITING EXTERNAL REVIEW**

## Deployment task blocked — PR #56 does not match this work (2026-08-20)

**Finding:** A deployment task asked Claude to carry "the merged PR #56" (trading-utility recovery, described as having "passed external review") through the governed production deployment path. Repository evidence contradicts the premise on every count checked; no deployment was performed.

**Evidence:**
- GitHub PR #56 for this repo (commit `92420b5`, squash-merged into `claude/vps-deployment-hardening-q3f7k2`) is `fix(execution): defer protection finalize to actual fill (P1 codex r4)` — an unrelated execution/protection-finalize fix (part of the separate PR I/J/K = #55/#56/#57 series). It has nothing to do with trading-utility recovery.
- `fix/trading-utility-conversion` (this branch, tip `4f7191c`) has no associated PR at all and is not an ancestor of `origin/claude/vps-deployment-hardening-q3f7k2` (`git merge-base --is-ancestor` → false). It has never been merged anywhere.
- This file's own Status line above — last updated on this branch at commit `c6bc024` — already read "AWAITING EXTERNAL REVIEW" before this deployment task arrived.
- No commit, branch, or doc anywhere in this repository's history associates "PR #56" (or any PR number) with trading-utility recovery.

**Decision:** Treated as a material conflict with accepted architecture/safety/scope (CLAUDE.md decision discipline) and the standing "Claude does not merge or deploy its own work; external review remains required" boundary (Hard boundaries, below). Did not deploy, merge, open a PR, or touch production. `docs/STATE.md`'s production pin is untouched and remains accurate to what is actually running.

**Change:** None to code or production — this record only.

**Remaining uncertainty:** Whether "PR #56" was simply a misidentification (the next sequential PR number was assumed rather than checked — #55/#56/#57 were in concurrent use by the unrelated execution-protection fix series, so this recovery's real future PR number would be #58 or later), or whether external review actually happened through a channel this repository doesn't record. Before any deployment proceeds: open an actual PR from `fix/trading-utility-conversion` against the correct base, get real external review, merge it, then update `STATE.md`/`WORK.md` to the true reviewed SHA.

## Recovery record (2026-08-20, branch `fix/trading-utility-conversion`)

Census of all 13 decision runs since soak start (Aug 14–20): ~80–96 candidates per run, $0.71 total model spend, **zero risk positions ever opened**. Five root causes found, all evidence-backed, all fixed within the accepted architecture:

1. **Finding:** 10/13 runs recorded "no trades" although the PM emitted complete valid plans. **Evidence:** deterministic replay of recorded payloads — `parse_json` scored the plan's inner `targets` array (5 pts/symbol) above the containing decision object (weights table still keyed on pre-constructor `decisions`); every plan with ≥8 targets was destroyed by a fragment of itself and retried at full research cost every 30 min. **Change:** `targets` key weight + nested-fragment containment filter + explicit non-dict guard (commit `0102aa9`). **Verification:** both killed production payloads (11 and 17 targets) now parse end-to-end; array/correction/wrapper behaviors regression-pinned.
2. **Finding:** the one fully-approved plan (8/19, XLE/XLF/XLI ≈$1,417) died between funding and broker. **Evidence:** broker order history + runtime log — SGOV funding sell filled 51s after submit; the 15s wait gave up, account was read pre-fill ($145.11), all three BUYs skipped as unfunded, proceeds landed 36s into a dead session, midday re-parked them. Alpaca forum/docs confirm NBBO-quote-driven fills make such latency normal in paper AND live. **Change:** funding sell gets a 180s terminal wait + 30s cash-settle poll; fail-closed unchanged (commit for `cash_sweep`). **Verification:** slow-fill/never-fills/broker-down tests; raw-cash execution gate untouched.
3. **Finding:** 8/18 15:03 the RM **approved** the plan with three sound halvings and the day still ended "REJECTED: parse error". **Evidence:** runtime log — verdict omitted two mandatory reasoning-chain prose fields; validation failure nulled the approving verdict. **Change:** one bounded schema-repair reprompt (RM + PM seats) naming the exact validation errors; double failure keeps the fail-closed reject path (commit `2809cb0`). **Verification:** repair/fail-closed/garbage tests built from the recorded broken verdict.
4. **Finding:** every execution-phase BUY skip was log-only; the funnel showed `proposed_not_executed` with no reason and the evening analyst graded mechanically-killed days as "need more proactive idea generation". **Change:** every skip site persists `execution_skip` evidence + `ctx.execution_skips`; a morning whose approved BUYs all died unfunded returns retryable `buys_unfunded` (fresh full chain incl. RM re-review — no veto bypass); funnel API surfaces skip reason/detail. **Verification:** skip-telemetry tests incl. persistence-failure-never-blocks; funnel contract test.
5. **Finding:** macro conservatism was partly hard-wired. **Evidence:** 14 transient FRED timeouts across the soak (5 series died in one run on 8/20 → "critical missing data", 55% cash); prompt + code demanded ≤3d staleness from *monthly* series, making high confidence structurally unreachable while runs cited "stale inflation" on the freshest print that exists. **Change:** bounded FRED retry with outage breaker; per-cadence staleness in `_stale()`, prompt calibration rules, and the sanity floor (monthly stale only past a missed release cycle). Daily discipline unchanged. **Verification:** retry/breaker/reset tests; per-cadence sanity tests.
   Plus: the 8/20 nuclear veto misread the constructor's deterministic risk-budget cap (PM 15% → order 10.65%) as PM dishonesty — capped orders now carry a `[constructor: …]` provenance note and the RM prompt says to audit the order as presented (no discipline change).

**Remaining uncertainty:** fixes verified against recorded payloads and unit/contract tests, not yet against a naturally-scheduled live session; RM prompt-compliance (veto-vs-modify) is mitigated via evidence correction, not guaranteed; the news-narrative state tracker has drifted factually (claims Fed tightening at 5.33% while DFF=3.63%) — observed, not yet fixed; `actual_provider` telemetry labels OpenRouter answers "anthropic" — attribution oddity, unfixed; the enabled intraday scanner has not yet naturally triggered (no ≥3% move — correct by design). Next natural morning sessions are the live verification.

The finish-line rollout is complete and production remains on the accepted Paper-only target. The current priority is no longer passive soak observation: it is to determine why QAMC performs substantial analysis but rarely converts legitimate opportunities into meaningful market exposure, and to correct the causes that are justified by evidence.

## Goal

Using natural Alpaca Paper evidence, make QAMC reliably:

**find opportunity → evaluate it → make a defensible bullish, bearish or neutral decision → execute when eligible → manage/exit the position → measure the result.**

Success is **not** “more trades.” Do not force activity, weaken safety, or tune to hindsight. When QAMC does not trade, the reason must be specific and defensible.

## Authorized work

Claude has broad autonomy inside the accepted architecture to investigate and improve the full path:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → SGOV funding → broker execution → position/exit management → reflection.**

Start from real production/paper evidence. Quantify where candidates disappear or are vetoed. Fix evidence-supported causes such as discovery/ranking quality, stale or incomplete evidence, prompt/decision paralysis, configuration that is unintentionally too restrictive, PM/Risk interaction problems, funding/execution defects, and position-management defects.

Do not preserve a prior choice merely because it already exists if it is preventing the product outcome. Material architecture or safety changes still require operator approval.

## Required evidence

At each meaningful checkpoint, keep the durable record concise:

**Finding → evidence → decision → change → verification → remaining uncertainty.**

Use Git commits/PR history for detail; do not create another governance/status system.

Before declaring the recovery successful, demonstrate from natural paper-market evidence that QAMC can detect and act on worthwhile opportunities in both supported bullish and bearish conditions, while also producing defensible no-trade decisions when appropriate.

## Secondary product debt

Mission Control has serious semantic/usability issues, including candidate/run attribution and misleading liquidity presentation. Fix read-side correctness required to diagnose trading behavior, but defer broad dashboard redesign until the trading-utility root causes are understood.

## Hard boundaries

- Alpaca **Paper only**.
- No margin, options or direct stock shorting.
- Bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards merely to increase activity.
- No new daemon/service/database/proxy/security/credential architecture without explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- Claude does not merge or deploy its own work; external review remains required.
