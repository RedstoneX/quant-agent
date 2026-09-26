# QAMC Engineering Contract

Current engineering lead: **Codex**. This contract is intentionally agent-neutral so any capable engineering agent can follow the same rules.

## Start

Read `docs/STATE.md`, then `docs/WORK.md`. Use `docs/OUTCOME.md` for product intent and only the accepted architecture/contracts relevant to the task. `docs/FUTURE.md` is conceptual only.

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
- **Missing required data anywhere end-to-end is a defect in the step that should have produced it (owner 2026-09-17).** Find why. Fix that step so it actually produces the data. Never invent the missing value. Never make skip/drop/ignore-and-continue the permanent product. A drop-the-name quarantine is temporary until the producing step fills. Current instance: blank "I'll sell if" (`docs/WORK.md` item 78). The rule is not limited to that field.

## Execution discipline

- Prefer outcome-driven work over micro-prompts.
- Run the narrowest decisive test first; broaden only when evidence requires it.
- Do not re-read unchanged authority or re-prove settled facts.
- UI/frontend acceptance requires rendered desktop and iPad inspection; tests/builds alone are insufficient. The instrument is `npm run visual:acceptance` in `frontend/` — it drives a real browser across desktop and iPad (landscape and portrait) in populated, empty and error states. It writes to a gitignored `visual-acceptance.local/`; look at the shots, do not commit them. Named here because the rule is worthless if the only tool that satisfies it has to be remembered.
- Stop when the result is proven. Re-validation without new evidence is waste.
- Keep handoffs short: **changed / verified / preview if relevant / unresolved blocker / production state**.
- Do not infer current defects from historical notes. Reopen a resolved area only from current operator or production evidence.
- Private preview/browser verification is standing-authorized for relevant engineering work.

## Standing rules for dispatched agents

Every subagent brief was pasting the same block of rules by hand, and an
omission has cost real time more than once. Point a brief at this section
instead of restating it — the source facts mostly already live in
`docs/WORK.md`'s "Engineering setup" and "Operational facts" notes and in
`README.md`'s "Install" section; this section adds only what those do not
already say.

- **Foreground only. Never background a run and end the turn waiting on it.**
  Nothing wakes an agent that stalls that way — it has happened to six agents
  in one night.
- **Interpreter, git-add/git-stash, `gh pr edit`, branch protection and the
  doc merge driver:** see `docs/WORK.md`, "Engineering setup" and "Operational
  facts". One completion to the `gh` note there: the PATCH needs a field flag
  to carry a body from a file, not just `-X PATCH` —
  `gh api -X PATCH repos/RedstoneX/quant-agent/pulls/<n> -F body=@<file>`.
  The merge driver's one-time per-clone `git config` command is in
  `README.md`, "Install".
- **A green check from the docs/GitBook integration is not CI.** Confirm the
  `tests` workflow actually queued with `gh run list --branch <branch>`; if
  nothing queued, trigger it with `gh workflow run tests --ref <branch>`.
  Two branches shipped with no run at all.
- **The `Adversary:` line in a PR body is the SOFT check and no longer the
  standard for a closure.** `scripts/work_queue.py`'s `ADVERSARY_LINE` regex
  (`^\s*Adversary\s*:\s*(.+)$`) matches `Adversary: <argument>` and does not
  match `## Adversary`; four agents got that wrong in one night, and it only
  ever tested that six words were written down — the module says so itself at
  its line 104. A change that RETIRES a board item is now held to the
  enumerated record under "Definition of done" below, which fails `pytest`.
  Write both: the PR-body line for the stop hook, the commit-message record
  for the gate. See `.claude/agents/qamc-adversary.md` for who produces the
  argument.
- **No board item closes without the adversary agent arguing against closing
  it first** (`.claude/agents/qamc-adversary.md`) — it has found something
  real on every run.
- **An item is deleted only when its question is answered**, never when it is
  merely declined or has gone quiet. Turning a feature off and writing "no
  source exists" is not an answer.
- **After any doc merge, count item numbers per section.** `docs/WORK.md`
  holds independently numbered lists; the same number legitimately repeats
  across sections but never within one. On a collision, renumber one item —
  never delete either — and update its `docs/BOARD_NOTES.md` key in the same
  commit (see "The owner's board" below).
- **Never contact the owner directly, never create task chips, never spawn
  further subagents.** An unverified agent claim reaching him as an
  actionable task defeats the point of routing through a lead agent.
- **Verify the load-bearing claim in anything you are told, including by the
  agent that dispatched you.** Roughly one confident claim in three at this
  desk has not survived checking. Get dates and durations from git, logs or
  the filesystem — never from impression.

## Git and continuity

Use dedicated branches/PRs for substantive work. Do not force-push or push implementation directly to `main`. Paper-beta autonomy includes merging the agent's own verified PR and deploying it. Keep rollback possible. Bundle production preflight/deploy/restart/acceptance into the shortest safe intervention.

## Decisions ratified

- Stops were too tight and that was the root cause of two separate failures. The ATR multiple must scale by setup type and macro regime — never a hardcoded constant.
- Real short selling, not inverse ETFs. Three stages: countable, safe, live.
- No dev/prod mirror. Production is paper and resets, so the case for enterprise staging collapses. Build the rehearsal harness instead.
- The system already sends marketable limit orders, which is a market order with a bounded worst case. No change needed.
- Documentation is the source of truth. Wrong documentation is corrected on sight without asking.
- Rehearsal alerts are suppressed rather than routed to a second Telegram bot.
- Missing required data is a defect in the producing step. Never invent. Never make skip/drop/ignore-and-continue the permanent product. (owner 2026-09-17)

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

**Conceptual — `docs/FUTURE.md`. Binding on nothing.**

The one file for future ideas; add new ones as sections there, never as new files. It records ideas so they are not lost, authorizes no work, describes nothing that exists, and must keep its `Status: CONCEPTUAL / NOT AUTHORIZED` header. Never cite one as a requirement or as evidence that something is planned.

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
## The owner's board, and what editing `docs/WORK.md` obliges you to do

The owner reads exactly one page: the status board at `/board`. It is generated
by `scripts/status_board.py`, which re-derives everything it shows — it records
nothing of its own. It reads each item's **number, title, status and ordering**
out of `docs/WORK.md`, and the owner-facing plain-English prose for that item out
of `docs/BOARD_NOTES.md`, keyed by number (`## item 44`, `## gate item 3`,
`## decision due YYYY-MM-DD`).

**If you change `docs/WORK.md`, three obligations follow.**

1. **Never renumber an existing item** without updating its key in
   `docs/BOARD_NOTES.md` in the same commit. The prose is keyed by number, so a
   renumber silently orphans it and the board renders that item as unexplained.
   The board cannot detect that it lost an explanation — it only knows the key
   did not match.
2. **A new backlog item or pending decision needs prose in `docs/BOARD_NOTES.md`,
   not in `docs/WORK.md`.** `docs/WORK.md` is agent-facing and byte-capped at
   100,000 by CI; `docs/BOARD_NOTES.md` is owner-facing and uncapped. Follow the
   shape that file documents: plain language, a concrete worked example, and
   where a ruling is needed, the decision and a recommendation. No file paths, no
   function names, no code identifiers, no pull-request numbers — a mechanical
   detector flags those and the board marks the prose as engineer-facing.
   **An item with no prose renders honestly as "no plain-English version yet".
   That is correct. Never invent an explanation to fill the gap** — a confident
   wrong explanation on the owner's board is far worse than a visible absence.
3. **Merging is not publishing.** The board is rebuilt automatically when
   `docs/WORK.md` changes *in the production checkout* (`/home/qamc/quant-agent`,
   see `scripts/systemd/quant-agent-status-board.path`), and that checkout only
   changes on deploy. Deploy is by hand onto a detached HEAD. So a merge to
   `main` alone leaves the owner reading the previous state with no indication
   anything is missing. Either deploy, or confirm a session is watching `main`
   and will.

4. **A new item declares its completion criteria in the same commit**, and
   retiring one accounts for every criterion it was filed with. Both fail
   `pytest`; see "Definition of done" below for the exact shape.

**Why this rule exists.** On 2026-09-11 the production checkout was found nine
commits behind, so the owner's page had been showing week-old work while several
sessions merged against it. Separately, three findings raised to him as questions
were never written to the board at all, leaving him asked to rule on things he
had no way to read. Both failures share one cause: the board was treated as
something that updates itself, and it does not.

## Definition of done — four checks that fail `pytest`

Work here half-lands. The measured cases: the Portfolio Manager was stopped
from being told it has no margin and never shown its real buying power; the
weekend-aware session counter was added and one of its readers switched over;
a catalogue of arbitrary numbers was finished and sourcing them never started;
a credential file was built and the units that read it never installed. Nobody
decided to ship half — the half in front of the author got fixed and the rest
became permanent because nothing asked again.

`scripts/definition_of_done.py` is the mechanical version and
`tests/test_definition_of_done.py` is where it blocks, because `pytest` is the
only check branch protection requires. Read the module for the reasoning and
for what each check cannot catch; this is the contract.

**Every check is diff-scoped.** A change that files no item, retires no item
and touches no registered shared quantity is subject to none of them. That is
the ceremony bound, and it held for 44 of the last 50 commits on `main`.

1. **Filing an item declares its own halves.** A new `**N. ...**` block in
   `docs/WORK.md` carries a `DONE WHEN:` line followed by one `- [ ]` bullet
   per half. A question only the owner can answer says `NO CRITERIA: <reason>`
   instead. Existing items are grandfathered — nothing is retrofitted.
2. **Closing an item accounts for every criterion it was filed with**, in a
   commit message: `Done-criteria-met: N/1`, or
   `Done-criteria-deferred: N/2 -> item M (YYYY-MM-DD)` naming an item that
   exists after the change. A criterion cannot simply stop being mentioned.
3. **Changing a registered shared quantity accounts for every site that reads
   it.** The consumer set is DERIVED from the tree by AST walk — including
   sites that compute the quantity by hand and call nothing — and compared
   against the diff's own line ranges, per site, not per file. Account for an
   untouched one with `Consumers-unchanged: <file>:<function> — <why>`. The
   declaration is checked against the derivation both ways, so a shorter list
   does not satisfy it. Registry: `SHARED_QUANTITIES` in the module.
4. **A closure records objections, not the fact that an adversary ran.**
   `Objection-N: <argument>` and `Response-N: CHANGED <path> — ...` or
   `Response-N: REJECTED — ...`, at least two, in a commit message. A
   `CHANGED` response must cite a path the diff actually touches; that is the
   one part a reader can falsify mechanically.
5. **A closure names an acceptance observable.**
   `Acceptance-observable: <what someone can confirm after a real live
   session>`, citing a path that exists. Nothing on this desk validates a
   prompt or behaviour change offline.

**The required `pytest` check is an aggregate, not a single test.** It is
`needs: shard` with `if: always()` over the two-way test-shard matrix in
`.github/workflows/test.yml`, and it goes red whenever either shard is not a
success — cancelled, never-ran or genuinely failed all report the same way.
A `pytest` failure that returns in a few seconds, with no test output to
speak of, is almost always the definition-of-done check above failing inside
one shard, not a test regression; read the shard's own log before assuming
otherwise. Separately, `test_the_settled_cost_ceiling_still_stops_the_
portfolio_manager` (`tests/test_rehearsal_reproduces_cost_ceiling.py`) is an
ops-acceptance test gated on `sudo -n -u qamc` read access to the production
database — it is SKIPPED, not failed, everywhere that access is unavailable,
which includes every CI run. Two agents independently reported it as
pre-existing repo breakage on 2026-09-18; both were wrong, and it is not
evidence of anything broken in this repository.

**The gate reads commit messages only, never the PR description** — `git log
base..HEAD`, nothing else — and that base must be resolved on a FULL-HISTORY
checkout. `.github/workflows/test.yml`'s checkout step carries
`fetch-depth: 0` for exactly this reason: a shallow (depth-1) checkout left
the gate unable to resolve its own base, and even once base resolution was
patched around that, an earlier commit on a multi-commit branch could still
be missing from the readable range, so a trailer written two commits back
went unseen with no explanation (item 132, filed 2026-09-18). Do not
"optimize" that checkout step back to a shallow one to save CI time — that
reopens item 132. If the gate reports a missing trailer that you wrote,
check `scripts/definition_of_done.py::read_scope_note`'s output first (it
is printed on any failure and names the exact commit range read, plus a
shallow-checkout warning) before assuming the trailer itself is wrong.

**Deployment is verified on the box, not claimed in a PR.**
`scripts/check_item_deployment.py` asks, per item, whether the commit that
added that number to the retired-numbers line is an ancestor of
`/home/qamc/quant-agent`'s HEAD. It is disjoint from the two existing drift
checks by design: `check_deploy_drift.py` compares one commit against
`origin/main`, `check_unit_drift.py` byte-compares installed systemd units,
and a box that passes both can still be running a checkout that predates a
closure. It has no timer yet — installing one is a deploy action. Run it by
hand: `scripts/run_item_deployment_check.sh --no-telegram`.
