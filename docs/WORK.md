# QAMC Current Work

## Active finish line

### Session start — read this first

**STANDING PRINCIPLE — NO ARBITRARY NUMBERS, EVER.** Every trade constant comes from real data, a cited source, or the instrument itself — never a flat count, round % or a number that "sounds prudent". **Approval does not make a flat number non-arbitrary.** Mark unmeasured numbers provisional, never settled.

**STANDING PRINCIPLE — MISSING DATA IS A DEFECT IN THE PRODUCING STEP (owner 2026-09-17).** Find why a required field is blank and fix that step so it actually produces the data. Never invent. Never make skip/drop/ignore-and-continue the product. A drop-the-name quarantine is temporary. Item 78 is the current instance; the rule is not limited to it. Fuller statement: `docs/OUTCOME.md`.

**This file holds only open work.** Finished work is written up in `docs/INCIDENT_HISTORY.md` (append-only, each entry opening with one plain-language line) and then deleted here, together with its `## item N` block in `docs/BOARD_NOTES.md` and its NUMBER — the number alone, never a reason — added to the retired line. `tests/test_status_board.py` fails if this file passes 100,000 bytes, if one change grows it by more than the shrinking growth budget its current fullness allows (owner ruling 2026-09-17: recording a genuine new defect must not be blocked just because nothing is finished yet to prune — see `work_md_growth_budget` in `scripts/status_board.py`), if a self-declared-finished item is left sitting on the board, or if it loses an item number without retiring it. Ratified architecture decisions go in `docs/QAMC_REMEDIATION_SPEC.md` as a numbered phase, not here.

## DECISIONS PENDING — CI FAILS WHEN ONE GOES OVERDUE

**Do not delete a line to pass the build — decide it, then remove it in the SAME commit.** Format: `- [ ] DECIDE BY YYYY-MM-DD — question` (`test_no_pending_decision_is_overdue` parses it). It exists because a deferred decision was forgotten in 2026-08 and cost a zero-trade day.

**RESOLVED 2026-09-25 — the mandate is SWING (days to weeks), not a quarterly-horizon value book.** Decided by the orchestrator after an adversary run, per the 2026-09-18 ruling that this question does not wait on the owner. Reason: `docs/OUTCOME.md:75` already rules the desk's horizon "swing — days to weeks"; `config/prompts/tech_analyst.md:3` already treats its own 5-15d window as signal-validity, not holding period, with PM/position_reviewer owning the hold; holding period is an OUTPUT of thesis health, not a setting. `config/prompts/evening_analyst.md` and `config/prompts/meta_reflector.md` carried un-migrated rot from the original upstream value mandate (medium-long-term/quarterly-horizon framing) and have been rewritten to match. Detail and the exact prose diff: `docs/BOARD_NOTES.md` ("item 99").

- [ ] DECIDE BY 2026-10-31 — Which model should run the desk's actual trade-decision seat?
  **Owner ruling 2026-09-13:** no model comparison until the job board is clean. The date exists only because this format needs one; it moves rather than forcing a decision. **Nobody proposes the run or its spend to him — he raises it or it does not happen.** Incumbent `openai/gpt-5.5` stays until a first run exists. Owner decision 2026-09-15 goes further: no test-environment work at all unless he asks. The seat is ~93% of the LLM bill. Verified detail 2026-09-14: `docs/INCIDENT_HISTORY.md` and `docs/BOARD_NOTES.md`.

**RECONFIRM AFTER A FEW DAYS LIVE — `max_calls_per_session: 40` (owner instruction 2026-09-03).** The cost circuit's runaway-loop defence, set from real data (worst complete session: 14 calls). A first number, not final. Once live sessions exist, re-pull `llm_budget_sessions.logical_calls`; raise it if a legitimate session gets close, never lower it on a hunch. History: `docs/INCIDENT_HISTORY.md`, "item 14".

## PM TEST GATE — GARBAGE IN, GARBAGE OUT

The PM model test means nothing until everything feeding the PM is clean; this gate blocks the model decision above. Cleared items are written up and deleted.

**The gate is EMPTY as of 2026-09-14 — every PM-gate item is closed, and the owner's condition (no model test runs while any gate item is open) is met.** Gate item 7 moved to the backlog as item 76.

<!-- END PM TEST GATE -->

**Alert design (owner rule 2026-09-02).** Every failure alerts in its OWN Telegram message, never bundled into a run summary; severity is carried in TEXT, never colour; deliberately not deduplicated — a still-broken seat keeps alerting.

**Rehearsal rig — do not run it unless the owner asks (owner decision 2026-09-15, which retired test-environment work).** `ops/rehearsal/` replays a session offline; usage, flags and caveats are in `ops/rehearsal/README.md`. The one caveat worth carrying here: it cannot validate a prompt rewrite, because it replays recorded answers into a changed prompt and passes regardless.

**Production.** Mission Control is `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/` (Tailscale Serve to the loopback API). **A `git checkout` on the box is not a deploy:** restart `quant-agent-api.service`, then confirm the hashed bundle filename the server returns matches the one under `src/api/static_cockpit/assets/`. Never record a production SHA here; read it: `sudo -n -u qamc git -C /home/qamc/quant-agent log --oneline -1` (and `status --porcelain` for config drift, expected empty).

**Standing cautions.** The pre-2026-08-27 LLM-spend baseline is contaminated by runaway loops — build no allocation conclusion on it. Model-market strategy (champion/challenger) is TABLED by owner decision 2026-08-27. The PM's "Proposal Conversion" block reads 21 days of history the 2026-09-02 reset erased: a quiet block means "no data yet", not "no stuck loops".

**Engineering setup.** Work as `ubuntu`, never as `qamc`. No venv in engineering checkouts: use `/home/ubuntu/projects/quant-agent/.venv/bin/python` with `PYTHONPATH` at the checkout root and the five dummy API-key env vars CI uses. Read the live box with `sudo -n -u qamc`. Log timestamps are **UTC**; the owner is **ET** — convert before quoting times to him.

**Working agreement.** Standing autonomy to execute the backlog without stopping at phase gates. Interrupt only for live-capital activation, new paid dependencies, secrets redesign, destructive infrastructure, or evidence a ratified decision was wrong. The grant covers executing, not expanding: report breakage that predates your task in plain language and let the owner decide. Never state a date or duration from impression — take it from `git log` and check the author (everything before **2026-08-09** is upstream `yebof`). The desk board (`docs/phases.yaml`, `/board`) is a source of truth; defects go on it. Orchestrate: delegate at task START (cheapest tier for docs and inventory, mid tier for bounded implementation, strongest for trading/risk logic), read diffs not whole files, never two writing agents in one worktree, and verify the single load-bearing claim of every agent report (check the test count; reproduce the cause) — a confidently wrong root cause was caught only that way on 2026-08-29.

**Operational facts.**
- Stage explicit paths; never `git add -A`. Never bare `git stash` — the ref is repo-global across worktrees; use `git stash push -m "<name>"` and pop by index, or a throwaway worktree.
- Branch protection requires the `pytest` check to pass but does NOT require an up-to-date branch [verified 2026-09-18: `required_status_checks.strict` is `false`, contexts `["pytest"]`, `enforce_admins` true]. So pull requests may be merged IN PARALLEL — do not rebase each one onto `main` first. Merge `main` in only when there is a real conflict. Never `--admin`, never force-push. The previous wording here claimed up-to-date branches were required; that was false and made every agent serialise merges against a restriction that does not exist.
- `gh pr edit` and `gh pr view` fail on this repo (deprecated Projects-classic field). Use `gh api repos/RedstoneX/quant-agent/pulls/N -X PATCH` (body from a file: see `AGENTS.md`).
- Agents stall on polling loops: give every agent an explicit polling budget, or poll yourself.
- **A deploy installs the schedule.** `scripts/merge_and_deploy.sh` checks out `origin/main`, copies any changed or new `scripts/systemd/*.service`/`*.timer` into the qamc user's systemd dir, runs `daemon-reload`, enables what is not on `paused_units.yaml`, then restarts the API service (item 122 fix, 2026-09-24; before that the copy was by hand). **This is NOT silent:** `quant-agent-unit-drift.service` byte-compares every repo unit against the installed one and alerts on Telegram (it reported "in sync, 30 units" on 2026-09-18). Board item 122 carries the defect.
- **Never hand-resolve a conflict in `docs/WORK.md`, `docs/BOARD_NOTES.md` or `docs/INCIDENT_HISTORY.md`.** They use `scripts/resolve_doc_conflict.py` as a git merge driver (`.gitattributes` + `scripts/git_merge_driver_docs.sh`; one-time per-clone `git config` in README.md "### Install"). **Markers now mean one of two things** (changed 2026-09-23): no driver configured, or the driver REFUSED — a refusal writes markers plus a gitignored `<doc>.merge-refusal` beside the file carrying the reason, which is how you tell them apart; it used to write NOTHING and leave your own stale copy looking resolved. The resolver merges numbered ITEMS, refuses unless every item on either side survives exactly once, and stops on a number collision (a renumber, never a delete); refusing is correct. Hand-resolving is how five live items were deleted (item 68). The CLI (`--from-index`, or `--kind`/`--base`/`--ours`/`--theirs`/`--out`) works without the driver, including dry runs.

### Ordered backlog — RESUME POINT

**PRIORITY ORDER, set 2026-09-17 (the owner authorised the ordering). Work it top-down — it overrides item-number order.**
- **Tier 1, can cost money or hide risk:** 89 (its residue), 80, 90, 111, 112, 127, 199. (87 and 88 closed 2026-09-18; 130 retired 2026-09-25.)
- **Tier 2, wastes money or opportunity:** 91, 82, 81, 92.
- **Tier 3, clarity and hygiene:** 89 (its thirteen clarity defects), 94. (93 retired 2026-09-25.)
- **To be decided by the orchestrator after an adversary run, not parked on the owner (ruling 2026-09-18 — his words: "I don't want you waiting on me on anything. You have the adversary in my place. Just make sure it gets documented." The adversary argues, it never rules; the orchestrator decides and records the decision and its reason before anything is built on it):** 86. (109(a) closed 2026-09-26 — the OWNER ruled it himself on 2026-09-25, so it never took the delegated route; item 109 stays on the board for its part (c) only. 96 retired 2026-09-25 — the delegated question is stale, the veto it asked about is built and wired. 95 retired 2026-09-26 — decided yes, under the already-ratified cap and ladder, with the cost of the debit now shown to the seat.)

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**Top of the backlog. Work the PRIORITY ORDER above; do not reorder from intuition.** The original census items (ranks 1-8) are all retired and written up; the measured census that ranked them is in `docs/INCIDENT_HISTORY.md`.

**17. Backup alert channel — OWNER DECISION, deferred, no due date.** No channel exists beyond Telegram, so an alert that cannot reach Telegram reaches nobody.

DONE WHEN:
  - [ ] OWNER'S CALL — his own 2026-09-03 deferral, no due date: a second alert channel is new scope and, for anything but plain email, a new paid dependency, so nobody proposes it and it closes only when he raises it
  - [ ] when he does: the record-keeping circuit-breaker trip is shown reaching him on the second channel while Telegram delivery is failing, which is the exact live pairing that went unnoticed
detail: docs/BOARD_NOTES.md (item 17)

**19. The model's consistency is an ASSET — three uses. UNBLOCKED 2026-09-30 — the item-18 dependency is removed.** Measured: 5 blinded runs, two arms, quality identical to four decimal places. Unblocked 2026-09-30: this item only ever waited on item 18's prompt-bulk defect, whose core cause merged 2026-09-04 (PR #252) and whose earnings share was re-measured down from 70% to 18.6% on 2026-09-30; item 18's three residuals (ranking arithmetic, an out-of-repo spend cap, a paid-benchmark-blocked reorder) moved to item 208 and touch nothing item 19 does.

DONE WHEN:
  - [ ] (a) a repeat-run check exists that proves a pipeline change actually reached the model — identical inputs, and the answer moves only when the pipeline did
  - [ ] (b) the no-variance measurement is recorded against the desk's own blinded/unblinded five-run pairs BEFORE any paid repeat is dropped, or a recorded decision that repeats stay
  - [ ] (c) the stable famous-name bias is subtracted arithmetically inside the ratified weighted composite, or recorded as not worth doing — three prompt-wording fixes already measured no-change, so a fourth wording attempt does not tick this
detail: docs/BOARD_NOTES.md (item 19)

**20. GATE THE DECISION ON EVIDENCE COVERAGE — owner's design, 2026-09-02; the COUNTING half is all that is left, and it is his, not an agent's. Do not trade on partial evidence. Detail: `docs/BOARD_NOTES.md` ("item 20").** His ruling is that a decision on incomplete evidence is fabricated, not degraded.

DONE WHEN:
  - [ ] OWNER'S CALL — the counting half: either his ratified minimum number of usable per-seat reads, or his ruling that partial coverage never gates a decision. Nothing published gives that number and fitting one to the desk's own history is forbidden, so no agent may pick it and a placeholder never ships. This is his own 2026-09-02 ruling that the bar is a risk judgement.
  - [ ] OWNER'S CALL — whether the intraday scan's hard-coded technical `data_status` (`src/pipeline.py`) should be able to report LOST at all. Today the only blocking seat can never be lost there; that follows from his own `evidence_gate.BLOCKING_SEATS` mandate, so an agent may not widen the gate or add a second blocking seat to work around it.
detail: docs/BOARD_NOTES.md (item 20)

**55. What IS a structural level — how many bars make a swing point, and how wide is a level's zone? OPEN, filed 2026-09-13.** Touch count is settled and pinned by a test: two touches, sourced (Tsinaslanidis 2012) — do not tighten it.

DONE WHEN:
  - [ ] Tsinaslanidis §4.5's own bounce test (how often price entering a band leaves the way it came, against randomly drawn bands) is RUN on this desk's own universe and bars, sweeping cluster tolerance 0.5/1/2/3/5% and pivot window 3/5/10/25, and the result is recorded in `docs/RESEARCH_FINDINGS.md` — a reading, not a fit
  - [ ] on that reading: either one pivot window and one tolerance are single-sourced in code (today 3 in one module and 5 in another) with that measurement as their `config/number_ledger.yaml` source, or — if the effect is flat across the sweep — the percentage tolerance is replaced by the span of the pivot bars themselves, which needs no constant at all
  - [ ] the two-touch minimum is left exactly as it is: sourced (Tsinaslanidis 2012, 733 US stocks / 20 years) and pinned by a test
detail: docs/BOARD_NOTES.md (item 55)

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN (structure shipped, magnitude calibration still open), carried out of item 52.**

DONE WHEN:
  - [ ] the magnitude→sign boundary is settled by EVIDENCE, not appetite: either a published SIGNED insider-sale scoring scheme is cited and `SmartMoneyObservation.signal_direction` returns -1 off the already-reported `holdings_fraction_band` (Scott & Xu's sourced >50%-of-holdings band), or the desk's own resolved smart-money outcomes are numerous enough to read a separation from
  - [ ] until one of those exists a sale stays NEUTRALISED at 0 and no agent picks the boundary number — the standing no-arbitrary-numbers and no-fitting rules settle that, this is not an appetite dial
detail: docs/BOARD_NOTES.md (item 63)

**70. One underived `1.0` is doing two different jobs in the exit path, and neither is read off anything — OPEN, filed 2026-09-14.** The noise-band ATR multiple sets when an adverse move stops being noise and is reused as the margin in the structural-protection check; a separate absolute minimum stop multiple, also 1.0, sets how tight a stop may be.

DONE WHEN:
  - [ ] the noise-band ATR multiple carries a published measurement of the quantity it actually bounds — the adverse move at which a move stops being ordinary daily wobble — or a named derivation, recorded in `config/number_ledger.yaml` with that source
  - [ ] the absolute minimum stop multiple carries its OWN independent source or derivation, as a separate ledger entry: the two may not be collapsed into one shared constant just because the digits both read 1.0
  - [ ] the measured over-refusal is re-measured after whichever change lands, against the same recorded exits (today: 7 of 8 discretionary exits the reviewer approved were blocked as "too small a move")
  - [ ] neither value is retuned to make sales easier or harder in the same pass — how readily the desk should block a sale at all is the owner's appetite and is NOT this item
  - [ ] 2026-09-26, PARTIAL: the SPLIT is built and the research is recorded (docs/INCIDENT_HISTORY.md, 2026-09-26). The single `1.0` is now two named constants — `NOISE_BAND_ATR_MULTIPLE` (adverse move from entry) and `BREAK_CONFIRMATION_ATR_MULTIPLE` (how far a close must clear a level) — at the same value, with no behaviour change, each with its own ledger entry, research and open question. The exit-guard band was also found mislabelled `derived` from the 1.25 trailing band while holding 1.0, and is now honestly `arbitrary`. STILL UNMET: neither value is sourced. The noise band's only published analogue is ~3 ATR (Wilder, Chandelier, Kaufman), which is a large LOOSENING of how readily the desk may sell and therefore owner appetite this item may not decide; the break margin has no ATR basis in the literature at all (Edwards & Magee answer in percent, Bulkowski in "a decisive close"), so it is unidentifiable in its own units. The absolute minimum stop multiple still carries no source of its own.
  - [ ] 2026-09-30: the owner ruling that risk tolerance is read PER NAME from the instrument's own behaviour and the seats' conviction, never as a global dial, closes the owner-appetite route both remaining values were routed to. Neither may now be settled by the owner picking a global number. The noise band settles with a per-name read of how far THAT instrument ordinarily travels against a holding; the break margin has NO ATR basis at all and settles only by re-expressing it as a per-name percentage of price scaled by level importance (Edwards & Magee ~3% major / ~1% short-term). Both change live selling behaviour and neither was built in this pass; no replacement number was invented for either.
  - [ ] 2026-09-30, CLOSED ROUTE: a per-name statistical band (each name's own median historical adverse move) was built, reviewed and REJECTED — the justifying example was arithmetically a counter-example, the bar depth was a second chosen constant, the windows overlapped, a 29-session hold returned ONE observation as a "median", the second call site was unpatched, and a bars-fetch failure reverted silently to the wider band. Do NOT re-attempt it: any summary statistic of a distribution is a choice of percentile, so "read the band off the instrument" can never be satisfied by summarising history.
  - [ ] 2026-09-30, REDUNDANCY QUESTION ANSWERED (code, not prose): the band is NOT redundant with the alignment test, and it has two homes doing different jobs. Home 1 (pipeline midday review, ahead of every non-external SELL/REDUCE/COVER) measures the move from AVERAGE ENTRY before any structure is consulted — nothing else in the exit path is anchored to entry, and being anchored to entry is precisely what the alignment ruling forbids; deleting it and letting structure+trend decide is the honest fix and removes the number by removing the mechanism, but it IS an exit-behaviour change and is not made under this item's record-truth pass. Home 2 (`check_structural_protection` fallback) is the last resort when a holding has neither a `thesis_invalid_if` nor a qualifying level — alignment has nothing to read there, so deleting it strips protection outright; it needs its own answer first.
  - [x] 2026-09-30, DONE (record truth, no behaviour change): the refusal log asserted the move was inside the band without disclosing that the band width came from a floored/defaulted session count; the durable `exit_blocked_inside_atr_noise_band` row and the exit-refusal row carried only the model's reason. Both now carry a machine-readable `rule=atr_noise_band ...` payload (side, adverse, entry, price, atr14, band multiple, band width, sessions_held, sessions_measured) ahead of the reason. Two `StructuralProtectionCheck` outcomes that reported `noise_band_intact` WITHOUT ever evaluating the band now report `no_adverse_move_from_entry` and `noise_band_unevaluable_no_data`.
detail: docs/BOARD_NOTES.md (item 70)

**75. The desk has no automatic profit-taking: its target never reaches the broker, a trim for profit is not an allowed exit reason, and the trail sits too loose — OPEN, filed 2026-09-14 after an owner question on ORCL.**

DONE WHEN:
  - [ ] each open position's target is drawn on the Mission Control chart (the owner's own request)
  - [ ] four exit rules are tracked on every trade WITHOUT placing orders — sell all at target; sell half and trail the rest; target tightens the trail instead of selling; today's desk — with the rules fixed before anyone looks at the results and no tuning afterwards
  - [ ] a ruling is recorded on whether a target plus a CONFIRMED breakdown may exit, and if so profit-taking becomes an allowed SELL reason and a chart breakdown can unlock an exit (today only the news seat emits state changes)
  - [ ] trail tightness is read off the instrument or a cited source; the six trail constants are item 90's half two and item 185's tranche — do not re-derive them here
  - [ ] an 8-K results release is visible to the exit path (invisible today)
  - [ ] nothing here ships alone and nothing is fitted to ORCL — one number changed by itself is the patch this item exists to prevent
detail: docs/BOARD_NOTES.md (item 75)

**76. PM-input shape: the one open piece is whether the PM uses its new macro-audit channel. OPEN, moved out of the PM TEST GATE 2026-09-14.** Write-up: `docs/INCIDENT_HISTORY.md`, 2026-09-13/14.

DONE WHEN:
  - [ ] BLOCKED and cannot close by building — a before/after benchmark of whether the PM actually uses `reasoning_chain.macro_audit` is a paid run, and the owner's 2026-09-15 decision is that no test-environment work happens unless he asks. Same blocker as items 77 and 18(a); one authorisation would release all three.
  - [ ] it is not reopened as a prompt-size problem
detail: docs/BOARD_NOTES.md (item 76)

**77. Model selection: the PM seat is the next open question. Pointer, 2026-09-14.** Detail: `docs/architecture/MODEL_ROUTING_POLICY.md`.

DONE WHEN:
  - [ ] BLOCKED and cannot close by building — the PM seat's model question IS the `DECIDE BY 2026-10-31` line at the top of this file, and the owner's 2026-09-13 ruling is that nobody proposes that run or its spend to him; he raises it or it does not happen. Same blocker as items 76 and 18(a).
  - [ ] it closes with that pending-decision line and is not answered twice
detail: docs/BOARD_NOTES.md (item 77)

**78. Delete the blank-falsifier isolate once Tech and the PM demonstrably produce a real falsifier — DEFECT (patch), instance of the missing-data standing principle.** The isolate is live and declares itself TEMPORARY: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py:2817`) drops any constructed BUY/SHORT whose falsifier is blank.

DONE WHEN:
  - [ ] the never-blank path is live: a falsifier blanked by a later wipe is healed back from the sentence the model already wrote, the seat is re-asked once (paid), and a still-blank name is REFUSED before the book — never invented, and never with skip-and-continue as the product
  - [ ] LIVE-BLOCKED, the same shape item 86 was before a live log line retired it on 2026-09-26: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py`) is deleted only once a real live session records the seats filling the box, and the item stays OPEN until a live session proves it
  - [ ] MEASURED 2026-09-30 against the live database, condition NOT met: the technical seat still returns a blank `thesis_invalid_if` on 60% of the stocks it answered on 2026-09-29 (134 of 223) and 54% on 2026-09-28 (14 of 26), which is no better than the 30-73% daily range it ran at before the wrapper-object schema landed on 2026-09-25, so the tightened answer format did not make the seat produce a falsifier; on the narrower set of names that actually became targets the seat was still blank 5 times in 75; and criteria 2 and 3 in the docstring of `_isolate_empty_soft_exit_entries` CANNOT BE EVALUATED AT ALL because no soft-exit heal row has ever been written — all 56 `seat_heal` rows in the database carry gate `seat_heal` for the news, smart-money and technical seats and none of them is the soft-exit gate, so before this item can be judged again the soft-exit heal path must record its own outcome row (`not_attempted`, `cap_blocked`, `failed`, `paid_retry`) per name per session.
detail: docs/BOARD_NOTES.md (item 78)

**90. Unsourced trade-governing numbers — the GATE now exists; re-deriving the numbers does NOT. TIER 1, half shipped 2026-09-18, item stays OPEN.** **Half one, DONE:** every numeric definition site in scope must carry a `config/number_ledger.yaml` entry saying where it came from, or `pytest` fails.

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").
  - [ ] 2026-09-30, second pass: the floor's VALUE is untouched and the evidence to judge it is now recorded per closed trade (entry ATR, stop basis, maximum adverse excursion, alongside the realised outcome already stored), and the pipeline's stale 1.5 fallback is closed at source by reading the declared default instead of a copied literal; the record is for FALSIFICATION only (was the floor ever violated in practice) and may NOT be optimised against, so the next pass reads it rather than re-deriving a multiple. Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").

DONE WHEN:
  - [ ] half two: every `status: arbitrary` row in `config/number_ledger.yaml` is sourced, measured, owner-ratified as appetite, or reformulated away, and `MAX_ARBITRARY_ENTRIES` — an EQUALITY, not a ceiling — reaches zero
  - [ ] HALF TWO IS SPLIT INTO TRANCHES, EACH WITH ITS OWN CRITERIA: items 182 (de-lever ladder and alert), 183 (order-placement gates and dead cash-sweep config), 185 (trailing-stop numbers and the volatility-eligibility question) and 186 (portfolio/cluster ceilings and three owner-appetite answers) are NOT pointers — each carries DONE WHEN criteria this item does not repeat. Item 90 ticks when all four are fully ticked and no `status: arbitrary` row remains; do not re-derive a constant here that belongs to one of them. (Corrected 2026-09-30: this line used to say the four carry one word-for-word criterion, which was false.)
  - [ ] half one is already DONE (2026-09-18): the ledger gate exists and the build fails on an unsourced trade-governing number. Its honest limit stands recorded — it proves a reason was WRITTEN, never that the reason is TRUE — and that limit is not something this item can close.
detail: docs/BOARD_NOTES.md (item 90)

**99. The analyst seats' falsifier: only one of five states one — TIER 2, filed 2026-09-18, re-scoped and MEASURED 2026-09-30. Detail: `docs/BOARD_NOTES.md` ("item 99").** Measured read-only on the production database 2026-09-30: `tech_analyst` states a falsifier on 195 of 195 actionable ratings since 2026-09-25 and on 1,664 of 1,665 before — the "blank on about 6 in 10" figure is entirely NEUTRAL ratings, where prompt and schema both REQUIRE it empty, and is not a defect. `news_analyst`, `earnings_analyst`, `macro_analyst` and `smart_money_analyst` state none at all: no prompt asks, no answer schema carries the field, and 0 of 103 recorded nominations have one. Their invalidation is SYNTHESISED downstream, so a name whose only backer is one of those seats clears the conviction bar on a templated falsifier. Pinned by `tests/test_analyst_seat_falsifier_contract.py`.

DONE WHEN:
  - [x] the analyst seats' real falsifier coverage is measured from recorded production output rather than inferred from prompt text, and pinned by a test that fails when the contract changes — 2026-09-30
  - [ ] a recorded decision on whether the four uncovered seats must state their own falsifier, or whether a synthesised one is accepted and labelled synthesised wherever it travels — the live-money half, because it is what the conviction bar counts
  - [ ] (b) the technical seat's prompt names the five data blocks it actually receives and does not claim ones it does not
  - [ ] (d) the deletion-site check exists: removing a mechanism greps its symbol name across every prompt and every Python-assembled agent string at that moment
  - [ ] no blanket prompt-text number scanner is built (rejected: ~1,825 numbers in the prompt files, mostly dates and list numbering)
  - [ ] the mandate/horizon half is NOT re-opened — resolved 2026-09-25 as SWING, days to weeks
  - [ ] SPLIT OUT 2026-09-30, do not re-file here: the ~55 prompt-only numbers and ~20 unsourced market claims (was 99(a)), the render-or-pin-every-code-controlled-sentence requirement (was 99(g)) and the (f) residue are item 107(b)/(c), which already carries them; the PM/RM/reviewer dead-weight prose (was 99(c)) is item 109(c). Item 99 is now the ANALYST seats' own prompts and their enforcement only.
detail: docs/BOARD_NOTES.md (item 99)

**107. Prompt drift the new check cannot see, and prompt-only numbers. Filed 2026-09-17; parts (a) and (c) SHIPPED 2026-09-26, (b) still open.** Reasoning and what was ruled out: `docs/INCIDENT_HISTORY.md`, 2026-09-17 and 2026-09-26. **Do not re-propose the three designs rejected on 2026-09-17, and do not build a second deletion-site grep — that one exists.**

DONE WHEN:
  - [x] (a) a behaviour that CHANGES without being deleted forces the prose describing it to be re-read — `src/prompt_bindings.py` + `config/prompt_bindings.yaml` pin code and prompt prose to each other by digest and fail the build when one side moves; the deletion-site check was checked for first and left alone
  - [x] (c) all ten standing sheets are covered by the rendering check, an unregistered sheet is refused, and a placeholder no renderer can resolve fails in CI instead of at agent construction on a live morning
  - [x] the 12/10 drift pair has one definition (`src.risk.metrics.DRIFT_WEIGHT_PCT` / `DRIFT_PNL_PCT`), both sheets render it, and the one remaining literal site is named in the test so the count can only go down — `src/pipeline.py:_build_position_facts`, not editable in that pass
  - [ ] the last literal drift-threshold site in `src/pipeline.py` uses the named constant
  - [ ] (b) the trade-picking sheet's prompt-only sizing arithmetic (bases 3.0/1.75/0.75 and their ranges, the 0.25 reward:risk bonus, the 0.5 stale halving at age >=8d, the +/-0.5pp shade) and the technical sheet's prompt-only levels ("3+ aligned signals", the 1-3/4-7/8+ freshness tiers, forward-PE 40/60 and P/S 15/25) are each sourced or taken OUT of the path — verified 2026-09-26 that no code computes any of them and none is in the number ledger. The +/-0.20/+/-0.10 evening tilt this item listed was already gone from the formula (2026-09-17); its dangling mention was removed 2026-09-26
  - [ ] the 12/10 drift pair and the two prompt-only sets above carry a `config/number_ledger.yaml` entry with a source, or the open question and what the desk pays meanwhile
detail: docs/BOARD_NOTES.md (item 107)

**109. (a) RULED AND BUILT 2026-09-26; (c) still open — the dead-weight prose the prompt-truth pass surfaced. Filed 2026-09-17.**

DONE WHEN:
  - [x] (a) a decision is recorded on whether macro counts as a per-name seat in the agreement gate, then ONE of `count_aligned_sources` and the PM's sheet is changed to match the other — owner ruling 2026-09-25; BOTH were changed to match it on 2026-09-26, and the sheet states the rule rather than flagging a dispute
  - [x] whichever side wins is justified from the PM prompt's own provenance rule, NOT from `docs/OUTCOME.md` — that rule ("macro is the regime the book is built in, not evidence about one name") is exactly what separates a broadcast stance from a sector-specific one, and it is left unchanged
  - [ ] (c) the dead-weight recitation (~35% of the PM's sheet, ~26% of the risk manager's, ~24% of the reviewer's) is deleted, with load-bearing recitation kept — SHARED with item 99(c), which is the same prose; do not strip it twice
detail: docs/BOARD_NOTES.md (item 109)

**119. The economics feed can leave required series un-attempted at the open, and the re-derived fix is only measured mid-morning — OPEN, filed 2026-09-18.** Re-filed out of PR #435 (closed unmerged).

DONE WHEN:
  - [ ] every required series demonstrably gets a real attempt inside the existing ceiling, proven against open-like conditions rather than a healthy mid-morning batch
  - [x] (b) a series still missing is named, the verdict it produced is visibly partial wherever it travels, and the desk never pays a second time on the same holes — RESTATED and met 2026-09-26. "The economist is not paid on the holes" was DECIDED AGAINST on measurement: the macro seat is bought once a day at the open and nowhere else, so skipping it deletes the regime frame rather than delaying it, and both measured partial runs had already burned the full ceiling so there was nothing bounded to wait for. The economist is paid once on what arrived; `MacroCoverage.verdict_stamp()` stamps `coverage_state`/`coverage_note` onto the verdict from the fetch record, and that stamp now survives `MacroStore.save_last_state`, the carry-forward into midday/close/intra, the PM sheet and the owner's `📊 Market:` line. A second paid call on the same holes was already impossible (`"partial"` is `CATEGORY_REPORTED`, never healable) and is now pinned by a test instead of left to the table. No coverage threshold was picked; 14/15 and 1/15 are both `partial`. Measurement, money and the argument against the other three options: docs/BOARD_NOTES.md (item 119)
detail: docs/BOARD_NOTES.md (item 119)

**147. A success with no usage data — the filed premise was wrong, the cache-hit half is FIXED 2026-09-26, the no-telemetry half stays open.** Measured on the production DB read-only 2026-09-26: `agent_logs` holds 667 rows over 2026-08-14..2026-09-26 and exactly 7 have a NULL cost — six on 2026-08-31, one on 2026-09-17, every one of them a `smart_money_analyst` synthesis-cache HIT that issued no provider request at all (`provider_requests=0`, `latency_s=0.0`, `input_message='[cached evidence hash]'`). There is no "full reservation": item 14 deleted the reservation layer on 2026-09-02, and neither affected day was ever marked inexact (`unknown_cost_rows=0`, `costs_exact=1` on both). A cache hit now books its real, exact zero instead of a NULL, and the day seeder no longer counts a provably request-free success as unknown spend — which had left a latent operator-only `legacy_unknown_cost` hard latch reachable on any day whose budget row is seeded after such a row lands (a mid-day deploy, the case the seeder exists for). What is left is the case that has never once occurred: a call that really did reach a provider and came back with no usable token or cost telemetry. Also measured: zero rows have tokens > 0 with a NULL cost, so BOARD_NOTES' open case (b) — known tokens, model missing from the pricing table — has never fired either.

DONE WHEN:
  - [ ] a success whose provider request DID happen but returned no usage telemetry is charged at a measured rate instead of hard-latching the desk — still open and deliberately not guessed at: `complete_call` sees only `cost is None`, the missing thing is the per-token rate, and the pricing table is the same one that failed, so there is no defensible basis to multiply by. Unchanged until either a rate source or a real occurrence exists.
  - [x] DONE 2026-09-26 — a cache hit is priced, not unknown: `smart_money_analyst.analyze` books `cost_usd=0.0` on the cache path, and `_unknown_cost_row_expr` in `src/cost_circuit.py` excludes a row proven free by its own record (`cost_usd IS NULL AND provider_requests = 0 AND status = 'success'`) from the seeded day's `unknown_cost_rows`. All three conditions are required; 194 legacy rows with a NULL `provider_requests` keep counting as unknown. Six tests in `tests/test_cost_circuit.py`.
detail: docs/BOARD_NOTES.md (item 147)


DONE WHEN:
  - [ ] the news-seat parse-failure rate is understood and either brought down or shown to already recover cleanly on retry — UNDERSTOOD and the LOSS is closed (the seat is re-asked on an unreadable field and, only if the re-ask fails too, the field is dropped and reads ABSENT while the rest of the report survives; an unsalvageable answer files an `analysis_drop` row per affected stock), but the RATE itself is neither brought down nor shown to recover: the seat can still emit "mixed", the cure at source needs `strict: true`, and the news answer cannot have it while `stock_news` is a ticker-keyed free-form map. Needs either live evidence that the re-ask recovers, or a decision on reshaping the answer so the enum can be enforced.
detail: docs/BOARD_NOTES.md (item 152)

**157. The technical seat has no enforced answer format on either route, so a malformed row still needs salvaging after the fact — filed 2026-09-19, from #538's write-up.** #538 made a broken row recoverable, not prevented.

DONE WHEN:
  - [ ] a live call confirms whether the Google route enforces a sent response schema
  - [ ] a decision is recorded on whether the schema change is worth it given row-salvage already ships
detail: docs/BOARD_NOTES.md (item 157)

**174. Nobody is told when the cost circuit lets itself back in — filed 2026-09-23 with the 503/self-clear fix (write-up in `docs/INCIDENT_HISTORY.md`).** A hard latch alerts Telegram; the new transient self-clear writes an `auto_reset` event and a log line only, so the owner sees "desk suspended" and never sees it come back.

DONE WHEN:
  - [x] a self-clear reaches the owner on the same surface the suspension did — the auto-expiry now sends the same Telegram alert the suspension does (🟢 RESUMED, naming the forgiven trigger, when it cleared and why, every number read from the `auto_reset` event row), keeping the DB event and log; durable/retryable like the quota-recovery alert, and suppressed under `QAMC_REHEARSAL=1` at the notifier chokepoint
  - [x] the resume is PAIRED to a suspension the owner actually received — 2026-09-26: the auto-clear captures the suspension's `alert_state` at the last instant it is knowable (the same write wipes it), and a resume for a suspension that never reached him is resolved as unpaired instead of sent, so an outage that ends before he hears about it is zero messages, not a dangling "back live"
  - [x] reproducible offline — the fault harness gained a `server_error_mid_stream` kind (a pre-generation 503 is provably free and can never latch; only a mid-stream one can), and a morning rehearsal with it reproduced the latch, the suspension alert, the auto-expiry and the paired resume alert end to end
  - [ ] cooldown and allowance re-read against a real occurrence — STILL OPEN: production has had ZERO real `auto_reset` events, so the 15-min cooldown and the 19/day allowance remain unmeasured; a rehearsal cannot measure them because it sets the cooldown itself
detail: docs/BOARD_NOTES.md (item 174)

**177. Paid intraday tick: trigger, cadence and held-book context are ONE decision, filed 2026-09-23. Item 90 half two tranche one; do not re-file the pieces.** The trigger decides whether a tick is paid, the cadence how many, the held book what a paid one costs [measured 09-21/22; `docs/INCIDENT_HISTORY.md`].

DONE WHEN:
  - [ ] all 3 leave `status: arbitrary`, `MAX_ARBITRARY_ENTRIES` falls by 3 — NOT MET, and now precisely blocked rather than merely unstarted. `move_threshold_pct` cannot be sourced yet: the desk's own 253 recorded selections show the flat threshold does not discriminate (median move 3.50% when a BUY/SHORT followed, 3.67% when nothing did; the 5-7% band produced zero orders from 51 selections), so re-picking it has no basis, and the ATR-relative form the ledger's own open question asks for was **unmeasurable because the denominator was never recorded**. That is fixed here — a mover's row now carries `atr_pct=` and `move_atr=` alongside `move_pct=`, from bars the scan already paid for, no behaviour changed — so the row can be sourced once the data exists. `max_candidates_per_scan` and `cooldown_hours` are owner-appetite, not research.
  - [x] the cadence ledgered and test-covered — the "unledgered" half was STALE: `src.config.INTRA_CHECK_TICK_MINUTES` has carried `status: sourced` since the item was filed. The untested half was real and is closed here. Production runs the **systemd timer**, not the APScheduler trigger (verified on the box 2026-09-26), and nothing pinned the timer's *spacing* to the constant the cost circuit derives its cooldown from — only its minutes were pinned, for an unrelated reason. `tests/test_systemd_units.py` now derives the production cadence from `quant-agent-intra_check.timer` crossed with `run_if_et_window.sh`'s ET window, holds it equal to `INTRA_CHECK_TICK_MINUTES`, holds the live-mode trigger to the same spacing, and fails if the operator-facing tick count in the wrapper outlives the schedule (it had: it still said ~14 after the 2026-09-17 move to 13).
  - [ ] every intra-preamble job on its own schedule — NOT MET, not attempted. This is the precondition for ever cutting the paid cadence: the free safety work (fill reconcile, stop-out reconcile, protection-restore and repeg drains) and the paid scan are welded to one 13-tick schedule, so cutting spend today means cutting loss-protection latency.
  - [x] spend and actions re-measured — 2026-09-26, against the production cost circuit read-only. `intra_check` is the desk's largest spender: **$13.93 of $22.18 all-time, 62.8%**, over 211 sessions of which 106 were paid, against morning's $7.66 over 31. 13 paid ticks a day since the timer moved, 14 before. A tick carrying the held book costs **1.54x** a movers-only tick ($0.167 vs $0.108 mean). **80% of paid ticks (85 of 106) produced no order**, and the whole record attributes 21 new positions to intraday discovery — **$0.66 of model spend per position opened**. The mover cap binds on 21.5% of runs and drops the excess with no record. Figures and method in `docs/BOARD_NOTES.md`.
detail: docs/BOARD_NOTES.md (item 177)

**182. The de-levering ladder's rungs and cash-deficit cushion are made-up money numbers with no board item — filed 2026-09-25, TIER 1.**

DONE WHEN:
  - [x] the forced cash-deficit cushion is sourced, measured, owner-ratified as appetite, or reformulated away — 2026-09-30, reformulated away (sized off the order's own limit floor; no replacement constant)
  - [ ] `GROSS_LADDER_ALERT_PCT` is sourced, measured or owner-ratified as appetite — at what drawdown must the owner be told, independently of what the ladder does to exposure? (deduplication against the ladder was attempted and reverted)

detail: docs/BOARD_NOTES.md (item 182)

**183. Five order-placement gates are made-up money numbers with no board item — filed 2026-09-25, TIER 1.**

DONE WHEN:
  - [ ] each constant is sourced, measured, owner-ratified as appetite, or reformulated away
  - [ ] DEAD CONFIG, remove rather than source: the T-bill cash-sweep was retired 2026-09-17 (`cash_sweep.enabled: false`), so its still-`arbitrary` constants `_BUY_LIMIT_PAD` 1.001, `_SELL_LIMIT_PAD` 0.999, `_FUND_BUFFER_FRAC` 0.01, `_FUND_BUFFER_MIN_USD` 50 and `CashSweepConfig.min_order_usd` 500 are unreachable while the sweep is off and should be deleted, not re-derived
  - [ ] 2026-09-26, PARTIAL. RESOLVED: the constructor's `$500` floor is DELETED — nothing in the constructor read it and the one call that forwarded it reached a parameter `apply_gross_ceiling` has ignored since 2026-09-24, so no order size, refusal or gate changes; the comment at its definition site claiming that gate still read it was false the day it was written. MEASURED, all three from the desk's own filled orders 2026-09-15 to 2026-09-25 (method in docs/INCIDENT_HISTORY.md): the 40bp belt runs at a median +2.6bp with a p90 of +26.4bp and a maximum of exactly +40.0bp, so it binds one order in thirty and every one of the 9 recorded slippage refusals sat 391–1466bp out; the 2% ask-skip fires at ~241bp above reference, inside a ~350bp band of the desk's own data containing no observation at all, so every multiple from ~1.004 to ~1.035 would have decided every observed case identically; and the 0.5% weight-delta floor faces zero commission and a measured ~1 cent execution cost on a $50 order. None of the three measurements picks a value — the belt censors its own tail, the skip gate sits in an empty gap, and the churn half of the weight-delta question is appetite. STILL UNMET (as of 2026-09-26): those three, plus the dead-config criterion below.
  - [ ] 2026-09-30, PARTIAL. RESOLVED: the owner ruled the desk has autonomy to nudge a position whenever its own reasoning calls for it, unless doing so is illogical (broker-mechanical) — so `min_trade_weight_delta` is DELETED outright rather than resized, closing the appetite half the 2026-09-26 measurement above left open; no replacement percentage was substituted. The mechanical bounds kept are read from the broker, not chosen: Alpaca's own per-asset fractionability flag (`AlpacaBroker.get_fractionability`), its tick-size normalization (`_quantize_price`), and a new fail-soft catch in `AlpacaBroker.submit_order` for a broker rejection (`_is_terminal_submission_rejection`) — because a genuinely tiny order can now reach the broker for the first time and may hit Alpaca's own documented $1 minimum notional for a BUY entry. TWO CORRECTIONS to that catch, same change: (a) it first reused board item 129's STOP-PLACEMENT retry set (APIError 400/404/422) on the ORDER-SUBMISSION endpoint without re-checking the codes mean the same thing there — Alpaca's own create-order reference (https://docs.alpaca.markets/reference/postorder) documents only 200, 403 and 422 for POST /v2/orders, and its troubleshooting guide (https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca) names 400 only in a funding flow, so the submission test is NARROWED to 422 alone and 400/404 propagate as before; 404 in this API addresses a resource by id, which on a submission would read as “created, then not found”, and swallowing it would hide a LIVE order. 403 is documented here but deliberately not added — widening what `submit_order` swallows is not what the classifier is for. (b) making the call NON-RAISING silently broke the D7 naked-short EMERGENCY_COVER in `src/pipeline_stages.py`, the one `submit_order` call site that read only the returned `id` instead of testing the result with `_order_accepted`: a rejected cover was written down as a `fill_status="submitted"` trade with a success event, on the exact path that runs when a SHORT has filled and its protective stop did NOT place. It now raises into the pre-existing failure branch — the CRITICAL operator page and the `emergency_cover_failed` event, no trade row. The other four call sites (`src/pipeline.py` x2, `src/pipeline_stages.py` entry submit, `src/execution/cash_sweep.py`) were audited and all already guard on `_order_accepted`. `tests/test_scale_in.py` now carries both a behaviour test and an AST source check that fails if any future edit drops the guard or moves it after the trade row. (c) deleting the floor also deleted the only reason the constructor's delta loop ever filed, which left candidates ending anonymously and broke board item 10's standing guard (`tests/test_every_drop_path_files_a_reason.py`). The guard is NOT weakened. Two honest reasons replace the one that went, neither of them a threshold and neither a new constant: `target_weight_zero_nothing_held` (the desk named a stock but asked for a weight of zero and holds none of it — all 8 of the newly-anonymous candidates in the production archive are this exact case [measured against `tests/fixtures/constructor_drop_paths_archive.json`], and the old message calling them “smaller than the desk's minimum” was never true of a zero) and `short_already_at_target_weight` (a held short exactly at target, which unlike the long side emits no HOLD row in this stage). ONE pinned replay verdict moved and is argued in that test rather than quietly re-pinned: a 0.196%-of-book add on CMCSA that the floor used to convert into a do-nothing HOLD is now attempted and refused by a pre-existing, unrelated data fault (no computable structural levels), so the name stopped being silently held and started being honestly refused — the ruling working, not a regression. A SEPARATE, unmeasured finding raised alongside this change and NOT acted on here: removing the floor also removes whatever rate-limit and brief-unprotected-replace-window protection its churn-suppression side effect provided (the same operational-churn cost item 185 already names for `MIN_RATCHET_PCT`) — flagged for the owner/adversary rather than silently kept. STILL UNMET: the belt and the skip gate, plus the dead-config criterion below.
  - [x] 2026-09-30. The 2% ask-skip is DELETED, not sourced and not ratified — with its SHORT mirror (`bid < floor / 1.02`), which was a derived copy of the same number. The gate refused an approved entry whenever the displayed IEX offer sat more than 2% past the slippage ceiling, on a multiple its own comment called "deliberately loose because the input is". MEASURED against every firing it has on record — 8 rows, the whole life of that telemetry (2026-09-15 to 2026-09-24), against Alpaca 1-minute bars: in all 8 the reference price was RIGHT and the quote was WRONG. The reference matched what the name was actually trading at to within a few bp, while the quoted ask sat 392–669bp above the HIGHEST price that name traded anywhere in a ±15-minute window around the refusal, and 6 of the 8 were trading strictly inside the very ceiling they were refused against. Zero of the 8 were a market that had run. A second measurement, taken live on 2026-09-30 13:47–13:58 UTC across 32 snapshots of 55 of the desk's own names (3,630 observations): this account's IEX top-of-book half-spread has a median of 19bp but 25 of those 55 names carry a median above 40bp, and the deleted condition was true on 17.2% of observations in an ordinary session — a gate that refuses roughly one entry in six on venue noise alone. It was also never protecting money: a limit at the ceiling cannot fill through the ceiling, so a genuinely runaway book now simply leaves the order resting until the existing end-of-session entry-protection cancel takes it, which is the same no-trade minus the false refusals. `src/trader_feed.py` had already refused to show the owner this gate's `slippage_gated` code for the same reason (2026-09-23); that finding is now acted on rather than only rendered around. NO multiple replaces it — a far-through quote is recorded as a `venue_quote_through_ceiling` pipeline event, which needs no threshold because it is a record and not a decision. SIDE-EFFECT, stated rather than buried and stated PRECISELY: an entry that rests unfilled is cancelled after a 90-second fill wait (`_ENTRY_FILL_TIMEOUT_S`), not at the close of the session, and the parent order's own tif is DAY. The owner page is CONDITIONAL, not reliable: `_alert_owner_entry_cancelled` runs only when `on_unfilled_cancel` fires, which needs `cancelled_here` True, so a `cancel_order_by_id` that raises logs an error and pages nobody; and an order is only registered with the protection sweep when it carries `pending_stop_price` (or is a scale-in whose live stop was cancelled), so an accepted entry without one is never watched, never cancelled and expires at the DAY close unannounced. Most of what this gate used to swallow becomes a Telegram alert; not all of it.
    SAME-LOOP BUDGET, fixed in the same PR: `entry_budget -= estimated_cost` fires on SUBMISSION and `order_ceiling = min(entry_budget, single_name_cap)` is read by every later candidate in the same submit loop, and the deleted `continue` used to skip both — so a far-through entry that genuinely cannot fill would trim or zero-size a later candidate that could have. Fixed by moving a far-through name to the BACK of the submit queue exactly once: still submitted, still sized identically, simply drawing the pool after the names quoting inside their own ceilings. No threshold and no new number — the test is the ceiling itself, and this is strictly less authority than the same reading carried as a refusal. Restoring the pool on cancel was considered and REJECTED as useless: the budget is a local of the execution stage, dead before the 90-second cancel lands, and the next session recomputes it from the broker.
    NOT COVERED by that fix: a deferred name re-reads its quote and re-tests the submit-window overrun, so in a burst that is already near its latency budget a deferred entry can be dropped as `latency_window` when it would otherwise have been submitted; a far-through name is deferred at most once, so a batch where every name reads far-through keeps its original order; and an ordinary entry that rests unfilled still holds the pool for the rest of the loop exactly as it did before this PR.
  - [ ] STILL UNMET, the 40bp belt (`ExecutionConfig.max_entry_slippage_bps`). It is now the ONLY bound on what an entry pays, and it is still not identified: the 2026-09-26 measurement leaves an indifference band of roughly 32bp to 390bp because the belt censors its own tail, and no published reference for an acceptable entry-slippage bound on retail marketable limits was found. Two routes out, neither taken here because neither should ride along with a live-path deletion. (a) REFORMULATION, the cheaper one: the universe screen already computes each name's own ordinary half-spread from daily bars with a cited estimator (Corwin & Schultz 2012) and compares half of it to this very belt — the two are already one relationship — so the entry ceiling could be read off the instrument as that name's own half-spread instead of a flat figure, leaving one constant (the screen's ceiling) where there are now two uses of one. The catch is that the belt absorbs reference-to-submission DRIFT as well as spread (it was raised from 25 to 40 in 2026-08 precisely because VLO drifted 28bp seconds after the open), so a spread-only ceiling would recreate that failure and the drift term needs its own instrument-read basis. (b) MEASUREMENT, newly unblocked by the deletion above: an entry too tight to fill now rests and is recorded instead of being pre-refused, so the rate at which 40bp fails to fill becomes observable from the desk's own orders for the first time.
  - [ ] CHECKED 2026-09-26, the reserve-band question: the deployment-gap advisory (`sweep_reserve_usd` / `cash_above_reserve`, surfaced on /account) is display-only with no consumer in this repo, but `reserve_pct` has a SECOND reader — the sweeper itself, which is disabled, not removed (`cash_sweep.enabled: false` while `src/pipeline.py` and `src/pipeline_stages.py` still construct and call `CashSweeper`). Deleting the advisory alone would leave the band alive, unreported and one flag from live: less honest than today. So `reserve_pct`, the four dead sweep constants and `CashSweepConfig.min_order_usd` die together with the sweeper and the advisory, or not at all — roughly 187 references across the pipeline, the API and nine test modules. Not started here.

detail: docs/BOARD_NOTES.md (item 183)

**185. Trailing-stop numbers are made-up money numbers with no board item — filed 2026-09-25. OPEN.**

DONE WHEN:
  - [x] `CHANDELIER_ATR_MULTIPLE` sourced (published Chandelier default) and `MIN_RATCHET_PCT` owner-ratified as churn appetite with its false cost premise corrected
  - [x] the 50%-of-price literal is removed from the code and from the ledger, and the guard/screen circularity is broken — both ends now compute from `widest_reachable_stop_atr_multiple` (2026-09-30)
  - [x] the screen-divisor mismatch (base 2.5 vs reachable 3.00, the 16.67-20% band the screen admitted and the guard would have argued with) is FIXED, not accepted
  - [x] no refusal on this path can leave a position unprotected — the midday guard clamps and places instead of skipping, per board item 80 (2026-09-30)
  - [ ] the ELIGIBILITY question is answered rather than declined: either a primary methodology document stating an absolute ATR/price bound is cited, or the ceiling is rewritten in the published cross-sectional-quantile form and the owner sets the quantile. Until then 33.3% stands as an arithmetic non-degeneracy floor ONLY and must not be described as a volatility appetite
  - [ ] the two rows the 3.00 now depends on (`min_stop_atr_multiple` 2.5, `stop_atr_regime_scale` risk-off 1.20) are resolved, or their open questions are explicitly inherited by this item rather than left orphaned

detail: docs/BOARD_NOTES.md (item 185)

**186. Portfolio and cluster risk ceilings are made-up money numbers with no board item — filed 2026-09-25.**

DONE WHEN:
  - [x] the three already-ratified ceilings (25 / 90 / 40) stay ratified, and the remaining three are researched to a definite verdict rather than left unexamined
  - [x] WITHDRAWN 2026-09-30 — the pairwise-correlation appetite question is moot: the cutoff is removed, not set. Cluster membership is now read from the book's own correlation-distance tree (Mantegna MST cut at its widest gap), per the owner's ruling that risk tolerance is never a global dial. Still transitive, still rationing only.
  - [ ] ***OWNER APPETITE*** how much smaller should a short open than a long carrying the same stated risk, given the loss above the stop is unbounded? Today's 1.5 means two-thirds the size. A stored-daily-bar build would replace this with a measurement
  - [ ] ***OWNER APPETITE*** how much of total equity may the desk lose overnight on ONE name whose just-filed report nobody has read, accepting the stop does not hold through a gap? Answer that tolerance L and the cap stops being chosen: it reads L divided by the expected absolute earnings-day move, and L = 0.25% reproduces today's 5%

detail: docs/BOARD_NOTES.md (item 186)

**187. FRED fetch reliability — the chronic `fetch_deadline_exceeded` failure and required series left un-fetched — filed 2026-09-25, carried out of item 175's retirement. Item 175's weekend/holiday overdue-date roll shipped and was retired; this is the separate, still-open half. Detail: `docs/BOARD_NOTES.md` ("item 187").** Every FRED failure in the retained log is `fetch_deadline_exceeded`; 4 of 12 runs reached full coverage, worst 5 of 15 [measured 09-17..23]. Owned by the approved fetch redesign.

DONE WHEN:
  - [ ] the `fetch_deadline_exceeded` rate is understood and either brought down or shown to recover cleanly inside the existing time ceiling, measured against real runs rather than a healthy mid-morning batch
  - [x] the FRED SERIES half is fixed and live: fair-share reserves plus the pre-open series cache; the deployed box ran 2026-09-30 with no series skipped.
  - [x] the EVENT-CALENDAR half is fixed here: the seven `/fred/release/dates` calls move off the trading path onto the existing pre-open prefetch timer and are served from `data/macro/release_schedule_cache.json` at the open; a release in neither cache nor wire stays a named failure and is never defaulted.
  - [ ] one clean morning open observed with 7/7 release schedules from cache before this item retires — the fix is deployed-and-unobserved until then.
detail: docs/BOARD_NOTES.md (item 187)

**188. The decision seats' last-resort route is now a small free model, and nobody has measured it at those seats — filed 2026-09-30.**

DONE WHEN:
  - [x] no seat has every reachable route on one provider, enforced mechanically against `config/settings.yaml` rather than by reading the config by eye
  - [ ] the substitute is either measured at the three decision seats through `ops/model_policy/benchmark_models.py`, or the seats are made to refuse rather than answer when only that route is left — decided on the measurement, not on a guess about how bad it is

detail: docs/BOARD_NOTES.md (item 188)

**190. The disabled cash-sweep / T-bill feature needs full retirement, not just its reachable band — filed 2026-09-30, carried out of item 183's dead-config finding. Detail: `docs/BOARD_NOTES.md` ("item 190").**

DONE WHEN:
  - [ ] every reference to the cash-sweep / T-bill feature (`CashSweeper`, `CashSweepConfig` and its fields, the dead pad/buffer constants, and the deployment-gap advisory fields that read `reserve_pct`) is either removed or re-justified as still reachable, across the pipeline, the API and the nine test modules item 183 identified
  - [ ] `config/number_ledger.yaml` rows for the constants that die with it are deleted rather than left describing code that no longer exists
  - [ ] MEASURED 2026-09-30, the premise is PARTLY WRONG and the scope is corrected here: this is not a pure deletion of switched-off code. `CashSweepConfig.reserve_pct` has a live non-sweep consumer — `src.risk.rules.deployment_gap_band_pct` reads it as the tolerance band for the `deployment_gap` advisory on EVERY session, and `/account` renders it as `reserve_usd` / `cash_above_reserve` via `src.api.deps.get_cash_sweep_reserve_pct`. Deleting the sweeper therefore cannot delete the band by default: the band must either die with the advisory (a separate call, since "how far under 100% still counts as fully invested" is appetite-shaped, not bookkeeping) or be given a home outside the retired feature. Item 183's band question is NOT resolved by this item as filed
  - [ ] MEASURED 2026-09-30, `cash_sweep.min_order_usd` is vestigial as a trade gate but NOT as prose: `src.pipeline_stages._min_order_usd` records (2026-09-24) that none of the three paths that used to reject a small trade still do, and `apply_gross_ceiling` accepts it only as an ignored parameter — but `src.agents.portfolio_manager` still SPEAKS it to the owner in the funding narrative ("under the $N minimum order worth placing"). It dies with the feature, and those owner-facing strings must be rewritten rather than merely dropped
  - [ ] MEASURED 2026-09-30, the 187-reference estimate is LOW. A live count over the checkout is ~550 mentions of `cash_sweep` / `CashSweeper` / `cash_sweeper` / `_sweep_symbol` / `SGOV` / `reserve_pct` across 88 files, including four frontend components (`LiquidityPanel`, `HoldingsStrip`, `DecisionStateBanner`, `funnelShared`), the Mission Control API schema and routes, `ops/preview/branch_preview.py`, and ~30 test modules rather than the nine item 183 named. The `sweep-vehicle liquidation before a BUY` entry already in `config/retired_mechanisms.yaml` is the registry hook the deletion must extend
  - [ ] the retirement is sequenced so no step leaves a half-wired feature: (1) settle the `reserve_pct` band and the `deployment_gap` advisory, (2) rewrite the portfolio-manager funding prose off `min_order_usd`, (3) delete `CashSweeper`, its pipeline/stage wiring and its tests-of-the-dead-path, (4) drop the ledger rows and the frontend/API surface

detail: docs/BOARD_NOTES.md (item 190)

**192. The local Python interpreter could silently drift from the one CI runs, with nothing checking it — filed 2026-09-30.**

DONE WHEN:
  - [x] the exact CI version is written in one place (`.python-version`) that both CI jobs read via `python-version-file`, instead of each job carrying its own literal
  - [x] any local pytest run, including a single test file, fails immediately and names both versions plus which one CI uses, if the running interpreter doesn't match the pin (`tests/conftest.py`, fires at collection so it can't be skipped by running one file)
  - [ ] the existing `.venv` (measured 3.12.3) actually gets rebuilt on the pinned 3.11 — deliberately NOT done here: other sessions run against that `.venv` right now, so a live rebuild is a scheduling call, not something this change should force mid-flight

detail: docs/BOARD_NOTES.md (item 192)

**194. A wall that forms after entry now re-derives the target, but only when a seat flags the symbol — filed 2026-09-30. Detail: `docs/BOARD_NOTES.md` ("item 194").**

DONE WHEN:
  - [ ] either the scheduled check's `TARGET_AIMS_PAST_A_STANDING_WALL` finding feeds the same `assess_target_revision` adjudication a seat flag does, or it is recorded why a seat flag must stay the only way in
  - [ ] AAPL and NOK are each either re-derived or recorded, by name, as findings the desk has decided not to correct

detail: docs/BOARD_NOTES.md (item 194)

**193. The scale-in cancel-to-rearm window leaves the WHOLE held position unprotected, and it is now measured — filed 2026-09-30. Detail: `docs/BOARD_NOTES.md` ("item 193").**

DONE WHEN:
  - [x] the window is measured from the broker's own cancel and rearm acknowledgements rather than from database write times, so the figure bounds real exposure instead of event bookkeeping — every scale-in now emits its own measured window (2026-09-30)
  - [ ] the gap between write-ahead-log row ids and filed cancel events is explained, so the pair count is known to be complete rather than a floor
  - [x] the desk can answer "is any position naked right now, and for how long" without a one-off query, whether by an alert, a dashboard line or a periodic check — every coverage sweep now names each symbol it deliberately skipped for a live scale-in, with its held quantity and roughly how long its protection has been down, in the run record and in the one greppable log line; a window longer than the longest the desk has ever measured pages the owner once per symbol per day, and with no measured history nothing is called overdue (2026-09-30)

detail: docs/BOARD_NOTES.md (item 193)

**199. Read the unbacked-stop floor off the chart instead of off an ATR multiple — filed 2026-09-30, carried out of item 90's `min_stop_atr_multiple` pass. TIER 1.** Detail: `docs/BOARD_NOTES.md` ("item 199").
DONE WHEN:
  - [ ] the share of real candidates that have a computed level below entry at any touch count is measured from production data, so the size of the population this actually removes from the ATR multiple is known rather than assumed
  - [ ] the far-anchor case is decided and written down: what the floor does when the nearest level below entry is distant enough to shrink the position materially, including whether the flat multiple remains as a ceiling on the widening
  - [ ] `config/number_ledger.yaml`'s entry for `src.config.RiskConfig.min_stop_atr_multiple` records the outcome, and either its status changes or its note states exactly which population it still governs
detail: docs/BOARD_NOTES.md (item 199) — item 90's ledger entry carries the retracted arguments so they are not re-proposed

**200. The status board's own file was one change away from blocking every other change — filed 2026-09-30. OPEN: the move is made, the guard against it recurring is not.**

DONE WHEN:
  - [x] `docs/WORK.md` is back under 70% of its cap by MOVING argument, history and measurement out of open items — not deleting it, not raising the cap — with every moved byte proved verbatim in `docs/BOARD_NOTES.md` by a line-level diff and the rendered owner prose unchanged block-for-block
  - [x] the cap and the growth budget are named as what they are: both PICKED, not derived (100,000 gave ~5% headroom over a measured 94,801; the 0.5 growth share calls itself provisional), and both allowed to be picked because a documentation size limit governs no money
  - [ ] the move is REPEATABLE without a human deciding what to carve: nothing yet stops the same items re-accreting history in place, so the next time the cap binds it will again be hand-work
   — the file passing (say) 80% should say so in the same place the growth-budget failure already speaks, rather than the first warning being a blocked merge

detail: docs/BOARD_NOTES.md (item 200)

**202. The rehearsal harness is not hermetic — a test that replays a RECORDED session downloads live market data — filed 2026-09-30.** `tests/test_rehearsal_reproduces_cost_ceiling.py::test_the_settled_cost_ceiling_still_suspends_paid_analysis` reaches yfinance for price history on every run and takes ~196s doing it; it FAILS on main today [measured 2026-09-30, `origin/main`, network reachable]. Pre-existing, not caused by the conftest network guard that exposed it. detail: docs/BOARD_NOTES.md (item 202)

DONE WHEN:
  - [ ] the rehearsal harness serves its market data from the recorded session rather than from the network, so the test passes with outbound HTTP fully blocked
  - [ ] the test is not skipped, not retried and not marked flaky to achieve that, and its runtime drops because it no longer waits on a live fetch
  - [ ] any OTHER test that still reaches the network is named, because the conftest guard now makes such a dependency fail loudly instead of silently


**208. Item 18's three residuals, carried forward — filed 2026-09-30 when item 18 was retired. The prompt-bulk defect that item 18 was opened for no longer applies and was re-measured under that item; these three leftovers remain OPEN, share no subject with it and were blocking item 19 for no reason. Detail: `docs/BOARD_NOTES.md` (item 208).** One changes what the ranking seat decides, one is an account setting outside this repo, and one cannot be closed by building at all.

DONE WHEN:
  - [ ] (a) a recorded decision, in `docs/INCIDENT_HISTORY.md`, on whether reward:risk (today a within-tier tiebreak) and net evidence (unused) join the ratified composite score — this is engineering under doctrine, not an owner call; per-seat sizing weights stay refused either way
  - [ ] (b) the OpenRouter key carries a provider-side spend cap, or its absence is recorded as accepted — the account is outside this repo, so it closes on an observation in the provider console, never on a test
  - [ ] (c) BLOCKED and cannot close by building — the BUY-eligibility section reorder needs a paid benchmark run the owner has forbidden unless he asks for it (same blocker as items 76 and 77); it stays open and untouched until he raises it
detail: docs/BOARD_NOTES.md (item 208)


**Retired item numbers — never reuse.** APPEND-ONLY as of 2026-09-30 — closing an item adds ONE NEW `- retired <scheme>: N[, N, ...]` line below, in the matching scheme, and never edits an existing line; the running lists used to live on this one physical line, and even the merge driver's own union rule (`scripts/resolve_doc_conflict.py::merge_retired`) could not save it, because GitHub's own squash-merge — what actually runs when a pull request merges on GitHub.com — never invokes a local git merge driver at all. Two closures now append two different lines and merge with no conflict, by construction; no driver needed for this part. **This still takes the NUMBER ONLY — never a reason.** Every retirement's reason lives in `docs/INCIDENT_HISTORY.md`, which is append-only and merges entry-by-entry the same way. `tests/test_status_board.py` fails a change that adds a reason to any line below, or that edits an existing line instead of appending a new one. The per-item reasons this line used to carry were moved to `docs/INCIDENT_HISTORY.md` on 2026-09-26, verbatim, losing nothing. Gate item 7 was moved, not closed: it is item 76. The two numbering schemes are separate — 3 is retired in BOTH, 20 is live here, and 40, 67 and 200 never existed [verified 2026-09-18 against this file's full git history]. Residue of items 100 and 103 lives in items 106 and 115; item 89 was SHRUNK, not retired. The §11.2 ladder stays; the ladder's own unmeasurable-drawdown behaviour is a separate live question. Run `scripts/next_board_number.py` for the next free number — it reads every line below, the live board, and open pull requests; never eyeball this list. It FAILS CLOSED as of 2026-09-30: if the open-pull-request read fails for any reason it exits non-zero and prints no number at all, because it used to print a warning and a number anyway and two pull requests both claimed item 192 that way. Treat a non-zero exit as a hard stop, not a prompt to guess; `--accept-unchecked-number` is the deliberate offline opt-out and labels its answer UNCHECKED.

- retired queue: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 61, 62, 65, 66, 68, 69, 71, 72, 73, 74, 79, 80, 81, 82, 83, 84, 85, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 100, 101, 102, 103, 104, 105, 106, 108, 110, 111, 113, 114, 115, 116, 117, 118, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 148, 149, 150, 151, 153, 154, 155, 156, 158, 159, 160, 161, 162, 164, 165, 166, 167, 168, 169, 170, 171, 172, 175, 176, 178, 179, 180, 181, 184, 189
- retired gate: 1, 2, 3, 4, 5, 6, 7, 8
- retired queue: 64
- retired queue: 191
- retired queue: 163
- retired queue: 86, 173
- retired queue: 198
- retired queue: 112
- retired queue: 152
- retired queue: 197
- retired queue: 18
## Evidence-only follow-ups — reopen only on concrete production evidence

- news-narrative factual drift; `actual_provider` attribution oddity.
