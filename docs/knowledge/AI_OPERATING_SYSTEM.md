# QAMC AI Operating System

## Purpose
This file governs how AI development agents work on QAMC so that model usage, context growth and engineering effort stay bounded **without weakening verification, safety or documentation discipline**.

GitHub is the durable source of truth. Claude Code sessions are disposable workers: recover state from the repository, complete one bounded slice, commit/push, STOP, and start fresh for the next authorized slice.

## Claude Code model policy
- **Sonnet is the default implementation model** for ordinary coding, tests, UI work, documentation updates and routine debugging.
- **Opus is an exception model** reserved for architecture/audit work, difficult debugging that Sonnet cannot resolve, high-risk safety decisions, and major independent checkpoint reviews where deeper reasoning materially changes confidence.
- **Haiku may be used only for genuinely mechanical/simple work** where the task does not require architectural judgment or safety-sensitive reasoning.
- Do not switch to Opus merely to write checkpoint reports, run tests or perform routine implementation.
- If the available model lineup changes, preserve the policy intent: use the least expensive model that can reliably complete the bounded task; escalate only for demonstrated need.

## Session and context discipline
1. Start a **fresh Claude Code session for each milestone or deliberately bounded implementation slice**. Do not continue a completed stage's long conversation into the next stage.
2. Start from the current approved `main` unless repository governance explicitly requires another base.
3. Rehydrate context from the mandatory repository read order in `AGENTS.md`; do not paste old Claude transcripts or giant historical prompts when the repository already records the state.
4. Read only the additional files needed for the authorized slice. Do not repeat repo-wide audits, donor scans or architecture research unless the current milestone explicitly requires them.
5. Keep prompts bounded to the authorized stage, explicit exclusions and checkpoint deliverables.
6. If a session becomes context-heavy, starts repeating work, or the task expands materially beyond the authorized slice, **STOP and split the work** rather than allowing the session to balloon.
7. Do not automatically escalate to a more expensive model because a session is long; prefer a fresh session with clean repository context first.

## Testing economy
- During implementation, run the **smallest targeted tests** that exercise the changed behavior.
- Run the **full existing suite once at the governed checkpoint**, unless failures or repository governance justify another full run.
- Do not repeatedly run the entire suite after documentation-only edits when no governing requirement calls for it.
- Never reduce safety-critical or acceptance testing merely to save model usage.

## Research and tool economy
- Prefer verified repository evidence over re-researching facts already captured in governed docs.
- Revisit external projects/donors only when their current state is relevant to the active milestone or a prior assumption has become stale.
- Optional integrations remain effort-bounded; stop and report when a supposedly small integration becomes a new project.

## GitHub self-service and delegation policy
The operator has given a standing workflow preference for QAMC: **when ChatGPT has a connected GitHub capability that can safely perform an already-authorized repository action, ChatGPT should perform it directly instead of asking the operator to click through GitHub or spending Claude Code usage on GitHub mechanics.**

Examples include repository inspection, branch/commit/PR inspection, bounded documentation/governance edits, branch creation, PR creation or metadata updates, checkpoint verification, and merging an already-reviewed/verified PR when the merge is within the operator's authorized scope.

- Prefer **ChatGPT → GitHub connector** for GitHub-native mechanics that the connector supports.
- Prefer **Claude Code** for implementation work that genuinely needs a code workspace, command execution, tests, debugging or broad source edits.
- Do not send the operator manual GitHub instructions for an action ChatGPT can safely complete directly, unless the operator explicitly asks to do it manually.
- Do not consume Claude Code credits merely to perform GitHub administration that ChatGPT can do itself.
- This standing preference does **not** authorize destructive, ambiguous or scope-expanding changes. Deleting important branches/data, force-moving refs, altering repository protections/settings, merging unverified implementation, or starting a new milestone still requires the applicable governance/authorization.
- Within an already-approved bounded task, handle routine GitHub publication mechanics end-to-end where possible: inspect → create/update branch or PR → verify → merge when authorized → report the result.

## Checkpoint and handoff discipline
At every STOP checkpoint:
1. Verify the bounded acceptance criteria.
2. Update governed documentation necessary to record the new state.
3. Commit and push the completed slice to its working branch.
4. Report the commit SHA, tests/results, files changed and unresolved blockers.
5. STOP. Do not begin the next stage in the same session unless explicitly authorized by repository governance and the operator.

After checkpoint acceptance/merge, the next worker should start a fresh session from the updated `main` and recover state from GitHub.

## Runtime boundary
Claude Code Cloud is a development environment, not QAMC's permanent runtime. `here.now` may be used for frontend preview/staging when appropriate; the permanent runtime target remains the governed Linux VPS/server architecture.
