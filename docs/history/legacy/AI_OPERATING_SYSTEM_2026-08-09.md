# QAMC AI Operating System

## Purpose
This file governs how AI development agents work on QAMC so model usage, context growth and engineering effort stay bounded **without weakening verification, safety or documentation discipline**.

GitHub is the durable source of truth. Claude Code is the engineering lead/orchestrator for authorized implementation work; ChatGPT remains the separate product/architecture/review/governance authority.

The governing principle is **delegate outcomes, not implementation recipes**. Give Claude the authorized objective, constraints and acceptance criteria, then let it determine the implementation architecture, worker topology, parallelism, testing sequence and integration details inside those boundaries.

## Claude Code model policy
Model names and defaults change quickly. Preserve the policy intent rather than hard-coding yesterday's lineup.

- For a large architecture/orchestration problem, the lead may use the most capable model/effort level available and justified by the task or the operator's current selection.
- Routine implementation, tests, documentation and mechanical workers should use an efficient capable model where worker-level model choice is available.
- Fast/cheap models are appropriate for bounded exploration and mechanical work, not safety-sensitive architectural judgment.
- Do not escalate model cost merely because a conversation is long; improve delegation/context hygiene first.
- Do not override an explicit operator model choice merely because another model exists.

## Capability-aware orchestration
Claude Code now has multiple ways to parallelize work. The lead should detect what is actually available in its current environment and choose the least complicated mechanism that safely fits the task.

Suitable mechanisms include:
- **subagents** for focused research, coding, testing or review in separate context windows;
- **background workers/tasks** when independent work can continue concurrently;
- **git worktree isolation** for parallel writers that must not share a working tree;
- **agent teams** when available and when workers genuinely benefit from peer-to-peer communication/shared task coordination;
- separate full sessions/branches for truly independent workstreams.

Rules:
1. Maximize **safe** parallelism, not worker count.
2. Partition parallel implementation by clear interfaces/file ownership. Do not let multiple workers edit the same files concurrently merely to increase throughput.
3. The orchestrator owns interface decisions, integration, conflict resolution and final verification.
4. Experimental orchestration features are optional accelerators, never project dependencies. If one is unavailable, unstable or expensive to coordinate, fall back immediately to simpler subagent/session delegation.
5. Do not pre-author or micromanage the exact number/roles of workers unless a specific risk requires it.
6. Prefer fresh-context reviewers with no authorship role for major self-review passes.

## Session, context and usage discipline
1. Start a **fresh Claude Code session for each newly authorized independent tranche of work**, not mechanically for every milestone when governance explicitly authorizes a coordinated multi-stage tranche.
2. Start from the current approved `main` unless an authorization document explicitly names another base.
3. Rehydrate from the mandatory repository read order in `AGENTS.md`; do not paste old Claude transcripts or giant historical prompts when GitHub already records the state.
4. Read only the additional files needed for the authorized work. Do not repeat repo-wide audits or donor research unless an assumption is stale or the current tranche requires it.
5. Use delegation to keep search results, logs and implementation detail out of the lead context when only the conclusion matters.
6. Context-window pressure and subscription/session usage limits are different problems. A usage-limit reset is **not** a reason to discard a coherent session/branch; resume it after reset if the context remains healthy. If context itself becomes bloated or confused, compact/delegate/split deliberately.
7. Cloud sessions may be long-running development workers; they are still disposable relative to GitHub. Important decisions, checkpoint state and implementation facts must be committed/documented rather than existing only in transcript memory.

## Planning discipline
For a large or cross-layer change, Claude should investigate before editing and establish a coherent plan. The plan may be produced by the lead, by dedicated planning/research workers, or by a native planning facility available in the environment. Planning tools are aids, not mandatory ceremony.

Do not spend substantial time configuring a planning/orchestration feature when an ordinary Claude plan plus repository context is sufficient.

## Testing economy
- During implementation, run the **smallest targeted tests** that exercise the changed behavior.
- Run the **full existing suite once at each externally governed checkpoint/tranche end**, unless failures or repository governance justify another run.
- For an explicitly authorized multi-stage tranche, stage-internal gates may use targeted suites plus any broader tests the lead judges necessary; the tranche-ending external checkpoint gets the governed full-suite run.
- Do not repeatedly run the entire suite after documentation-only edits when no governing requirement calls for it.
- Never reduce safety-critical or acceptance testing merely to save model usage.
- UI milestones require visual/runtime verification in addition to unit tests when practical; use the browser/preview/render capability actually available rather than treating green unit tests as visual proof.

## Independent self-review
Claude self-review is encouraged but does not replace ChatGPT's external checkpoint review.

For substantial changes:
- use a fresh-context reviewer/subagent/teammate that did not author the code where practical;
- ask it to challenge correctness, safety boundaries, integration assumptions, test gaps and requirement coverage rather than polish style;
- fix verified findings before presenting the checkpoint;
- preserve the final review result in the checkpoint report.

Premium/usage-credit review services are optional. Do not incur separate paid review usage merely because the feature exists unless the operator has authorized that spend or it is within an explicitly available included allowance.

## Research and tool economy
- Prefer verified repository evidence over re-researching facts already captured in governed docs.
- Revisit external projects/donors only when their current state is relevant to active work or a prior assumption is stale.
- Optional integrations remain effort-bounded; stop and report when a supposedly small integration becomes a new project.

## GitHub self-service and delegation policy
The operator has given a standing workflow preference for QAMC: **when ChatGPT has a connected GitHub capability that can safely perform an already-authorized repository action, ChatGPT should perform it directly instead of asking the operator to click through GitHub or spending Claude Code usage on GitHub mechanics.**

Examples include repository inspection, branch/commit/PR inspection, bounded documentation/governance edits, branch creation, PR creation or metadata updates, checkpoint verification, and merging an already-reviewed/verified PR when the merge is within the operator's authorized scope.

- Prefer **ChatGPT → GitHub connector** for GitHub-native mechanics that the connector supports.
- Prefer **Claude Code** for implementation work that genuinely needs a code workspace, command execution, tests, debugging or broad source edits.
- Do not send the operator manual GitHub instructions for an action ChatGPT can safely complete directly, unless the operator explicitly asks to do it manually.
- Do not consume Claude Code credits merely to perform GitHub administration that ChatGPT can do itself.
- This standing preference does **not** authorize destructive, ambiguous or scope-expanding changes.
- Within an already-approved bounded task, handle routine GitHub publication mechanics end-to-end where possible.

## Checkpoint and handoff discipline
### Default workflow
At every normal external STOP checkpoint:
1. Claude verifies the acceptance criteria.
2. Claude updates governed implementation documentation necessary to record the state.
3. Claude commits and pushes the completed slice/tranche to its working branch.
4. Claude reports commit SHA(s), tests/results, files changed and unresolved blockers.
5. Claude **STOPS**. ChatGPT independently inspects the actual GitHub work; the operator accepts or rejects; ChatGPT handles authorized merge/sign-off mechanics.

### Explicit multi-stage tranche exception
Repository governance may explicitly authorize several tightly coupled stages as one engineering tranche. In that case:
- the authorization document defines which internal stage gates Claude may cross autonomously;
- Claude must still self-verify and create clear commit/documentation boundaries at each internal gate;
- Claude must not claim operator acceptance of those internal gates;
- Claude stops at the tranche-ending external checkpoint, where the normal ChatGPT/operator review resumes.

Current authorization for the Mission Control UI tranche is recorded in `docs/MISSION_CONTROL_BUILD_TRANCHE.md`.

## Runtime boundary
Claude Code Cloud is a development environment, not QAMC's permanent runtime. `here.now` may be used for frontend preview/staging when appropriate; the permanent runtime target remains the governed Linux VPS/server architecture.