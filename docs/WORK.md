# QAMC Current Work

Status: **TRADING-UTILITY RECOVERY — PR #56 MERGED; DEPLOYMENT PREPARED; AWAITING ONE OPERATOR (`ubuntu`) COMMAND**

## Current integration truth

- Recovery branch: `fix/trading-utility-conversion`, merged via PR #56 into `main` at `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df` (verified against GitHub directly, not assumed).
- Production is **not** yet running this recovery — still pinned at `775296e1d516279381a4c516dfb3e783b33a7495`. `docs/STATE.md` remains authoritative for the deployed SHA; it will be updated from actual post-deployment evidence, not from this entry.
- Deployment is fully prepared and verified from `dev`: target SHA/tree confirmed against `origin/main`; 30-file baseline→target delta reviewed (no unexpected content, no secret-shaped strings); all seven recovery fix markers independently confirmed present in `d14e28d` by file:line (parse containment `src/agents/base.py`, funding wait/poll `src/execution/cash_sweep.py`, schema-repair `src/agents/risk_manager.py`, skip telemetry `src/pipeline_context.py`, unfunded retry `src/pipeline.py`, FRED staleness `src/data/macro.py`, constructor provenance `src/portfolio_constructor.py`); full suite independently reproduced at **1997 passed**; a Gate-C-focused subset (the delta's own test files) independently run at **163 passed**.
- The actual rollout script is `ops/review/qamc-recovery-rollout.sh` on branch `claude/trading-utility-recovery-rollout` (pushed, not merged — matching how `ops/review/*` was handled for the prior finish-line rollout). It is a derivative of the externally reviewed `ops/review/qamc-finish-line-rollout.sh`: Gate A/B/D/E, the deployment-state machine, convergent rollback and every self-integrity check are byte-identical; only the baseline/target identity, Gate C's focused suite, and one new content-verification block (the seven fix markers, checked three times: pre-checkout, post-checkout, post-enable) differ. The existing 116-test structural suite for this script family passes unmodified against it. Full details and the single operator command are in `ops/review/README-recovery-rollout.md` on that branch.
- `dev` has no privilege to execute the rollout (account boundary: runtime is `qamc`, administration/recovery is `ubuntu`). This is the one remaining step; see the handoff for the exact command.
- A temporary branch note claiming PR #56 was unrelated to this work was based on a stale/incorrect repository view (checked before Rex updated PR #56 to point at this branch) and is superseded by current GitHub state — recorded here so it is not mistaken for a live concern.

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
2. Run the one prepared operator command (`ops/review/README-recovery-rollout.md` on `claude/trading-utility-recovery-rollout`) to deploy the exact accepted merged SHA through the existing governed rollout path, preserving Alpaca Paper authorization and the production-specific intraday enablement.
3. Verify services/timers/API/Telegram/provider wiring and the new recovery behavior from the rollout's own Gate B/C/E evidence and transcript, update `docs/STATE.md` from that evidence, then allow natural sessions to provide the actual trading evidence.

Deployment passing proves the machinery, not trading success.

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
