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
- UI/frontend acceptance requires rendered desktop and iPad inspection; tests/builds alone are insufficient. The instrument is `npm run visual:acceptance` in `frontend/` — it drives a real browser across desktop and iPad (landscape and portrait) in populated, empty and error states. It writes to a gitignored `visual-acceptance.local/`; look at the shots, do not commit them. Named here because the rule is worthless if the only tool that satisfies it has to be remembered.
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

## Document authority

Two tiers. Know which one you are reading before you trust it.

**Tier 1 — the authority stack. Closed to additions.**

1. `AGENTS.md` — the engineering contract and operating rules.
2. `docs/STATE.md` — accepted current truth about the system.
3. `docs/WORK.md` — active work, including the ordered backlog that a cold session resumes from.
4. `docs/OUTCOME.md` — the product mandate and intended outcome.

No agent may add a fifth. Proposals to change any of these follow the ratification rule below.

*`docs/PROJECT_COMPASS.md` held a fifth slot here and was retired by the owner on 2026-08-31. It was human-only — agents could not correct it under this section — and it had drifted into contradicting `OUTCOME.md` about the project's core purpose: it still framed QAMC as a paper-only experiment asking whether cheap AI adds trading value, against `OUTCOME.md`'s owner-corrected mandate that QAMC is a systematic trading desk built to make money. It duplicated `OUTCOME.md`'s role rather than serving one `OUTCOME.md` couldn't. Do not recreate it.*

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