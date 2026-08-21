# QAMC Current Work

Status: **TRADING-UTILITY RECOVERY — DEPLOYED AND VERIFIED; AWAITING NATURAL PAPER-MARKET EVIDENCE**

## Current integration truth

PR #56 (trading-utility recovery) is merged to `main` and deployed to production at `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`, via the governed rollout script (`ops/review/qamc-recovery-rollout.sh` on `claude/trading-utility-recovery-rollout`), run by the operator as `ubuntu` on 2026-08-20 23:58:56 UTC and ending `GATE E / FINISH LINE PASSED`. Independently corroborated live from `dev` immediately after (health `ok`, `paper=true`, `/cockpit`/`/ui` 200). Full deployment evidence, the two review rounds' defects and fixes (baseline-state handling, a newline asymmetry, a block-scan gap, a trailing-comment asymmetry, and a silent-second-signal gap in convergence — all found and fixed before any rollout ran, all with dedicated regression tests, 150/150 passing against the exact committed GitHub bytes) are in `docs/STATE.md` and `ops/review/README-recovery-rollout.md`; not duplicated here.

Deployment proves the machinery is wired correctly. It does not prove the recovery works — see Goal and Natural validation required below.

## Product/architecture principle

QAMC is an autonomous Alpaca trading system, not a separate “paper-trading architecture.” Alpaca Paper is the **currently authorized execution environment** used to validate the system before any future live-capital authorization.

The same trading-critical architecture must apply across environments:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → funding → broker execution → position/exit management → reflection.**

Paper/live differences belong at the broker/configuration boundary (credentials, endpoint/account selection and genuine execution-mechanics differences). Do not create paper-specific decision logic, weaker safety, alternate position management, or shortcuts that would require a later live-trading rearchitecture.

## Recovery finding and accepted changes

Production forensics across the natural validation runs found that QAMC was often analyzing legitimate opportunities without converting them into exposure for mechanical reasons rather than deliberate investment judgment. The reviewed recovery addresses the evidenced blockers:

1. **PM parse destruction** — nested `targets` fragments could outscore and replace the complete PM decision. Parser selection is corrected and production payloads are regression-pinned.
2. **SGOV funding race** — approved BUYs could be abandoned before the funding SELL completed. Funding now waits/polls within a bounded fail-closed window and execution still requires confirmed broker cash.
3. **Schema-complete decisions rejected** — approving PM/Risk decisions could die on missing narrative schema fields. One bounded repair is allowed only for non-decision fields; decision-bearing content is strictly preserved or the run fails closed.
4. **Invisible execution kills** — deterministic BUY skips were log-only. Skip reasons are now durable evidence, surfaced in the funnel, and fully unfunded approved mornings become safely retryable through the full decision chain.
5. **Artificial macro conservatism** — transient FRED failures and impossible freshness rules suppressed confidence. FRED gets bounded retry/breaker behavior and staleness now follows each series' actual cadence.
6. **Risk misread deterministic sizing** — constructor risk-budget caps are now explicitly identified so AI Risk does not mistake deterministic sizing for PM inconsistency.

Full branch suite reported after final review fixes: **1997 passed**.

## Goal

After merge and governed deployment, use natural market evidence to determine whether QAMC reliably:

**finds opportunity → evaluates it → makes a defensible bullish, bearish or neutral decision → executes when eligible → manages/exits the position → measures the result.**

Success is **not** “more trades.” Do not force activity, weaken safety, or hindsight-tune. When QAMC does not trade, the reason must be specific and defensible.

## Next authorized work

1. ~~Complete external GitHub integration of PR #56.~~ Done — merged to `main` at `d14e28d`.
2. ~~Deploy the exact accepted merged SHA through the existing governed rollout path.~~ Done — `GATE E / FINISH LINE PASSED`, verified live, recorded in `docs/STATE.md`.
3. Observe natural Alpaca Paper sessions against the criteria in Natural validation required, below. No forcing, no manufacturing, no hindsight tuning. This is passive observation, not an implementation task — the next engineering work is whatever the observed evidence justifies.

Deployment passing proved the machinery, not trading success.

## Natural validation required

Before declaring the recovery successful, natural market sessions should demonstrate:

- worthwhile opportunities can survive discovery and reach PM/Risk;
- defensible eligible trades can reach funded broker submission;
- supported bearish opportunities can be expressed through the approved inverse ETFs;
- no-trade decisions remain possible and explainable;
- position management and exits behave coherently after entry;
- execution/funding failures are visible rather than silently interpreted as investment decisions;
- observed performance and missed opportunities can be measured without hindsight tuning.

## Remaining uncertainty

The fixes are strongly supported by recorded production evidence and deterministic tests but have not yet been validated as a complete chain after deployment. Two lower-priority observed issues remain outside this recovery gate unless they materially distort validation: news-narrative factual drift and `actual_provider` attribution oddity.

## Secondary product debt

Mission Control still has semantic/usability debt, including candidate/run attribution and liquidity presentation. Read-side correctness needed for trading diagnosis is valid; broad dashboard redesign remains secondary to proving trading utility.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.** Live-broker order submission requires a separate future authorization; this is an environment/safety gate, not a separate trading architecture.
- No margin, options or direct stock shorting. Bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards merely to increase activity.
- Do not introduce paper-only trading semantics that would need replacement for live-capital operation.
- No new daemon/service/database/proxy/security/credential architecture without explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- Claude does not merge or deploy its own work; external integration and governed deployment remain required.
