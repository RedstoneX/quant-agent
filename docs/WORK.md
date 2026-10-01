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
  **Owner ruling 2026-09-13:** no model comparison until the job board is clean. The date exists only because this format needs one; it moves rather than forcing a decision. **Nobody proposes the run or its spend to him — he raises it or it does not happen.** Incumbent `openai/gpt-5.5` stays until a first run exists. Owner decision 2026-09-15 goes further: no test-environment work at all unless he asks. The seat is ~93% of the LLM bill. Verified detail 2026-09-14: `docs/INCIDENT_HISTORY.md` and `docs/BOARD_NOTES.md`. Absorbs retired board item 77 (a bare pointer at this line; routing detail in `docs/architecture/MODEL_ROUTING_POLICY.md`; it closes with this line and is not answered twice).

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
- **To be decided by the orchestrator after an adversary run, not parked on the owner (ruling 2026-09-18 — his words: "I don't want you waiting on me on anything. You have the adversary in my place. Just make sure it gets documented." The adversary argues, it never rules; the orchestrator decides and records the decision and its reason before anything is built on it):** 86. (109(a) closed 2026-09-26 — the OWNER ruled it himself on 2026-09-25, so it never took the delegated route; item 109(c) retired 2026-10-01. 96 retired 2026-09-25 — the delegated question is stale, the veto it asked about is built and wired. 95 retired 2026-09-26 — decided yes, under the already-ratified cap and ladder, with the cost of the debit now shown to the seat.)

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**Top of the backlog. Work the PRIORITY ORDER above; do not reorder from intuition.** The original census items (ranks 1-8) are all retired and written up; the measured census that ranked them is in `docs/INCIDENT_HISTORY.md`.

**17. Backup alert channel — OWNER DECISION, deferred, no due date.** No channel exists beyond Telegram, so an alert that cannot reach Telegram reaches nobody.

DONE WHEN:
  - [ ] OWNER'S CALL — his own 2026-09-03 deferral, no due date: a second alert channel is new scope and, for anything but plain email, a new paid dependency, so nobody proposes it and it closes only when he raises it
  - [ ] when he does: the record-keeping circuit-breaker trip is shown reaching him on the second channel while Telegram delivery is failing, which is the exact live pairing that went unnoticed
detail: docs/BOARD_NOTES.md (item 17)

**20. GATE THE DECISION ON EVIDENCE COVERAGE — owner's design, 2026-09-02; the counting half is BUILT as a per-name RECORD (2026-10-01) and what is left is one unrelated owner call. Do not trade on partial evidence. Detail: `docs/BOARD_NOTES.md` ("item 20").** His ruling is that a decision on incomplete evidence is fabricated, not degraded.

DONE WHEN:
  - [x] The counting half, closed as a RECORDING rather than a bar (2026-10-01). Two honest attempts at deriving a coverage threshold both failed — nothing published states one, and the production evidence table cannot supply one because per-name coverage is unrecorded for the news seat in every run and partial for macro [measured read-only against the production DB, 228 runs with symbol-scoped evidence]. Both failure reasons are written down in `src/evidence_gate.py` beside `name_coverage`. Per the owner's standing ruling that risk is read per name and never set as a global dial, the question collapses to the categorical one the seat half already answers, asked once per name: did this seat answer ABOUT this name. `evidence_gate.name_coverage` records that per candidate, `pipeline._record_name_coverage` persists it on every decision, and no ratio, minimum or verdict ships with it — a test asserts the record carries no numeric field at all.
  - [ ] OWNER'S CALL — whether the intraday scan's hard-coded technical `data_status` (`src/pipeline.py`) should be able to report LOST at all. Today the only blocking seat can never be lost there; that follows from his own `evidence_gate.BLOCKING_SEATS` mandate, so an agent may not widen the gate or add a second blocking seat to work around it.
detail: docs/BOARD_NOTES.md (item 20)

**55. What IS a structural level — how many bars make a swing point, and how wide is a level's zone? OPEN, filed 2026-09-13.** Touch count is settled and pinned by a test: two touches, sourced (Tsinaslanidis 2012) — do not tighten it.

DONE WHEN:
  - [ ] FIRST, because 2026-09-30 found the sweep below is not runnable at all: a daily-bar source exists for it — either a one-off pull of this desk's universe committed as a test fixture, or the rehearsal account's read path — since the repo holds no bar cache and `src/execution/broker.py::get_bars` needs credentials a build agent must not touch
  - [ ] Tsinaslanidis §4.5's own bounce test (how often price entering a band leaves the way it came, against randomly drawn bands) is RUN on this desk's own universe and bars, sweeping cluster tolerance 0.5/1/2/3/5% and pivot window 3/5/10/25, and the result is recorded in `docs/RESEARCH_FINDINGS.md` — a reading, not a fit
  - [ ] on that reading: either one pivot window and one tolerance are single-sourced in code (today 3 in one module and 5 in another) with that measurement as their `config/number_ledger.yaml` source, or — if the effect is flat across the sweep — the percentage tolerance is replaced by the span of the pivot bars themselves, which needs no constant at all
  - [ ] the two-touch minimum is left exactly as it is: sourced (Tsinaslanidis 2012, 733 US stocks / 20 years) and pinned by a test
detail: docs/BOARD_NOTES.md (item 55)

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN (structure shipped, magnitude calibration still open), carried out of item 52.**

DONE WHEN:
  - [ ] the magnitude→sign boundary is settled by EVIDENCE, not appetite: either a published SIGNED insider-sale scoring scheme is cited and `SmartMoneyObservation.signal_direction` returns -1 off the already-reported `holdings_fraction_band` (Scott & Xu's sourced >50%-of-holdings band), or the desk's own resolved smart-money outcomes are numerous enough to read a separation from
  - [ ] PREREQUISITE FOUND 2026-10-01: the desk's own data cannot settle it because it holds NO sale observations to read — the production evidence store has 521 smart-money observations across 325 analyst rows, ALL buys, zero sales [measured, production DB specialist_evidence, 2026-08-26..2026-09-30] and only 80 trades in total [measured, same DB]; the remaining build is a RECORDING of insider-sale rows (with `holdings_fraction_band`) plus their forward return, found first by tracing why no sale reaches the stored evidence although the parser emits them
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

**75. Automatic profit-taking: the whole-position case is answered by the alignment exit; the residue is the PARTIAL (trim) case and the recorded target's one surviving live effect — OPEN, filed 2026-09-14 after an owner question on ORCL, rewritten 2026-10-01 against the "exit on ALIGNMENT, never on a target" ruling.**

Why it was rewritten. Four of the six original criteria were written before the 2026-09-30 ruling and three of them ask for exactly what the ruling bars (sell all at target; sell half at target; target tightens the trail). They are VOID, not deferred — tracking a target-based rule is only useful if the desk might one day adopt it, and it may not. The third original criterion asked for a ruling on whether a target plus a confirmed breakdown may exit; the 2026-09-30 ruling IS that ruling, and the answer is no. The trail-tightness criterion was already pointed at items 90 and 185 and stays there.

What was checked against live code on 2026-10-01, because the filing's own claims had rotted. The target still reaches no broker (`src/api/holding_why.py` records this and it is still true: no caller passes `take_profit_price` to Alpaca). But the claim that the target gates whether the structural trail runs is now FALSE — `src/risk/trailing.py` removed that gate under item 142 and the structural trail runs for a range trade once it is past the owner-ratified +2R ratchet trigger, target or no target. One live effect survives: whether price has exceeded the target decides whether the +1R lock floor constrains that trail, so a number the desk calls made-up still moves a live stop. That is now said in the owner-facing note rather than contradicted by it.

DONE WHEN:
  - [x] the target's one remaining live effect (gating the +1R trail floor on a range trade) is stated wherever the target is shown to the owner, and pinned by a test — done 2026-10-01, `src/api/holding_why.py` + `tests/test_holding_why.py`
  - [ ] the desk records, for every open position every session, the alignment-exit reading it already computes (how far below the last mark price has closed, in that name's own ATR) EVEN WHEN it does not trigger an exit — the desk today keeps no trace of a position that weakened and recovered, which is the only population a partial could ever be read off
  - [ ] that record has been read once, and the answer written into `docs/BOARD_NOTES.md` (item 75): either positions do pass through a durable intermediate band of weakening before the trend ends, in which case a trim has something to key off, or they do not, in which case the alignment exit is the whole answer and this item retires
  - [ ] no trim fraction is chosen before that record exists; two derivations were attempted on 2026-10-01 and both failed, and the reasons are written down in `docs/BOARD_NOTES.md` (item 75) so neither is retried blind
  - [ ] nothing here is fitted to the desk's own trading record, and nothing ships alone
detail: docs/BOARD_NOTES.md (item 75)

**76. PM-input shape: the one open piece is whether the PM uses its new macro-audit channel. OPEN, moved out of the PM TEST GATE 2026-09-14.** Write-up: `docs/INCIDENT_HISTORY.md`, 2026-09-13/14.

DONE WHEN:
  - [ ] BLOCKED and cannot close by building — a before/after benchmark of whether the PM actually uses `reasoning_chain.macro_audit` is a paid run, and the owner's 2026-09-15 decision is that no test-environment work happens unless he asks. Same blocker as the model-seat decision line at the top of this file and 18(a); one authorisation would release all three.
  - [ ] it is not reopened as a prompt-size problem
detail: docs/BOARD_NOTES.md (item 76)

**78. Delete the blank-falsifier isolate once Tech and the PM demonstrably produce a real falsifier — DEFECT (patch), instance of the missing-data standing principle.** The isolate is live and declares itself TEMPORARY: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py:2817`) drops any constructed BUY/SHORT whose falsifier is blank.

DONE WHEN:
  - [ ] the never-blank path is live: a falsifier blanked by a later wipe is healed back from the sentence the model already wrote, the seat is re-asked once (paid), and a still-blank name is REFUSED before the book — never invented, and never with skip-and-continue as the product
  - [ ] LIVE-BLOCKED, the same shape item 86 was before a live log line retired it on 2026-09-26: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py`) is deleted only once a real live session records the seats filling the box, and the item stays OPEN until a live session proves it
  - [ ] MEASURED 2026-09-30 against the live database, condition NOT met: the technical seat still returns a blank `thesis_invalid_if` on 60% of the stocks it answered on 2026-09-29 (134 of 223) and 54% on 2026-09-28 (14 of 26), which is no better than the 30-73% daily range it ran at before the wrapper-object schema landed on 2026-09-25, so the tightened answer format did not make the seat produce a falsifier; on the narrower set of names that actually became targets the seat was still blank 5 times in 75; and criteria 2 and 3 in the docstring of `_isolate_empty_soft_exit_entries` CANNOT BE EVALUATED AT ALL because no soft-exit heal row has ever been written — all 56 `seat_heal` rows in the database carry gate `seat_heal` for the news, smart-money and technical seats and none of them is the soft-exit gate, so before this item can be judged again the soft-exit heal path must record its own outcome row (`not_attempted`, `cap_blocked`, `failed`, `paid_retry`) per name per session.
  - [ ] MEASURED AGAIN 2026-10-01 against the live database (specialist_evidence, 13,815 rows total; 327 technical-seat analysis rows since 2026-09-26): condition STILL NOT met, the technical seat returned a blank or `unknown` `thesis_invalid_if` on 53 of 78 stocks (68%) on 2026-09-30, after 134 of 223 on 2026-09-29 and 14 of 26 on 2026-09-28, so the blank rate has not fallen; the portfolio manager emitted a falsifier on all 2 targets it wrote on 2026-09-30, but 2 is too few to demonstrate anything; zero `soft-exit missing after retry` refusals and zero `soft_exit_heal` rows exist, so criteria 2 and 3 are still unevaluable. Do not re-measure until the soft-exit heal outcome row exists.
detail: docs/BOARD_NOTES.md (item 78)

**90. Unsourced trade-governing numbers — the GATE now exists; re-deriving the numbers does NOT. TIER 1, half shipped 2026-09-18, item stays OPEN.** **Half one, DONE:** every numeric definition site in scope must carry a `config/number_ledger.yaml` entry saying where it came from, or `pytest` fails.

**2026-09-30 — `risk.min_stop_atr_multiple` (2.5): the value is UNCHANGED, the claim that it was SOURCED is withdrawn, and the reformulation is filed as item 199 rather than refused.** Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").
  - [ ] 2026-09-30, second pass: the floor's VALUE is untouched and the evidence to judge it is now recorded per closed trade (entry price, entry ATR, the entry stop and its basis, and the maximum ADVERSE and FAVOURABLE excursions, alongside the realised outcome and stop-hit category already stored; the ATR multiple is recomputed from those, not stored again), and the pipeline's stale 1.5 fallback is closed at source by reading the declared default instead of a copied literal; the record is for FALSIFICATION only (was the floor ever violated in practice) and may NOT be optimised against, so the next pass reads it rather than re-deriving a multiple. Detail: `docs/BOARD_NOTES.md` ("item 90 — the 2026-09-30 `min_stop_atr_multiple` pass").

DONE WHEN:
  - [ ] 2026-10-01: the CLASSIFICATION is now mechanical and ratcheted, and it says the item is further from closing than the status field implied. `src/number_sources.py` partitions every ledger row into item 90's three states and a fourth that is the defect — a live number in none of them — and `config/number_ledger_route_history.yaml` ratchets that fourth count for EQUALITY, the same shape as `MAX_ARBITRARY_ENTRIES` and for the same reason (a hand-kept literal drifts from its record; a ceiling rewards deleting the row instead of answering it). Measured from the ledger on 2026-10-01: 329 rows — 109 not trade-governing, 82 sourced or measured, 3 ratified as a bound, 1 in state 3 with a named recording, and **134 in none of the three states**. An `arbitrary` row may now declare `settles_by:` (kind, state built/specified, where, what it records, what closes it); a malformed route is a hard build failure, because a route that cannot be acted on reads as an answer and quietly removes the row from the outstanding count. NO number was derived, moved or re-picked in this pass.
  - [ ] REMAINING WORK, named by the check rather than by prose (listing 134 ids here would duplicate the ledger): the 134 routeless rows are enumerated by `classification()["unclassified"]`, and sit in `src.config` (36), `src.agents` (30), `src.risk` (21), `src.pipeline` (18), `src.portfolio_constructor` (9), `src.execution` (8), `src.data` (5), `src.verdicts` (4), `src.pipeline_stages` (2) and `src.rotation` (1). Each closes by gaining a `settles_by:` route or a source, never by deletion and never by a derivation campaign — the repeated failure this item records is that these numbers cannot be derived from data the desk never recorded, so the unit of work is now the RECORDING, one appended negative delta at a time.
  - [ ] the only row in state 3 today is `src.config.RiskConfig.min_stop_atr_multiple` (2.5), whose settling recording was BUILT on 2026-09-30 (per-closed-trade entry stop, basis and excursions, `src/storage/db.py`); its value is untouched and the recording is for FALSIFICATION only.
  - [ ] half two: every `status: arbitrary` row in `config/number_ledger.yaml` is sourced, measured, owner-ratified as appetite, or reformulated away, and `MAX_ARBITRARY_ENTRIES` — an EQUALITY, not a ceiling — reaches zero
  - [ ] HALF TWO IS SPLIT INTO TRANCHES, EACH WITH ITS OWN CRITERIA: items 182 (de-lever ladder and alert), 183 (order-placement gates and dead cash-sweep config), 185 (trailing-stop numbers and the volatility-eligibility question) and 186 (portfolio/cluster ceilings and three owner-appetite answers) are NOT pointers — each carries DONE WHEN criteria this item does not repeat. Item 90 ticks when all four are fully ticked and no `status: arbitrary` row remains; do not re-derive a constant here that belongs to one of them. (Corrected 2026-09-30: this line used to say the four carry one word-for-word criterion, which was false.)
  - [ ] half one is already DONE (2026-09-18): the ledger gate exists and the build fails on an unsourced trade-governing number. Its honest limit stands recorded — it proves a reason was WRITTEN, never that the reason is TRUE — and that limit is not something this item can close.
detail: docs/BOARD_NOTES.md (item 90)

**107. Prompt drift the new check cannot see, and prompt-only numbers. Filed 2026-09-17; parts (a) and (c) SHIPPED 2026-09-26, (b) still open.** Reasoning and what was ruled out: `docs/INCIDENT_HISTORY.md`, 2026-09-17 and 2026-09-26. **Do not re-propose the three designs rejected on 2026-09-17, and do not build a second deletion-site grep — that one exists.**

DONE WHEN:
  - [x] (a) a behaviour that CHANGES without being deleted forces the prose describing it to be re-read — `src/prompt_bindings.py` + `config/prompt_bindings.yaml` pin code and prompt prose to each other by digest and fail the build when one side moves; the deletion-site check was checked for first and left alone
  - [x] (c) all ten standing sheets are covered by the rendering check, an unregistered sheet is refused, and a placeholder no renderer can resolve fails in CI instead of at agent construction on a live morning
  - [x] the 12/10 drift pair has one definition (`src.risk.metrics.DRIFT_WEIGHT_PCT` / `DRIFT_PNL_PCT`), both sheets render it, and the one remaining literal site is named in the test so the count can only go down — `src/pipeline.py:_build_position_facts`, not editable in that pass
  - [ ] the last literal drift-threshold site in `src/pipeline.py` uses the named constant
  - [ ] (b) the trade-picking sheet's prompt-only sizing arithmetic (bases 3.0/1.75/0.75 and their ranges, the 0.25 reward:risk bonus, the 0.5 stale halving at age >=8d, the +/-0.5pp shade) and the technical sheet's prompt-only levels ("3+ aligned signals", the 1-3/4-7/8+ freshness tiers, forward-PE 40/60 and P/S 15/25) are each sourced or taken OUT of the path — verified 2026-09-26 that no code computes any of them and none is in the number ledger. The +/-0.20/+/-0.10 evening tilt this item listed was already gone from the formula (2026-09-17); its dangling mention was removed 2026-09-26
  - [ ] the 12/10 drift pair and the two prompt-only sets above carry a `config/number_ledger.yaml` entry with a source, or the open question and what the desk pays meanwhile
detail: docs/BOARD_NOTES.md (item 107)

**119. The economics feed can leave required series un-attempted at the open, and the re-derived fix is only measured mid-morning — OPEN, filed 2026-09-18.** Re-filed out of PR #435 (closed unmerged).

DONE WHEN:
  - [ ] every required series demonstrably gets a real attempt inside the existing ceiling, proven against open-like conditions rather than a healthy mid-morning batch
  - [x] (b) a series still missing is named, the verdict it produced is visibly partial wherever it travels, and the desk never pays a second time on the same holes — RESTATED and met 2026-09-26. "The economist is not paid on the holes" was DECIDED AGAINST on measurement: the macro seat is bought once a day at the open and nowhere else, so skipping it deletes the regime frame rather than delaying it, and both measured partial runs had already burned the full ceiling so there was nothing bounded to wait for. The economist is paid once on what arrived; `MacroCoverage.verdict_stamp()` stamps `coverage_state`/`coverage_note` onto the verdict from the fetch record, and that stamp now survives `MacroStore.save_last_state`, the carry-forward into midday/close/intra, the PM sheet and the owner's `📊 Market:` line. A second paid call on the same holes was already impossible (`"partial"` is `CATEGORY_REPORTED`, never healable) and is now pinned by a test instead of left to the table. No coverage threshold was picked; 14/15 and 1/15 are both `partial`. Measurement, money and the argument against the other three options: docs/BOARD_NOTES.md (item 119)
detail: docs/BOARD_NOTES.md (item 119)

**203. A provider success with no usable cost or token telemetry — carried over from item 147 (2026-09-30), zero occurrences measured across all of  as of that date.**

DONE WHEN:
  - [ ] a success whose provider request DID happen but returned no usable token or cost telemetry is understood and either priced from a fallback source or proven free and excluded from unknown-cost counting, the same evidentiary standard item 147 set for cache hits.
detail: docs/BOARD_NOTES.md (item 203)

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

**185. Trailing-stop numbers are made-up money numbers with no board item — filed 2026-09-25. OPEN.**

DONE WHEN:
  - [x] `CHANDELIER_ATR_MULTIPLE` sourced (published Chandelier default) and `MIN_RATCHET_PCT` owner-ratified as churn appetite with its false cost premise corrected
  - [x] the 50%-of-price literal is removed from the code and from the ledger, and the guard/screen circularity is broken — both ends now compute from `widest_reachable_stop_atr_multiple` (2026-09-30)
  - [x] the screen-divisor mismatch (base 2.5 vs reachable 3.00, the 16.67-20% band the screen admitted and the guard would have argued with) is FIXED, not accepted
  - [x] no refusal on this path can leave a position unprotected — the midday guard clamps and places instead of skipping, per board item 80 (2026-09-30)
  - [ ] the ELIGIBILITY question is BLOCKED, not declined — 2026-10-01 names the blocker and the recording that lifts it, and no further derivation attempt is permitted. Blocked because: no primary methodology document stating an ABSOLUTE ATR/price bound exists to cite (searched 2026-09-30); the published form is cross-sectional and the screen has NEVER executed, so there is no cross-section to take a quantile of; and routing the quantile to the owner as appetite is barred by his 2026-09-30 ruling that risk is per name and never a global dial. CLOSES WHEN: the screen runs and persists each run's full ATR(14)/price cross-section with names and date, giving the quantile form a measured distribution. Until then 33.3% is an arithmetic non-degeneracy floor ONLY and must not be described as a volatility appetite — re-measured 2026-10-01 off the desk's own stored daily bars (101 symbols, 276 sessions to 2026-09-30): median 2.53%, p90 5.50%, max 8.33%, not one name above 10%, so it has never bound and would not
  - [x] the two rows the 3.00 depends on are explicitly INHERITED by this item rather than left orphaned (2026-10-01), and the 1.20 risk-off scaler's open question was found to be the barred maximum-adverse-excursion study item 90 had already deleted from `min_stop_atr_multiple` — it is replaced by a lag-correction reading off the instrument (ATR(14) already re-measures volatility every session, so the scaler may be double-counting and deleting it is a legitimate outcome), the recording that would settle it is named, and `tests/test_no_fitted_open_questions.py` now fails the build on any ledger open question promising a fitted resolution

detail: docs/BOARD_NOTES.md (item 185)

**186. Portfolio and cluster risk ceilings are made-up money numbers with no board item — filed 2026-09-25.**

DONE WHEN:
  - [x] the three already-ratified ceilings (25 / 90 / 40) stay ratified, and the remaining three are researched to a definite verdict rather than left unexamined
  - [x] WITHDRAWN 2026-09-30 — the pairwise-correlation appetite question is moot: the cutoff is removed, not set. Cluster membership is now read from the book's own correlation-distance tree (Mantegna MST cut at its widest gap), per the owner's ruling that risk tolerance is never a global dial. Still transitive, still rationing only.
  - [x] 2026-09-30 OWNER RULING APPLIED: risk is never a global dial, so no appetite number on this item is routed to the owner any more; each remaining ceiling is either replaced by a per-name read or recorded as blocked with its blocker named. Both previously routed questions are WITHDRAWN, not pending
  - [ ] `short_gap_risk_multiple` (1.5) becomes a read off that stock's own overnight-gap behaviour instead of one constant for every short — BLOCKED on stored daily bars, which the desk does not keep (the constructor is handed `analysis.atr_14` and no bar history, verified 2026-09-30). No value picked, no appetite asked
  - [ ] the queued-earnings BUY clamp (5% of the book) stops being a global share — the structural alternative identified 2026-09-30 is that an unread filing IS an unconvicted seat, which under standing doctrine (all five seats right to enter) bars the BUY rather than sizing it; it changes live sizing behaviour, so it needs an adversary pass before it ships and was NOT applied in this pass

  - [x] the portfolio and cluster ceilings (25 total at-risk, 90 terminal sector and its constructor mirror, 40 cluster share) each end in a definite state rather than as an open appetite question — 2026-10-01: values unchanged and still owner-ratified, every appetite question WITHDRAWN under the 2026-09-30 ruling, both failed derivations written down per ceiling (two of them algebraic cancellations: 25 is five full-size names and 40% of 25% is two, at the ratified 5% per-trade envelope), and each row now names the recording that would settle it with the route ratchet moved to match. Do not re-derive these three
detail: docs/BOARD_NOTES.md (item 186)

**187. FRED fetch reliability — the chronic `fetch_deadline_exceeded` failure and required series left un-fetched — filed 2026-09-25, carried out of item 175's retirement. Item 175's weekend/holiday overdue-date roll shipped and was retired; this is the separate, still-open half. Detail: `docs/BOARD_NOTES.md` ("item 187").** Every FRED failure in the retained log is `fetch_deadline_exceeded`; 4 of 12 runs reached full coverage, worst 5 of 15 [measured 09-17..23]. Owned by the approved fetch redesign.

DONE WHEN:
  - [ ] the `fetch_deadline_exceeded` rate is understood and either brought down or shown to recover cleanly inside the existing time ceiling, measured against real runs rather than a healthy mid-morning batch
  - [x] the FRED SERIES half is fixed and live: fair-share reserves plus the pre-open series cache; the deployed box ran 2026-09-30 with no series skipped.
  - [x] the EVENT-CALENDAR half is fixed here: the seven `/fred/release/dates` calls move off the trading path onto the existing pre-open prefetch timer and are served from `data/macro/release_schedule_cache.json` at the open; a release in neither cache nor wire stays a named failure and is never defaulted.
  - [ ] ONE morning open (N = 1, the number this item already stated) recorded in the production table `fred_fetch_coverage_runs` with `full_coverage = 1`: all configured series returned, `series_not_attempted` empty, and every configured release returned with `releases_from_cache` equal to `releases_configured`. Check: `SELECT * FROM fred_fetch_coverage_runs ORDER BY id DESC`. Rows exist only from the first open after this deploys; a row with `full_coverage = 0` does not count, and a missing row is not a pass.
detail: docs/BOARD_NOTES.md (item 187)

**188. The decision seats' last-resort route is now a small free model, and nobody has measured it at those seats — filed 2026-09-30.**

DONE WHEN:
  - [x] no seat has every reachable route on one provider, enforced mechanically against `config/settings.yaml` rather than by reading the config by eye
  - [ ] every decision seat persists, beside the responding model, whether its answer passed that seat's own acceptance gate and why it did not — the database, not a paid benchmark, is what makes the substitute measurable (2026-09-30: only 3 free-model answers exist at these seats and no acceptance verdict is stored beside ANY model, so no rate is computable; 2026-10-01 re-read: still exactly 1 per seat, all three well-formed with every required field but n=1 proves nothing, so this recording is now the ONLY closing condition and the measurement box waits on it)
  - [ ] with that recording in place, the substitute is either measured at the three decision seats from the desk's own rows, or the seats are made to refuse rather than answer when only that route is left — decided on the measurement, not on a guess about how bad it is

detail: docs/BOARD_NOTES.md (item 188)

**190. The disabled cash-sweep / T-bill feature needs full retirement, not just its reachable band — filed 2026-09-30, carried out of item 183's dead-config finding. Detail: `docs/BOARD_NOTES.md` ("item 190").**

DONE WHEN:
  - [ ] every reference to the cash-sweep / T-bill feature (`CashSweeper`, `CashSweepConfig` and its fields, the dead pad/buffer constants, and the deployment-gap advisory fields that read `reserve_pct`) is either removed or re-justified as still reachable, across the pipeline, the API and the nine test modules item 183 identified
  - [ ] `config/number_ledger.yaml` rows for the constants that die with it are deleted rather than left describing code that no longer exists
  - [ ] MEASURED 2026-09-30, the premise is PARTLY WRONG and the scope is corrected here: this is not a pure deletion of switched-off code. `CashSweepConfig.reserve_pct` has a live non-sweep consumer — `src.risk.rules.deployment_gap_band_pct` reads it as the tolerance band for the `deployment_gap` advisory on EVERY session, and `/account` renders it as `reserve_usd` / `cash_above_reserve` via `src.api.deps.get_cash_sweep_reserve_pct`. Deleting the sweeper therefore cannot delete the band by default: the band must either die with the advisory (a separate call, since "how far under 100% still counts as fully invested" is appetite-shaped, not bookkeeping) or be given a home outside the retired feature. Item 183's band question is NOT resolved by this item as filed
  - [ ] MEASURED 2026-09-30, `cash_sweep.min_order_usd` is vestigial as a trade gate but NOT as prose: `src.pipeline_stages._min_order_usd` records (2026-09-24) that none of the three paths that used to reject a small trade still do, and `apply_gross_ceiling` accepts it only as an ignored parameter — but `src.agents.portfolio_manager` still SPEAKS it to the owner in the funding narrative ("under the $N minimum order worth placing"). It dies with the feature, and those owner-facing strings must be rewritten rather than merely dropped
  - [ ] MEASURED 2026-09-30, the 187-reference estimate is LOW. A live count over the checkout is ~550 mentions of `cash_sweep` / `CashSweeper` / `cash_sweeper` / `_sweep_symbol` / `SGOV` / `reserve_pct` across 88 files, including four frontend components (`LiquidityPanel`, `HoldingsStrip`, `DecisionStateBanner`, `funnelShared`), the Mission Control API schema and routes, `ops/preview/branch_preview.py`, and ~30 test modules rather than the nine item 183 named. The `sweep-vehicle liquidation before a BUY` entry already in `config/retired_mechanisms.yaml` is the registry hook the deletion must extend
  - [x] STEP 1 DONE 2026-09-30 (PR item190-step1): the `deployment_gap` advisory band now reads `deployment_gap.band_pct` (1.0, value unchanged) instead of `cash_sweep.reserve_pct`; the band stays as owner-appetite, ledgered `arbitrary`. Remaining: steps 2-4 below. `reserve_pct` itself is still read by the sweeper and the /account `reserve_usd` display until step 3/4.
  - [x] STEP 2 DONE 2026-09-30: portfolio-manager and rotation funding prose no longer quotes the $500 `min_order_usd` (logic and number unchanged). STEP 3 PARTIAL 2026-09-30: `CashSweeper.fund_buys`, `park_excess`, their pipeline/stage call sites and their tests are deleted; STILL TO DO: the `sweep-vehicle liquidation before a BUY` registry entry must move to `retired:` and the three seat prompts that say the vehicle is auto-liquidated must be reworded; the enabled/view hooks (`_sweeper()` consumers, `split_positions`, `reserve_usd`) remain; `release_retired_vehicle` and `_retired_cash_park_symbol` are LIVE (run every session to sell a leftover vehicle and exempt it from the stop audit) and must stay until the vehicle is confirmed not held.
  - [x] STEP 4 PART DONE 2026-10-01: the `/account` liquidity view no longer counts the parked sweep vehicle as deployable cash when the sweep is disabled. MEASURED against the live checkout: the engine's `TradingPipeline._compute_deployable_cash` adds 0.0 when `_sweeper()` returns None (which it does on `enabled: false`), and `CashSweeper.fund_buys` returns 0.0 on its first line, so nothing converts the vehicle back to cash for the BUY phase; `src.api.routes_live._compute_liquidity` added it unconditionally and so would have read ABOVE the figure the PM sizes against. LATENT, never an incident: the production DB (`/home/qamc/quant-agent/data/quant_agent.db`, read-only, 2026-10-01) holds 0 SGOV position rows (14 historical SGOV trades). Guarded by `tests/test_single_definition_quantities.py::test_disabled_sweep_does_not_inflate_the_dashboard_deployable`
  - [ ] STEPS 2 AND 3 ARE NOT MERGED ON `origin/main` — verified 2026-10-01 at `bf1a7a83`: `src/execution/cash_sweep.py` still exists, `TradingPipeline.__init__` still constructs `CashSweeper`, `src/pipeline_stages.py` still calls `sweeper.fund_buys`, and `src.agents.portfolio_manager` still speaks `min_order_usd`. The REST of step 4 is therefore blocked, not skipped: deleting the `config/number_ledger.yaml` rows for `_BUY_LIMIT_PAD`, `_FUND_BUFFER_FRAC`, `_FUND_BUFFER_MIN_USD`, `_FUND_CASH_SETTLE_*`, `_FUND_TERMINAL_TIMEOUT_S`, `_SELL_LIMIT_PAD`, `CashSweepConfig.reserve_pct` and `CashSweepConfig.min_order_usd` would leave live constants unledgered, and dropping `reserve_usd` / `cash_above_reserve` / `sweep_parked_value` from `/account` and the frontend would remove the only surface that shows a vehicle still held under a retired sweep. Remaining criteria for this item: steps 2 and 3 land, THEN the ledger rows and the API/frontend sweep fields go in one pass with the `config/retired_mechanisms.yaml` entry extended
  - [ ] the retirement is sequenced so no step leaves a half-wired feature: (1) settle the `reserve_pct` band and the `deployment_gap` advisory, (2) rewrite the portfolio-manager funding prose off `min_order_usd`, (3) delete `CashSweeper`, its pipeline/stage wiring and its tests-of-the-dead-path, (4) drop the ledger rows and the frontend/API surface

detail: docs/BOARD_NOTES.md (item 190)

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
  - [x] 2026-09-30, the RECORDING exists, and it — not another re-derivation — is what this item now turns on: every position the desk opens pins its entry price, entry ATR, the stop placed at entry and that stop's basis (the constructor's own `stop_rule`, which already separates a stop sitting on a COMPUTED structural level from one set by the ATR band), and every position accumulates its worst AND best excursion while open, joining the realised outcome and the `broker_stop_fill` category already on the exit row. The stop's distance in ATR multiples is RECOMPUTED from entry price, entry stop and entry ATR rather than stored a second time, per "never store what code can recompute". Anything genuinely unavailable at that moment is stored NULL, never substituted. Two hard caveats any reader must carry: the excursions are accumulated from session snapshots, so each is a FLOOR on the true figure (a reading that says the floor WAS violated is trustworthy; one that says it was not is only "not observed"), and legacy rows predating the columns are NULL. FALSIFICATION ONLY — this record may show whether the ratified floor was ever violated in practice and may NOT be swept for a better multiplier; doctrine bars fitting a number to this desk's history.
  - [ ] the share of real candidates that have a computed level below entry at any touch count is measured from production data, so the size of the population this actually removes from the ATR multiple is known rather than assumed
  - [ ] the far-anchor case is decided and written down: what the floor does when the nearest level below entry is distant enough to shrink the position materially, including whether the flat multiple remains as a ceiling on the widening
  - [ ] `config/number_ledger.yaml`'s entry for `src.config.RiskConfig.min_stop_atr_multiple` records the outcome, and either its status changes or its note states exactly which population it still governs
detail: docs/BOARD_NOTES.md (item 199) — item 90's ledger entry carries the retracted arguments so they are not re-proposed

**201. The rest of the cancel+resubmit stop path — filed 2026-09-30 alongside the in-place amend fix. Detail: `docs/BOARD_NOTES.md` ("item 201").** `replace_stop_loss` now amends one plain resting protective stop's price atomically, but every other stop-moving path (the fractional hybrid pair, `shift_stops_down`, multi-stop positions, bracket/OTO legs) still cancels then resubmits and still opens an unprotected window.

DONE WHEN:
  - [ ] each remaining cancel+resubmit stop path is either measured against the broker and converted to an in-place amend, or documented as genuinely unable to amend
  - [ ] the fractional hybrid pair's two legs are measured for whether both can be amended in place without collapsing to one stop
  - [ ] a test fails if any converted path cancels before its amend is refused

detail: docs/BOARD_NOTES.md (item 201)

**200. The status board's own file was one change away from blocking every other change — filed 2026-09-30. OPEN: the move is made, the guard against it recurring is not.**

DONE WHEN:
  - [x] `docs/WORK.md` is back under 70% of its cap by MOVING argument, history and measurement out of open items — not deleting it, not raising the cap — with every moved byte proved verbatim in `docs/BOARD_NOTES.md` by a line-level diff and the rendered owner prose unchanged block-for-block
  - [x] the cap and the growth budget are named as what they are: both PICKED, not derived (100,000 gave ~5% headroom over a measured 94,801; the 0.5 growth share calls itself provisional), and both allowed to be picked because a documentation size limit governs no money
  - [ ] the move is REPEATABLE without a human deciding what to carve: nothing yet stops the same items re-accreting history in place, so the next time the cap binds it will again be hand-work
   — the file passing (say) 80% should say so in the same place the growth-budget failure already speaks, rather than the first warning being a blocked merge

detail: docs/BOARD_NOTES.md (item 200)

**202. The rehearsal harness is not hermetic — a replay of a RECORDED session still reaches live providers — filed 2026-09-30.** Closed so far: the curl_cffi hole, recorded daily bars, and (2026-10-01) rebinding the market provider on the morning-research stage, which held its own reference and so kept the live one after the swap — tech_analyst now runs offline [measured 2026-10-01]. `tests/test_rehearsal_reproduces_cost_ceiling.py::test_the_settled_cost_ceiling_still_suspends_paid_analysis` still XFAILs. detail: docs/BOARD_NOTES.md (item 202)

DONE WHEN:
  - [ ] the run reaches the Portfolio Manager offline instead of ending `APIConnectionError: Connection error.` — find what still calls a live provider there and serve it from the recording or fail loudly
  - [ ] no component builds its own live MarketDataProvider during a rehearsal: blocked yfinance crumb fetches still retry per symbol and cost ~188s [measured 2026-10-01]
  - [ ] any OTHER test that still reaches the network is named, because the conftest guard now makes such a dependency fail loudly instead of silently


**208. Item 18's three residuals, carried forward — filed 2026-09-30 when item 18 was retired. The prompt-bulk defect that item 18 was opened for no longer applies and was re-measured under that item; these three leftovers remain OPEN, share no subject with it and were blocking item 19 for no reason. Detail: `docs/BOARD_NOTES.md` (item 208).** One changes what the ranking seat decides, one is an account setting outside this repo, and one cannot be closed by building at all.

DONE WHEN:
  - [x] (a) DONE 2026-10-01 — decision recorded: neither joins the composite (see docs/INCIDENT_HISTORY.md 2026-10-01); originally: a recorded decision, in `docs/INCIDENT_HISTORY.md`, on whether reward:risk (today a within-tier tiebreak) and net evidence (unused) join the ratified composite score — this is engineering under doctrine, not an owner call; per-seat sizing weights stay refused either way
  - [ ] (b) the OpenRouter key carries a provider-side spend cap, or its absence is recorded as accepted — the account is outside this repo, so it closes on an observation in the provider console, never on a test
  - [ ] (c) BLOCKED and cannot close by building — the BUY-eligibility section reorder needs a paid benchmark run the owner has forbidden unless he asks for it (same blocker as items 76 and 77); it stays open and untouched until he raises it
detail: docs/BOARD_NOTES.md (item 208)


**211. Alarm flapping — the desk paged the owner on BOTH edges of a self-clearing fault, and he muted every alert — filed 2026-09-30.** 107 Telegram messages went out between 26 and 29 Sep, 46 on the 28th and 42 on the 29th [measured, production `notifier_sends`]. 22 "PAID ANALYSIS SUSPENDED" and 22 "PAID ANALYSIS RESUMED" of those are ONE underlying fault — paid provider calls failing — latching and self-clearing all weekend, announced twice per cycle, plus 5 identical deploy-drift repeats from a timer-run unit that had no per-type suppression at all. The owner turned every desk alert off, including live-risk ones, so this defect is currently suppressing the alerts that protect money. FIXED HERE: `LLMCostCircuitBreaker._notify_if_needed` defers the owner page for a `_SELF_CLEARING_HARD_TRIGGERS` latch until it has outlived `transient_latch_cooldown_minutes` — the circuit's OWN self-clear timing, read from the same config field `_auto_clear_transient_latch_locked` gates on, not a threshold picked here. A latch that expires inside that window leaves `alert_state` at 0, which the existing item-174 pairing already reads to suppress the matching "RESUMED" note, so a blip is one recorded episode and zero messages. Both owner-facing messages now carry the episode's duration and how many times the same trigger self-cleared today. Nothing is dropped: the trip event, a once-per-latch `suspend_alert_deferred` event and the CRITICAL log line all still land in `llm_circuit_events`, and `scripts/check_deploy_drift.py` now claims through a new GENERIC per-type, per-key, ET-day marker (`coverage_watchdog.claim_typed_alert`) that writes every refused claim to `suppressed_alerts` in the watchdog state file.

detail: docs/BOARD_NOTES.md (item 211)

DONE WHEN:
  - [x] a transient provider latch that self-clears inside the circuit's own self-clear window sends the owner NOTHING and is still fully recorded
  - [x] the durability threshold is read from `transient_latch_cooldown_minutes`, so changing the self-clear timing moves the paging threshold with it
  - [x] the suspension and resume messages both state the episode's duration and its self-clear count
  - [x] repeat suppression is per alert TYPE and per key, never global, so one noisy fault cannot silence an unrelated one
  - [ ] the `suppressed_alerts` record and the `suspend_alert_deferred` events are surfaced on the read-only API/dashboard — NOT DONE HERE, they are durable in the state file and the DB but no endpoint reads them yet

**214. Nobody has read the technical seat's schema-hygiene counters, so whether the Google route actually honours the sent schema is still unanswered — filed 2026-09-30, OPEN, carrying item 157's first criterion.** Item 157's enforced answer format shipped on both wire routes, but its live-confirmation criterion could never run: no deployed process holds a real Google credential for a pytest call. `_record_answer_hygiene` was shipped instead and records fenced-markdown and undeclared-key hits per provider on every real call; nobody has since looked at what it recorded.

DONE WHEN:
  - [ ] the recorded hygiene counts are read off production for both the openrouter-tagged and google-tagged calls, over a stated window
  - [ ] a conclusion is written down on whether the Google route enforces the sent schema, or the counts are shown to be too sparse to conclude
detail: docs/BOARD_NOTES.md (item 214)


**210. A properly structured codebase, built in the right order — ratified by the owner 2026-09-30: no parallel split and no rebuild; drain the open pull requests, then split the two oversized files as the only work in flight, and rebuild the tests in the same pass. Detail: `docs/BOARD_NOTES.md` (item 210).** The desk's behaviour is not what is broken; two oversized files and too little recorded evidence are.

DONE WHEN:
  - [ ] 1. the open pull-request queue is at zero, because the split moves `src/pipeline.py` and `src/pipeline_stages.py`, which nearly every open pull request touches, so splitting sooner collides with all of them
  - [ ] 2. the split is executed as the ONLY work in flight, following `docs/PIPELINE_SPLIT_PLAN.md` (9 modules out of `src/pipeline.py`, 8 out of `src/pipeline_stages.py`, 12 ordered steps), each step landing on its own so the tree is never half-moved
  - [ ] 3. the test suite is rebuilt in the same pass, and no test is left patching a name on the pipeline module that has moved: 42 tests patch `pipeline.compute_indicators` and 20 patch `pipeline._get_sector`, and once that code moves they patch nothing, quietly run the real implementation and still pass [measured 2026-09-30, grep of the tests]
detail: docs/BOARD_NOTES.md (item 210)


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
- retired queue: 147
- retired queue: 77
- retired queue: 183
- retired queue: 182
- retired queue: 192
- retired queue: 195
- retired queue: 196
- retired queue: 109
- retired queue: 19
- retired queue: 99
- retired queue: 157
## Evidence-only follow-ups — reopen only on concrete production evidence

- news-narrative factual drift; `actual_provider` attribution oddity.
