# QAMC Engineering Contract

Current engineering lead: **Codex**. This contract is intentionally agent-neutral so any capable engineering agent can follow the same rules.

## Start

Read `docs/STATE.md`, then `docs/WORK.md`. Use `docs/OUTCOME.md` for product intent and only the accepted architecture/contracts relevant to the task. `docs/FUTURE_*` is conceptual only.

Do not trust a document's claimed status at face value — check reality first: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1`.

## Paper-beta autonomy

While QAMC remains Alpaca Paper, already-authorized engineering may run end-to-end without a human review/merge/deploy gate:

**diagnose → implement → test → inspect → PR → merge → deploy → verify → rollback if needed**

Use two active accounts until QAMC is stable. `ubuntu` is engineering/operator: Codex sessions, checkouts/worktrees outside `/home/qamc`, Git/GitHub, dev tooling, tests/builds, private preview/browser work, Docker/sudo engineering tasks, and deployment orchestration. `qamc` is runtime-only: it owns `/home/qamc/quant-agent`, runtime `.env`/OneCLI wiring, services/timers, and QAMC Paper execution — never run Codex as `qamc` or turn it into a general engineering account. Keep `dev` parked: no normal use and no permission expansion during stabilization.

This fast lane does not authorize live capital, paid dependencies, secrets/credential redesign, destructive infrastructure replacement, or material architecture outside current authority.

## Parallelism — systemwide engineering policy

Use parallel workers/subagents proactively when independent work can safely run at the same time and doing so shortens the critical path.

- Parallelize independent investigation, code surfaces, targeted tests, logs/evidence, browser/visual verification and documentation checks.
- The lead agent owns integration and resolves conflicting findings.
- Avoid duplicate fan-out, repeated fact-finding, or overlapping writes without clear ownership.
- Use separate worktrees when they materially simplify independent implementation.
- Use the strongest available reasoning model for architecture, trading logic, safety-sensitive changes, hard debugging, difficult review and ambiguous UX/product judgment.
- Use cheaper/faster workers for bounded tests, searches, inventories, log parsing and mechanical evidence collection.
- Escalate a cheap worker when the work becomes ambiguous or reasoning-heavy.

Parallelism is an efficiency tool, not an agent-count target.

## Hard boundaries

- Alpaca Paper only; live-broker order submission needs separate explicit authorization.
- Preserve **Specialists → Portfolio Manager → AI Risk → deterministic Python → broker**.
- Deterministic risk/broker protections remain final authority and fail closed.
- Mission Control/API/journal/search/UI remain read-only and non-critical to trading unless accepted work explicitly changes that. Telegram remains output-only.
- Do not expose secrets or fabricate production state. Preserve OneCLI secret handling. No public exposure of QAMC or OneCLI.
- Existing trading records remain canonical; UI/journal/search projections are derived and must not become a second trading-memory system.
- Before trading-core changes, read `docs/architecture/SAFETY_BOUNDARIES.md`.
- For API/read-side changes, preserve the isolation contract in `docs/architecture/MISSION_CONTROL_API.md`.
- Do not force or manufacture trades, or weaken safeguards to increase activity.
- Do not create paper-only trading semantics — no code path that behaves differently "because it's paper."
- No new daemon/service/database/proxy/security/credential/orchestration architecture, and no paid alternative-data dependency, without separate explicit approval.

## Execution discipline

- Prefer outcome-driven work over micro-prompts.
- Run the narrowest decisive test first; broaden only when evidence requires it.
- Do not re-read unchanged authority or re-prove settled facts.
- UI/frontend acceptance requires rendered desktop and iPad inspection; tests/builds alone are insufficient.
- Stop when the result is proven. Re-validation without new evidence is waste.
- Keep handoffs short: **changed / verified / preview if relevant / unresolved blocker / production state**.
- Do not infer current defects from historical notes. Reopen a resolved area only from current operator or production evidence.
- Private preview/browser verification is standing-authorized for relevant engineering work.

## Git and continuity

Use dedicated branches/PRs for substantive work. Do not force-push or push implementation directly to `main`. Paper-beta autonomy includes merging the agent's own verified PR and deploying it. Keep rollback possible. Bundle production preflight/deploy/restart/acceptance into the shortest safe intervention.

## Decisions ratified

- Stops were too tight and that was the root cause of two separate failures. The ATR multiple must scale by setup type and macro regime — never a hardcoded constant.
- Real short selling, not inverse ETFs. Three stages: countable, safe, live.
- No dev/prod mirror. Production is paper and resets, so the case for enterprise staging collapses. Build the rehearsal harness instead.
- The system already sends marketable limit orders, which is a market order with a bounded worst case. No change needed.
- Documentation is the source of truth. Wrong documentation is corrected on sight without asking.
- Rehearsal alerts are suppressed rather than routed to a second Telegram bot.

## Shipped tranche — Smart Money Analyst & research/reading experience

This is the standing acceptance contract for a tranche that has already shipped. Kept as the yardstick for what "done" means here, not as open work.

### Smart Money Analyst outcome

Use first-party, credentialless SEC data for v1. Phase A is broad Form 4
discovery of exact non-derivative open-market purchase/sale codes `P` and `S`,
with accession-level provenance, transaction time, SEC acceptance time, lag,
owner identity/role, amendment and 10b5-1 context where present. Python owns
parsing, direction, recency, materiality, independent-owner clustering,
deduplication and admission eligibility. Quiet or unchanged evidence must use
zero model tokens; the LLM sees only compact surviving evidence and may
synthesize meaning but cannot author source facts or admission.

The permanent configured universe remains unchanged. A fresh external `P`
purchase that clears the higher external materiality threshold may be admitted
for the current run only after deterministic Alpaca common-US-equity/tradable
eligibility, supported-exchange, minimum-price, minimum-history, minimum
20-session dollar-liquidity and known-sector checks. At most three external
symbols may be admitted per run. Admission only adds the symbol to that run's
research/PM allowlist; it must still receive current Technical analysis and
pass Portfolio Manager grounding, AI Risk, every deterministic risk/funding
rule and broker protection. It is never written into the permanent universe.

Schedule 13D/13G and curated-manager 13F deltas remain possible later phases,
not v1 admission inputs. Alpha Vantage may be considered only as an optional
cross-check/fallback; Bargo may be reconsidered if access arrives. Neither is
a current dependency. Paid alternative-data dependencies remain unauthorized.

The Smart Money Analyst should identify **viable present-tense trading evidence**, not merely summarize disclosure feeds. It must distinguish evidence by freshness and economic meaning. Congressional trades can be disclosed up to roughly 45 days after the transaction and 13F holdings can be filed up to 45 days after quarter-end, so those streams are primarily thematic/confirmatory context. SEC Form 4 insider transactions are generally filed within two business days and are materially more timely. Any genuinely real-time/near-real-time stream made available under the accepted free source may be treated according to its actual timestamp and provenance.

The seat should intelligently suppress noise and surface only material patterns: clustered or repeated activity; unusual size/direction relative to the available disclosure; multiple independent smart-money streams aligning; activity that confirms or contradicts current News/Macro/Earnings/Technical evidence; and fresh evidence that changes the current thesis. A lone stale politician transaction is not a trade signal. Every surfaced finding must state what happened, when it happened, when it became knowable, why it matters now, and whether it is actionable, confirmatory, contradictory or merely historical.

If other genuinely free, reliable, source-backed smart-money streams are available under acceptable terms, Codex may incorporate them into this same seat rather than proliferating agents. Provider/API details should remain replaceable rather than becoming trading architecture.

The new seat may inform the Portfolio Manager through the existing specialist-evidence path. It must not bypass PM, AI Risk, deterministic Python or broker protections and must not create a new execution path.

### Research/reading experience outcome

Desktop should have a strong designed default composition, then let the operator rearrange it. Reuse Dockview so panels can move, resize, tab, maximize and persist their layout. iPad should be composed for reading, not squeezed from desktop.

The writing should be **compact but substantive**. Short sentences. Strong editing. No filler, repeated conclusions, forced jokes, fake quotes or generic AI throat-clearing. Wit should come from judgment, not punchlines. Quiet days should stay quiet.

Use visual structure where it genuinely helps:
- **signal stack** — quick agreement/conflict across relevant agents;
- **what changed** — the new information since the prior useful read;
- **tension** — the most important disagreement or contradiction;
- **why now** — why the item deserves attention today;
- **evidence strip** — compact factual chips instead of prose where possible;
- **mini chart/sparkline** — only when it adds immediate market context;
- **Read / PM / Risk** — clearly separated judgment, portfolio implication and risk consequence;
- **dry annotation** — occasional, restrained, evidence-based commentary when the situation earns it.

Do not force every device onto every card. The point is rhythm and hierarchy, not decoration. One important story may be visually dominant while supporting research is smaller. Balance matters more than symmetry.

Favor useful editorial synthesis such as daily market thesis, agent findings, disagreement, Smart Money evidence, PM ruling, Risk response, proposed-versus-executed delta, position review, after-the-bell lessons and tomorrow watch. Raw structured evidence remains secondary drill-down.

No fabricated confidence, quotes, history or facts. Sparse, stale, partial, no-news, no-trade and provider-error states must look intentional and remain truthful.

### Acceptance

This tranche is complete when real stored QAMC data demonstrates that:

1. An operator can read a coherent daily story without opening logs or JSON.
2. Every relevant agent has a useful, visually balanced representation of its findings, strongest evidence, meaningful changes and disagreement where supported.
3. The writing is substantive without being verbose, visually scannable, and has a restrained private-desk personality rather than corporate/LLM prose.
4. Signal stacks, change markers, tension, why-now context, evidence strips, mini-chart context, Read/PM/Risk separation and occasional dry annotations are used where they improve comprehension rather than mechanically everywhere.
5. PM/Risk/execution are understandable as deltas: what PM wanted, what Risk changed, what deterministic code allowed/blocked, and what actually executed.
6. Desktop research panels are genuinely movable/resizable/tabbable/maximizable with persisted layout and a sensible default workspace.
7. iPad has a deliberately designed reading/navigation experience with no horizontal overflow or micro-text.
8. Smart Money Analyst is SEC-source-backed, accession/timestamp/lag-aware,
   attributable, direction-validated, noise-suppressing, and reaches PM only
   through the accepted specialist path. Any external symbol is run-scoped,
   visibly admitted by deterministic evidence, and still traverses the full
   Technical → PM → AI Risk → deterministic gate → broker chain.
9. Empty, stale, partial and provider-error states are truthful and visually composed.
10. Targeted tests/build pass and rendered desktop+iPad visual acceptance passes with zero console/page errors and no horizontal overflow.

### Engineering posture for this tranche

This was outcome-driven work: autonomy to inspect the repository, choose the simplest implementation consistent with accepted architecture, make routine engineering/design decisions, implement, test, visually inspect, commit, push, merge, deploy and verify production under the standing Paper-beta workflow — without splitting into micro-PRs or over-specifying implementation from the handoff. The same stop conditions as `## Paper-beta autonomy` applied: a genuine unresolved product/safety/architecture conflict, a paid dependency, a live-capital boundary, or an external credential/authorization requirement that could not be satisfied from existing project resources.

## Document authority

Two tiers. Know which one you are reading before you trust it.

**Tier 1 — the authority stack. Closed to additions.**

1. `AGENTS.md` — the engineering contract and operating rules.
2. `docs/STATE.md` — accepted current truth about the system.
3. `docs/WORK.md` — active work, including the ordered backlog that a cold session resumes from.
4. `docs/OUTCOME.md` — the product mandate and intended outcome.
5. `docs/PROJECT_COMPASS.md` — human-facing compass. Human-only; agents do not rewrite it.

No agent may add a sixth. Proposals to change any of these follow the ratification rule below.

**Tier 2 — reference documents. Subordinate, non-authoritative, consumable.**

Analysis artefacts such as `docs/QAMC_REMEDIATION_SPEC.md`, `docs/AGENT_ROLE_AUDIT.md` and `docs/RESEARCH_FINDINGS.md`. They exist to carry findings and plans that would otherwise be lost between sessions.

- A reference document is **never** cited as truth over Tier 1. Where they disagree, `STATE.md` wins and the reference document is stale.
- They have a **lifecycle**: they empty as their contents are implemented, and are **deleted when consumed** rather than maintained indefinitely.
- Creating one requires a reason that Tier 1 cannot serve. "It felt like reference rather than governance" is not that reason — that argument can justify anything, which is precisely why this section exists.
- A reference document may never quietly become a source of truth. If its content belongs in Tier 1, move it there and delete the original.

**Tier 3 — technical reference. Durable.**

`docs/architecture/*` — how a subsystem actually works: `SAFETY_BOUNDARIES.md`, `MISSION_CONTROL_API.md`, `MODEL_ROUTING_POLICY.md`, `MODEL_PROVIDER_ARCHITECTURE.md`, `DECISION_CHAIN_AUDIT.md`, `CREDENTIAL_DELIVERY_EVIDENCE.md`.

Long-lived, unlike Tier 2 — they are maintained rather than consumed, and **must be updated in the same change that alters the subsystem they describe**. They are authoritative on mechanism and never on status: if one implies a capability exists, `STATE.md` decides whether it actually does.

**Conceptual — `docs/FUTURE_*`. Binding on nothing.**

`FUTURE_LIVE_SENTINEL.md`, `FUTURE_SECURITY_OBSERVATORY.md`. Ideas recorded so they are not lost. They authorize no work, describe nothing that exists, and must carry their `Status: CONCEPTUAL / NOT AUTHORIZED` header. Never cite one as a requirement or as evidence that something is planned.

**Project-standard — `README.md`, `SECURITY.md`.**

Outward-facing files following normal open-source convention. `README.md` describes the system to a newcomer; where it disagrees with `STATE.md`, `STATE.md` wins and the README is stale.

---

Status, current truth, and active work always live in Tier 1. If you are about to create a document to hold any of those, you are making a mistake.

**Every documentation file in this repository belongs to exactly one of the five categories above.** If you cannot place a file, that is the signal not to create it.

## Governance ratification

`OUTCOME.md`, `STATE.md` and `WORK.md` define the mandate this system is built to. Agents may **propose** changes to them; only the **owner** may **accept** those changes.

- Put proposed governance changes in a PR and state the change plainly in the PR description. It becomes accepted truth when the owner merges it — not before.
- Never record an engineering decision in these documents as though it were an owner requirement. If scope was cut, a capability deferred, or a constraint adopted for implementation convenience, say so explicitly and attribute it to the agent that decided it.
- Treat any constraint already present in these documents as **unverified** if it lacks such attribution. Ask rather than inherit it.

**Why this rule exists.** A 2026-08-27 review found that scope decisions taken by coding agents had been written into these documents as accepted constraints, and every later agent then treated them as instructions from the owner. Direct short selling is the clearest case: an agent decided it was out of scope, recorded that in the governance docs, and the system was subsequently built long-only against an owner mandate that never excluded shorting. The same mechanism recorded the project's purpose as a research question about model quality, when the actual mandate is to make money.

Nobody misrepresented anything. Decisions laundered into requirements because there was no ratification step. This is that step.