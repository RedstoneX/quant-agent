# QAMC Current Work

Status: **FINISH-LINE PAPER PRODUCTION ROLLOUT AUTHORIZED — ORCHESTRATED, STAGE-GATED EXECUTION**

This file is the current work/handoff contract only. Historical detail belongs in Git history and accepted architecture/verification records.

## Goal

Take QAMC from the current pinned production state to the intended **finished paper-trading operating state** in one outcome-driven tranche, without returning to operator micro-approvals between routine engineering stages.

Finish line:

- production runs the accepted PR #48 fixes plus the already-active Telegram restoration;
- SGOV funding correctness and Tech batch completeness are active and verified;
- `intraday_scan` is enabled on the existing `intra_check` cadence **only after** its production gate passes;
- the existing Specialist → Portfolio Manager → AI Risk Manager → deterministic Python/broker chain remains intact;
- bearish opportunity discovery continues through the already-approved inverse ETFs;
- all existing scheduled paper-trading paths, Mission Control, OneCLI, broker/data/model connectivity and Telegram notifications are healthy;
- no live trading, margin, options, direct stock shorting, public exposure or new durable infrastructure is introduced;
- final production SHA/config and evidence are recorded cleanly in `STATE.md` / `WORK.md`.

## Execution model — orchestrator owns the run

Claude is authorized to create a **lead/orchestrator subagent** for this tranche. The orchestrator should maintain the stage ledger, delegate bounded work to specialist subagents where useful, independently verify evidence, and advance through the stages when objective gates pass.

The orchestrator may approve routine internal stage transitions without returning to the operator. A failed gate should trigger investigation/fix/retest within the accepted architecture, not a new operator decision.

Stop for the operator only when genuinely required by:

- `ubuntu`/sudo privilege or another action unavailable to `dev`/Claude;
- a real credential/secret that only the operator can supply;
- a material architecture/safety conflict or a product/value fork not already decided here;
- an irreversible action outside the accepted paper-trading boundary.

When operator privilege is required, bundle the required actions into **one concise, guarded script/command** wherever safe, then continue the same tranche after the output is returned.

## Authorized stages

### Stage A — rehydrate, pin, preflight

- Read `CLAUDE.md`, `docs/STATE.md`, this file, `docs/OUTCOME.md`, and only the relevant accepted architecture/runbooks.
- Inspect current GitHub `main`, current production `9c736c1`, and the exact diff between them.
- Pin one exact deployment SHA; do not deploy a moving branch tip blindly.
- Verify that the production delta is limited to already accepted work: PR #48, Telegram restoration already active in production, and documentation/governance changes. If unrelated code has appeared, stop for external reconciliation.
- Run the appropriate full/focused tests and safety checks before touching production.
- Capture an exact rollback SHA/config path.

**Gate A:** target diff understood, tests green, rollback explicit, Alpaca Paper and deterministic safety boundaries unchanged.

### Stage B — production converge with intraday still OFF

- Deploy the exact pinned accepted target to the `qamc` production checkout using the existing deployment/account boundaries.
- Preserve OneCLI Telegram credential injection and existing `.env` secret hygiene.
- Keep `intraday_scan.enabled: false` during this stage.
- Restart/reload only what the existing deployment model requires; do not create new services/timers.
- Verify Mission Control/API, database, Alpaca Paper, OpenRouter/model routing, OneCLI, scheduled wrappers/timers and Telegram notification path.

**Gate B:** production healthy on the pinned target; PR #48 SGOV and batch fixes active; Telegram still works; intraday remains disabled; no safety regression.

### Stage C — verify PR #48 behavior

Prove, without fabricating production state:

- SGOV remains cash-equivalent sweep parking rather than a PM thesis;
- deployable-liquidity accounting is correct;
- `CashSweeper.fund_buys()` only reports confirmed raw broker-cash increase;
- execution's final raw-cash gate remains authoritative;
- Tech batch processing cannot silently drop submitted symbols and exposes terminal failure/partial state honestly;
- Mission Control/journal observability remains truthful.

Use deterministic tests, safe paper/account evidence and read-only production inspection. Do not force a market trade merely to manufacture evidence.

**Gate C:** no blocker in SGOV funding or Tech batch completeness; production health remains green.

### Stage D — enable intraday opportunity discovery

This stage is **explicitly authorized by the operator now**; it no longer requires a separate future approval if Gates A–C are green.

- Enable `intraday_scan` using the already accepted implementation/configuration and existing `intra_check` cadence.
- Do not add a new timer/service/daemon.
- Preserve cooldown/dedup, concurrency guards, current-session incomplete-data labeling, and the same Specialist → PM → AI Risk → deterministic gate → execution path.
- Preserve inverse-ETF bearish expression only; no direct shorting/options/margin.
- Verify the enabled configuration and scheduled path without forcing a trade.

**Gate D:** scanner enabled and reachable on the existing cadence; concurrency/safety controls intact; no new trading authority introduced.

### Stage E — end-to-end operational acceptance

Run a final adversarial verification pass across the whole paper system:

- exact deployed SHA and clean production checkout;
- Alpaca Paper only;
- OneCLI/provider/broker/FRED/DB health as applicable;
- seven existing timers/scheduled wrappers healthy;
- Telegram notification delivery healthy without exposing the bot token;
- Mission Control `/cockpit` and `/ui` healthy/read-only;
- SGOV funding fix present;
- Tech batch completeness fix present;
- intraday scan enabled with accepted guardrails;
- deterministic risk/execution remains final authority;
- no live trading, margin, options, direct stock shorting, public QAMC/OneCLI exposure, account-boundary collapse or new durable infrastructure.

Do not wait for an actual market opportunity or force a trade as a completion condition. The finish line is **operational readiness and verified wiring**, not guaranteed order generation.

**Gate E / goal post:** all checks green or any remaining uncertainty is explicitly bounded and non-blocking.

## Hard boundaries

- Alpaca **Paper only**.
- No margin, options or direct stock shorting.
- Bearish expression remains through existing approved inverse ETFs.
- Deterministic Python/broker protections remain final.
- No broker-write Mission Control controls.
- Telegram remains output-only; no command/control plane.
- No new daemon/service/database/proxy/security/credential architecture without a new architectural decision.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Do not expose QAMC or OneCLI publicly.
- Do not weaken OneCLI secret handling or write the real Telegram bot token to `.env`/logs.
- Do not redesign trading/risk semantics merely to make a gate pass.
- Do not force or manufacture paper trades for verification.

## Git / review boundary

Claude may implement, test, deploy already-merged accepted code, repair in-scope defects found by the gates, and push a dedicated review branch. Claude must **not merge its own new repository work to `main`**. Any new code/documentation changes discovered during this tranche must be committed/pushed for external ChatGPT review/integration.

Routine stage approval is the orchestrator's job; GitHub merge approval remains ChatGPT's job.

## Current starting point

- Production: `9c736c158fec84129765c25a9429254d3602ad6b` (`9c736c1`).
- Telegram: active; real bot token only in OneCLI; non-trading delivery verified.
- PR #48: merged to `main`, not yet deployed.
- `intraday_scan.enabled`: false.
- Alpaca paper soak: active.

Proceed to the goal post as one coordinated tranche. Do not stop after each successful stage merely to ask permission to continue.