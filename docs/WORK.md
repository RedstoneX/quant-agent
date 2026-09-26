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
- **Tier 1, can cost money or hide risk:** 89 (its residue), 80, 90, 111, 112, 127. (87 and 88 closed 2026-09-18; 130 retired 2026-09-25.)
- **Tier 2, wastes money or opportunity:** 91, 82, 81, 92.
- **Tier 3, clarity and hygiene:** 89 (its thirteen clarity defects), 94. (93 retired 2026-09-25.)
- **To be decided by the orchestrator after an adversary run, not parked on the owner (ruling 2026-09-18 — his words: "I don't want you waiting on me on anything. You have the adversary in my place. Just make sure it gets documented." The adversary argues, it never rules; the orchestrator decides and records the decision and its reason before anything is built on it):** 86, 95, 109(a). (96 retired 2026-09-25 — the delegated question is stale, the veto it asked about is built and wired.)

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**Top of the backlog. Work the PRIORITY ORDER above; do not reorder from intuition.** The original census items (ranks 1-8) are all retired and written up; the measured census that ranked them is in `docs/INCIDENT_HISTORY.md`.

**17. Backup alert channel — OWNER DECISION, deferred, no due date.** No channel exists beyond Telegram, so an alert that cannot reach Telegram reaches nobody.

DONE WHEN:
  - [ ] OWNER'S CALL — his own 2026-09-03 deferral, no due date: a second alert channel is new scope and, for anything but plain email, a new paid dependency, so nobody proposes it and it closes only when he raises it
  - [ ] when he does: the record-keeping circuit-breaker trip is shown reaching him on the second channel while Telegram delivery is failing, which is the exact live pairing that went unnoticed
detail: docs/BOARD_NOTES.md (item 17)

**18. 70% of the PM's prompt was earnings-filing prose — MEASURED 2026-09-02, PARTIALLY FIXED, core cause MERGED 2026-09-04 (PR #252), and these three follow-ons are all that is left. Closed detail: `docs/INCIDENT_HISTORY.md` (items 18a-18e).** (a) The BUY-eligibility section reorder — BLOCKED: it needs a paid benchmark the owner has forbidden unless he asks.

DONE WHEN:
  - [ ] (a) BLOCKED and cannot close by building — the BUY-eligibility section reorder needs a paid benchmark run the owner has forbidden unless he asks (same blocker as items 76 and 77)
  - [ ] (b) a recorded decision, in `docs/INCIDENT_HISTORY.md`, on whether reward:risk (today a within-tier tiebreak) and net evidence (unused) join the ratified composite score — the item's own note says this is NOT an owner call; per-seat sizing weights stay refused either way
  - [ ] (c) the OpenRouter key carries a provider-side spend cap, or its absence is recorded as accepted — outside this repo, so it closes on an observation in the provider console, not on a test
detail: docs/BOARD_NOTES.md (item 18)

**19. The model's consistency is an ASSET — three uses. Do not start these before item 18.** Measured: 5 blinded runs, two arms, quality identical to four decimal places.

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

**56. Is a stop too wide, and read off what? The THRESHOLD is still unread. OPEN, narrowed 2026-09-14.** At what probability of being touched inside the trade's own horizon does a stop stop being a stop?

DONE WHEN:
  - [ ] one of three routes lands, and only one is needed: a published measurement of the touch probability below which a stop stops being a stop, cited in `docs/RESEARCH_FINDINGS.md`; or the gate is re-expressed with no threshold at all (refuse when the stop's touch probability is below the target's reach probability on the same instrument); or the width gate is DELETED on the recorded ground that sizing already answers a wide stop with a smaller position
  - [ ] whichever lands, the two 1.5 multiples and the 1.67% touch-probability constant leave `status: arbitrary` in `config/number_ledger.yaml`, or cease to exist with the gate
  - [ ] the per-trade recorded stop-touch probability the desk now stamps on every sized trade is the evidence used, not a fresh benchmark run
detail: docs/BOARD_NOTES.md (item 56)

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN (structure shipped, magnitude calibration still open), carried out of item 52.** One scalar in [0,1] is both the ranking key and the dollar multiplier, with no direction. **STRUCTURE FIX 2026-09-25:** a derived `SmartMoneyObservation.signal_direction` channel now carries the sign (buy +1, sale/exchange/unknown 0), and both deterministic ranking keys in the smart-money analyst multiply value*weight by it, so a contra/bearish sale can no longer rank or size as a bullish buy of equal magnitude; buys keep their exact former contribution. The desk is long-only on smart-money admission (admission requires direction=="buy"), so a sale is safely neutralised, never counted as bullish. **open_question (owner-appetite/research):** the magnitude→sign boundary that would let a large sale (Scott & Xu's sourced >50%-of-holdings band, already on the row as `holdings_fraction_band`) score bearish (-1) versus a small sale's mild-positive — no published SIGNED scoring scheme exists; ruled out pending a source or enough own outcome data. Do NOT pick that number.

DONE WHEN:
  - [ ] the magnitude→sign boundary is settled by EVIDENCE, not appetite: either a published SIGNED insider-sale scoring scheme is cited and `SmartMoneyObservation.signal_direction` returns -1 off the already-reported `holdings_fraction_band` (Scott & Xu's sourced >50%-of-holdings band), or the desk's own resolved smart-money outcomes are numerous enough to read a separation from
  - [ ] until one of those exists a sale stays NEUTRALISED at 0 and no agent picks the boundary number — the standing no-arbitrary-numbers and no-fitting rules settle that, this is not an appetite dial
detail: docs/BOARD_NOTES.md (item 63)

**64. The backtest still rations the risk budget alphabetically when the budget binds — OPEN.** Live spends down the ranked verdicts; this engine has none, so equal asks are served by ticker spelling. **Ruled out, with reasons: `docs/INCIDENT_HISTORY.md`, 2026-09-16**, notably ranking by the engine's own reward:risk, which would silently change who gets capital.

DONE WHEN:
  - [ ] either the backtest ranks its candidates by the LIVE desk's own ranking rule, or a recorded decision in `docs/INCIDENT_HISTORY.md` states that this engine cannot evaluate rationing at all because it cannot replay the live verdicts. Not an owner call: the no-fitting doctrine already forbids inventing a stand-in score (including the engine's own reward:risk, ruled out 2026-09-16), so those two are the only outcomes left.
  - [ ] whichever way it goes, every backtest result keeps printing how many of its days the risk ceiling bound and that the tie-break is alphabetical, so the numbers can never be read as evidence about how the live desk picks among trades
  - [ ] this item is NOT closed as a ranking fix while the tie-break is still ticker spelling
detail: docs/BOARD_NOTES.md (item 64)

**65. Four of the five analyst seats have no strength scale of their own — OPEN, opened 2026-09-13.** Only Technical states a lean; news, macro, smart_money and earnings report no stated strength and rank on weighted conviction alone.

DONE WHEN:
  - [ ] either news, macro, smart_money and earnings each emit a per-call strength they must state and justify (the way Technical publishes buy vs strong-buy), or a recorded decision in `docs/INCIDENT_HISTORY.md` that direction plus confidence is genuinely all those four can say. Not an owner call: this is a prompt-output change under already-agreed doctrine, and the no-arbitrary-numbers rule already forbids every alternative.
  - [ ] nothing derives a strength from a field a seat already reports (double-counted conviction, deleted 2026-09-13), borrows Technical's rung, or fits one from history — and any own-data reading waits for the conviction ledger to hold 20 resolved calls per seat
  - [ ] the four seats stay in the ranking throughout; dropping them is not an answer
detail: docs/BOARD_NOTES.md (item 65)

**70. One underived `1.0` is doing two different jobs in the exit path, and neither is read off anything — OPEN, filed 2026-09-14.** The noise-band ATR multiple sets when an adverse move stops being noise and is reused as the margin in the structural-protection check; a separate absolute minimum stop multiple, also 1.0, sets how tight a stop may be.

DONE WHEN:
  - [ ] the noise-band ATR multiple carries a published measurement of the quantity it actually bounds — the adverse move at which a move stops being ordinary daily wobble — or a named derivation, recorded in `config/number_ledger.yaml` with that source
  - [ ] the absolute minimum stop multiple carries its OWN independent source or derivation, as a separate ledger entry: the two may not be collapsed into one shared constant just because the digits both read 1.0
  - [ ] the measured over-refusal is re-measured after whichever change lands, against the same recorded exits (today: 7 of 8 discretionary exits the reviewer approved were blocked as "too small a move")
  - [ ] neither value is retuned to make sales easier or harder in the same pass — how readily the desk should block a sale at all is the owner's appetite and is NOT this item
detail: docs/BOARD_NOTES.md (item 70)

**74. One piece of news can cut the same holding twice in a day, and whether that is a fault is a design question — OPEN, filed 2026-09-14.** A midday REDUCE on a hard trigger can repeat at the close on the same trigger; the pipeline only warns, and the position-reviewer prompt explicitly allows it.

DONE WHEN:
  - [ ] a decision is recorded — by the orchestrator after an adversary run, per the 2026-09-18 ruling that this class does not wait on the owner — on whether a hard trigger is spent once it has been acted on for that symbol that day
  - [ ] if it is NOT spent: what separates a genuinely worse reading of the same filing from a repeat of the same reading is named and ENFORCED in the exit-claim check, not left as a log warning plus a permissive line in the position-reviewer prompt
  - [ ] the frequency is measured against the desk's own recorded same-day REDUCE pairs — it is unmeasured today, and the 2026-05-04 AMZN double cut is the only named instance
detail: docs/BOARD_NOTES.md (item 74)

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
  - [ ] LIVE-BLOCKED, the way item 86 is: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py`) is deleted only once a real live session records the seats filling the box, and the item stays OPEN until a live session proves it
detail: docs/BOARD_NOTES.md (item 78)

**86. The live-fill `trade_updates` websocket has never once authenticated — cause found and fix SHIPPED 2026-09-18 (#517), item stays OPEN until a live attempt proves it.** 1,017 failures across three days, zero successes [measured 2026-09-18].

DONE WHEN:
  - [ ] one live log line records `trade_updates websocket authenticated`
detail: docs/BOARD_NOTES.md (item 86)

**90. Unsourced trade-governing numbers — the GATE now exists; re-deriving the numbers does NOT. TIER 1, half shipped 2026-09-18, item stays OPEN.** **Half one, DONE:** every numeric definition site in scope must carry a `config/number_ledger.yaml` entry saying where it came from, or `pytest` fails.

DONE WHEN:
  - [ ] half two: every `status: arbitrary` row in `config/number_ledger.yaml` is sourced, measured, owner-ratified as appetite, or reformulated away, and `MAX_ARBITRARY_ENTRIES` — an EQUALITY, not a ceiling — reaches zero
  - [ ] SHARED CRITERION: that is word-for-word the single criterion items 182, 183, 185 and 186 carry, because those four are this item's half two split into tranches. Item 90 ticks when they all do; do not re-derive a constant here that belongs to one of them.
  - [ ] half one is already DONE (2026-09-18): the ledger gate exists and the build fails on an unsourced trade-governing number. Its honest limit stands recorded — it proves a reason was WRITTEN, never that the reason is TRUE — and that limit is not something this item can close.
detail: docs/BOARD_NOTES.md (item 90)

**95. DELEGATED TO THE ORCHESTRATOR — may the portfolio manager plan against borrowed money? Filed 2026-09-17, ownership moved 2026-09-18. Detail: `docs/BOARD_NOTES.md` ("item 95").** Borrowing costs ~6.25% a year on the overnight debit balance [measured], so the leveraged part of the book must beat 6.25%, not zero.

DONE WHEN:
  - [ ] prerequisite (3) is answered: whether any de-levering ladder rung has ever been exercised, real or rehearsed. (1) and (2) are already answered — paper trading did NOT charge the interest [measured 2026-09-18, one overnight debit of -$915.83, zero `INT` activity rows; one night, not proof], and the ladder's equity series is INCOMPLETE.
  - [ ] the ladder's lost `daily_pnl.total_value` rows are repaired or the loss is recorded as permanent, because the ladder today measures roughly -1.3% peak-to-trough where the real figure is about -2.7% [measured 2026-09-18 against the 2026-08-28 backup]
  - [ ] the portfolio manager is actually SHOWN the account's borrowing capacity — the item's own separate defect, and a precondition for the question meaning anything
  - [ ] a decision is recorded, by the orchestrator after an adversary run (the 2026-09-18 ruling already moved this off the owner), on whether the PM may PLAN against borrowed money, stating the ~6.25%/yr hurdle the leveraged part of the book must beat
detail: docs/BOARD_NOTES.md (item 95)

**99. Analyst-seat prompt audit and enforcement gap — TIER 2, filed 2026-09-18; not yet placed in the owner's priority order. Detail: `docs/BOARD_NOTES.md` ("item 99").** **(d) Enforcement: build the check at the DELETION site** — when a mechanism is removed, grep its symbol name across every prompt and every Python-assembled agent string.

DONE WHEN:
  - [ ] (a) the ~55 numbers that exist only as prompt prose and the ~20 unsourced market-structure claims are each sourced, rendered from the code value, or deleted
  - [ ] (b) the technical seat's prompt names the five data blocks it actually receives and does not claim ones it does not
  - [ ] (c) the dead-weight prose — roughly a third of the PM's sheet, a quarter of the risk manager's and a quarter of the position reviewer's — is stripped, with load-bearing recitation kept (the reviewer's trigger vocabulary is the named case: a seat that does not know the words drops every exit silently)
  - [ ] (d) the deletion-site check exists: removing a mechanism greps its symbol name across every prompt and every Python-assembled agent string at that moment
  - [ ] (f) "2+ oversized → cut every BUY 25%" stops being unenforced prose — enforced, sourced or deleted, same class as (a)
  - [ ] (g) every prompt sentence stating a code- or config-controlled fact is either rendered from that value or pinned by a drift test, per item 168's pattern — the deletion-site grep alone catches neither item 98 nor item 168, which were value drift
  - [ ] no blanket prompt-text number scanner is built (rejected: ~1,825 numbers in the prompt files, mostly dates and list numbering)
  - [ ] the mandate/horizon half is NOT re-opened — resolved 2026-09-25 as SWING, days to weeks
detail: docs/BOARD_NOTES.md (item 99)

**170. `congresswatch.us` rows have no filing date, so the desk estimates one at trade+45d — filed 2026-09-20 from item 126's review.** That estimate feeds `SmartMoneyFinding`'s `lag_days <= 45` test, which then cannot fail on an estimated row by construction. **Measured:** real-date rows show median 60-day lag [2026-09-19]; estimated rows read as best-behaved instead.

DONE WHEN:
  - [x] a cluster with no real filing date can FAIL the lateness test (test fails today) — RESOLVED 2026-09-24 (#657, already on main): `SmartMoneyFinding.deterministic_eligibility`'s congressional branch now requires `not disclosure_date_estimated` alongside `lag_days <= 45`, so an all-estimated cluster (lag==45 by construction) fails; a same-lag REAL cluster still passes. `tests/test_congressional_trading.py::test_estimated_disclosure_date_cannot_satisfy_the_freshness_gate`.
  - [x] no new unsourced constant; `config/number_ledger.yaml` records the choice — no threshold changed, only which observations may satisfy the existing sourced 45-day ceiling; ledger entry for `congress_assumed_max_disclosure_lag_days` unchanged and still accurate.
  - [x] estimated-vs-real is visible wherever lag reaches a seat or owner — RESOLVED 2026-09-25: `#657` carried the flag onto `SmartMoneyObservation` and the eligibility gate, but the seat-facing summary (`SmartMoneyAnalystAgent._compact_symbol`) still dropped it; now `disclosure_date_estimated_count` is reported alongside `lag_days_range`, and each `representative_transactions` row carries its own `disclosure_date_estimated` flag. `tests/test_smart_money.py::test_compact_symbol_surfaces_estimated_disclosure_dates_to_the_seat`.
detail: docs/BOARD_NOTES.md (item 170)

**173. Residue of the ledger share-count fix — filed 2026-09-23.** Defect and fix: `docs/INCIDENT_HISTORY.md`. **(a) HAS A DEADLINE.** The bad count hid a real gap: EQNR left the book 2026-09-21 16:19-16:45 UTC with no trades row for 8.5962 sh [measured, production DB, read-only, 2026-09-23].

DONE WHEN:
  - [ ] EQNR resolved on its own evidence before the lookback expires, or the alert bounded
  - [x] (c) DONE 2026-09-25 — `get_symbols_with_open_ledger_qty` signs by position side: a COVER-family action and a filled buy-to-cover TRAIL_STOP now RETIRE a short (a 36-short fully covered reads 0, not -72); long-side signs unchanged. Both routes fixed together; the pinning known-defect test was deleted per its own instruction; new tests in `tests/test_stop_out_reconciliation.py`. Silent today (caller is LONG-only), correct for when shorts are enabled.
  - [x] (b) VERIFIED 2026-09-25 — no change needed. All four call sites checked on origin/main: intraday + evening already reconcile fills first (#697); morning and the midday/close review keep the old order intentionally (their `_reconcile_fills` runs later over this session's own rows only, so no stale 'submitted' SELL raises a false page). No remaining old-order site produces a false CRITICAL, so #697 was not touched.
detail: docs/BOARD_NOTES.md (item 173)

**112. A de-lever that leaves the book over its ceiling now writes a durable record — TIER 1, filed 2026-09-18 out of the item 87 audit, RECORD SHIPPED 2026-09-19; the alert decision stays open.** `_enforce_gross_ceiling` used to only log a warning; a run-scoped `specialist_evidence` row (`stage='gross_delever'`, `outcome='still_over_ceiling'`) is now written whenever a de-lever finishes over ceiling, carrying gross/equity before and after and each order's outcome.

DONE WHEN:
  - [ ] a de-lever pass that finishes with the book still over its gross ceiling reaches the owner in its OWN Telegram message, severity carried in text. Not an owner decision: his standing alert-design rule (2026-09-02, recorded at the top of this file) already says every failure alerts in its own message, and a de-lever that fails to get the book under its limit is a failure. The `specialist_evidence` row (`stage='gross_delever'`, `outcome='still_over_ceiling'`) already exists, so only the delivery half is left.
  - [ ] no further automatic selling is added in the same pass — changing what a failed de-lever DOES is a ladder change and needs its own item and its own adversary run
detail: docs/BOARD_NOTES.md (item 112)

**107. Prompt drift the new check cannot see, and prompt-only numbers. Filed 2026-09-17.** Reasoning and what was ruled out: `docs/INCIDENT_HISTORY.md`, 2026-09-17.

DONE WHEN:
  - [ ] (a) prompt-described behaviours are registered so a behaviour that CHANGES, not only one deleted, gets scanned — nothing is registered today, so the shipped check is blind to the whole class
  - [ ] (b) the PM's prompt-only sizing arithmetic (bases 3.0/1.75/0.75, the +0.25 reward:risk bonus, the ±0.20/±0.10 evening tilt, the 0.5 stale halving at age ≥8d) and the technical seat's "3+ aligned signals", 1-3/4-7/8+ freshness tiers and forward-PE 40/60 + P/S 15/25 levels are each sourced, derived or deleted
  - [ ] the reviewer's `weight_pct > 12%` escalation and the `DRIFT` flag's matching 12 stop being bare inline literals with three homes — named or moved to settings, so the rendering mechanism can reach them at all
  - [ ] (c) every prompt number that has a settings key is rendered by `prompt_limits.py`, which covers 2 of 10 prompt files today; anything with no settings key falls to (b)
  - [ ] SHARED CRITERIA with item 99: (b) here is 99(a), and (c) here is 99(g). They are one requirement seen from two audits — tick them together rather than doing the work twice.
detail: docs/BOARD_NOTES.md (item 107)

**109. One mandate question and the dead-weight prose the prompt-truth pass surfaced. Filed 2026-09-17. Prompts corrected; NO gate touched.** Part (b) was removed as fixed (PR #489, verified on main 2026-09-18).

DONE WHEN:
  - [ ] (a) a decision is recorded on whether macro counts as a per-name seat in the agreement gate, then ONE of `count_aligned_sources` and the PM's sheet is changed to match the other. Not the owner's: the 2026-09-18 delegation ruling recorded at the top of this file moved this exact item to the orchestrator-after-an-adversary-run, and the item is already listed there.
  - [ ] whichever side wins is justified from the PM prompt's own provenance rule, NOT from `docs/OUTCOME.md`, which says nothing on this beyond a cash-deployment line — that miscitation has already been made twice
  - [ ] (c) the dead-weight recitation (~35% of the PM's sheet, ~26% of the risk manager's, ~24% of the reviewer's) is deleted, with load-bearing recitation kept — SHARED with item 99(c), which is the same prose; do not strip it twice
  - [ ] nobody settles (a) by editing the gate first: changing `count_aligned_sources` moves trades
detail: docs/BOARD_NOTES.md (item 109)

**114. The revisable take-profit refuses rather than re-derives after a big run, by design — filed 2026-09-18 with the change that created it.** `src/risk/target_revision.py` holds the ENTRY PRICE and the PINNED HORIZON fixed across a re-derivation, so `horizon_reach` is still measured from entry over the whole original horizon.

DONE WHEN:
  - [ ] how often each refusal actually fires is measured against the desk's own recorded target revisions — `REFUSAL_NO_STRUCTURE_LEFT_IN_DIRECTION` and `REFUSAL_DERIVED_TARGET_BEHIND_PRICE` are both believed uncommon and neither has been counted
  - [ ] on that measurement, either the reach is re-anchored on the current close over the REMAINING horizon, or a recorded decision that the two refusals stand as they are. The blocker named in this item is gone: the evening-vs-technical horizon contradiction was resolved SWING on 2026-09-25 and item 97 was retired the same day, so this no longer waits on a horizon question.
  - [ ] the target is never re-anchored on the current price alone — that would make the target a function of the price move, the one thing a revision must not be legitimised by
detail: docs/BOARD_NOTES.md (item 114)

**119. The economics feed can leave required series un-attempted at the open, and the re-derived fix is only measured mid-morning — OPEN, filed 2026-09-18.** Re-filed out of PR #435 (closed unmerged).

DONE WHEN:
  - [ ] every required series demonstrably gets a real attempt inside the existing ceiling, proven against open-like conditions rather than a healthy mid-morning batch
  - [ ] a series still missing after the bounded re-ask is named, the economist is not paid on the holes, and a later repair pays only once the set is complete
detail: docs/BOARD_NOTES.md (item 119)

**125. The desk's sentiment verdicts are unvalidated and nothing measures them — filed 2026-09-18. A finance word list was investigated and REJECTED; do not re-propose it.** Sentiment is an LLM-emitted enum constrained by a hand-authored 4-axis table (`config/prompts/earnings_analyst.md`) whose own rule is that the verdict "must be derivable from these 5 fields — show the arithmetic".

DONE WHEN:
  - [x] some check exists that could show a sentiment verdict was wrong, without replacing the five-field derivation — `src/sentiment_measure.py` scores each stored verdict by the sign of the realized forward move (bullish wrong iff price fell, bearish wrong iff it rose); no band, no tuned constant, and the five-field derivation is untouched
  - [x] that check is fed by real sessions the desk has already paid for, not by a new benchmark or test environment — it reads the on-disk earnings verdicts the desk already writes and the daily bars it already fetches; `scripts/measure_sentiment_verdicts.py` runs it over the real store
detail: docs/BOARD_NOTES.md (item 125)

**127. Two of the desk's own processes can run the same broker-mutating repair at once, and the whole of `intra_check`'s preamble writes to the broker before any lock — TIER 1, filed 2026-09-18. Two findings of one investigation, filed as ONE item because one lock closes both; do not re-file the class separately. Detail: `docs/BOARD_NOTES.md` ("item 127").** **The disease:** every write in `intra_check`'s preamble runs before any lock, and only the paid opportunity scan is lock-protected; the same exposure is open on other shared broker writes. **The worst pairing is not two repairs racing — it is a repair ADDING a stop in the window where a session has deliberately CANCELLED stops in order to sell.** **Decided shape: ONE lock around the whole repair pass, not per-symbol** — the measured evidence contains nothing arguing for per-symbol granularity.

DONE WHEN:
  - [ ] every broker-mutating write in `intra_check`'s preamble runs inside the same process lock the paid scan already holds
  - [ ] a repair cannot add a stop inside a session's deliberate cancel-stops-then-sell window, proven against that pairing and not only against two repairs racing

**138. Five unsourced order-price buffer sites carrying three values — filed 2026-09-18, TIER 1.** A 1% ladder offset, a 0.5% midday offset and a 3% stop-limit buffer decide whether an order fills, and none of the five sites is in `config/number_ledger.yaml` (they sit in the broker/execution path item 130 shows the ledger's scope rule excludes). **Item 118 is a NEAR-NEIGHBOUR and does NOT cover this** — it asks whether the ladder's 1% limit fills on a gap day; this is the whole family of unsourced price buffers.

DONE WHEN:
  - [x] all five sites carry a ledger entry with a source, or the open question and what the desk pays meanwhile, with no value changed in the same pass

**139. Roughly 74 of the repo's 90 registered git worktrees are session scratch under `/tmp` — filed 2026-09-18, housekeeping, pre-existing, nobody's current task.** CORRECTION to the filing brief: none of them is stale in git's sense — every registered path still exists, so `git worktree prune` removes nothing [verified 2026-09-18].

DONE WHEN:
  - [x] finished sessions remove their own worktree, or a swept-on-a-schedule rule exists and is recorded
detail: docs/BOARD_NOTES.md (item 139)

**143. `docs/RESEARCH_FINDINGS.md` has ZERO entries for ATR bands, trailing, profit-taking, pacing or ranking granularity — filed 2026-09-18.** The only exit-side measurement it carries is the level-touch/stop bar in section 7.

DONE WHEN:
  - [ ] each of the five areas has either a research entry or a recorded statement that no published source was found, with what was searched
detail: docs/BOARD_NOTES.md (item 143)

**147. A model call that succeeds with NO usage data is charged the FULL reservation and marks the day inexact — filed 2026-09-18.** `src/cost_circuit.py` stamps the day inexact whenever it charges the conservative reserve; three live null-cost rows exist on 2026-08-31.

DONE WHEN:
  - [ ] a success with no usage is reconciled against the provider's own billing or charged at a measured rate, rather than at the reservation
detail: docs/BOARD_NOTES.md (item 147)

**148. Two level-ranking numbers are invisible to the ledger, and the correlation window changed with no note — filed 2026-09-18.** The level strength that decides which six levels the analyst ever sees is `len(cluster) / (1.0 + distance_pct / 10.0)` in `src/data/levels.py`, and the 40% maximum distance beside it: both are INLINE literals, so item 90's scanner — which reads module-level constants and config defaults — cannot see either, an instance of item 130's scope hole on the data side.

DONE WHEN:
  - [x] both inline literals carry a ledger entry or are moved to a scanned definition site, with no value changed in the same pass — DONE on main before this pass: the `/ 10.0` divisor is now the named `LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT` (ledgered `arbitrary`, PR #684), and the flat 40% max-distance cap no longer exists — replaced 2026-09-12 by the ATR `horizon_reach` window (ledgered via `MAX_REACH_ATR_MULTIPLE`/`MAX_HORIZON_SESSIONS`). No value changed.
  - [x] the correlation window's current value has a recorded reason, or is named as arbitrary like the threshold beside it — the correlation window has no definition site the number-ledger can attach a row to (it rides `trading.lookback_days`, which carries no numeric default), so it took the "recorded reason" branch: the reason is written at `_ensure_correlation_matrix` and cross-referenced beside `CLUSTER_CORRELATION_THRESHOLD`. Honest finding: the 5y window is INHERITED from the structural-level fetch (settings.yaml records the 320→1800 raise as "purely for structure"), not justified for correlation clustering; clustering needs only 20 overlapping returns. No value or behaviour changed.
detail: docs/BOARD_NOTES.md (item 148)

**152. A research seat's answer coming back unreadable has no board item — filed 2026-09-18, from the log-health report; the technical-seat half is SETTLED by #538 (2026-09-19), news seat still open.** A parse failure means the call was paid for and thrown away with nothing to show for it; measured on the retained logs: 11 on the news seat, 79 on the technical seat [measured 2026-09-18 against `quant_agent.log` and its five rotations].

DONE WHEN:
  - [x] the news-seat parse-failure rate is understood and either brought down or shown to already recover cleanly on retry — shipped by #695 (2026-09-25): the whole-answer non-JSON path now gets the same one paid heal retry the schema path had, the retry flag no longer leaks across the long-lived instance (which had silently disabled the retry for every later failure in the run), and any final exhausted failure persists its raw payload and logs in log_health's `seat_answer_unreadable` family instead of being paid-and-discarded
detail: docs/BOARD_NOTES.md (item 152)

**154. A research seat being unreachable, with the work going ahead short-handed, has no board item — filed 2026-09-18, from the log-health report.** `Morning research degraded` fired 14 times across the retained logs.

DONE WHEN:
  - [x] a decision made short-handed this way is marked as such wherever the desk records it, or the missing seat is shown not to change the decision
detail: docs/BOARD_NOTES.md (item 154)

**157. The technical seat has no enforced answer format on either route, so a malformed row still needs salvaging after the fact — filed 2026-09-19, from #538's write-up.** #538 made a broken row recoverable, not prevented.

DONE WHEN:
  - [ ] a live call confirms whether the Google route enforces a sent response schema
  - [ ] a decision is recorded on whether the schema change is worth it given row-salvage already ships
detail: docs/BOARD_NOTES.md (item 157)

**163. The PM's narrative and its own emitted number disagree with nothing checking it — filed and verified 2026-09-19 against the stored reasoning and target rows.** Run `601011e0` (09-16): `sizing_logic` prose says "RSG and AAPL get 2.5% risk each," but RSG's own emitted `risk_allocation_pct` is 0.5.

DONE WHEN:
  - [x] a check flags a mismatch between the PM's reasoning and its own emitted number — a standalone validator (`src/risk_narrative_check.py`, wired once in `DecisionStage`) reads the stored PM decision and flags a symbol whose `sizing_logic` prose names an explicit risk % that materially differs from that symbol's emitted `risk_allocation_pct`, recording each mismatch to the pipeline evidence stream as `sizing_narrative_check / mismatch`. DETECTION ONLY: it never changes a target, size, price or exit; `risk_allocation_pct` stays authoritative. It reuses the tolerance and the narrow risk-% matcher the existing per-symbol `TargetPosition.thesis` check uses, and stays silent on any prose it cannot pair to a symbol with confidence (no false positives). NOTE: the pre-existing `TargetPosition._flag_risk_narrative_mismatch` checked each position's own `thesis`, NOT the whole-book `sizing_logic` this item was filed against — so this closes the actual filed surface.
  - [ ] the RSG case is re-examined to see which value the seat meant — STILL OPEN: this needs the stored run `601011e0` (09-16) rows read back to judge whether the seat meant 2.5% or 0.5% for RSG; the detector above surfaces the disagreement but does not decide which side was right.

**165. Item 91's calendar-days bug has siblings the fix didn't touch — filed 2026-09-20, adversary pass on item 91's retirement.** PR #493 correctly fixed the position reviewer's own `too_early`/`time_fraction` pace calculation to read session-based `sessions_held` instead of calendar-day `days_held` — that specific mechanism is genuinely closed and item 91 is retired for it.

DONE WHEN:
  - [x] the evening reviewer's holding-time figure is either converted to sessions or explicitly labeled as calendar days so the model isn't silently judging session-scale progress off a calendar-day number — the evening thesis-health snapshot now emits `sessions_held` alongside `days_held`, computed with the shared `broker.trading_sessions_held` helper, matching the field the reviewer prompt reads for pace
  - [x] the `1/3` pace floor is REMOVED per owner ruling 2026-09-25 — not re-derived: the owner ruled the "one-third of the pinned horizon" wait a made-up clock stacked on a guessed horizon, so there is no elapsed-time floor before pace is judged. The `sessions_held < max(1, pinned_horizon / 3)` gate and its `too_early`/`not-yet-measurable` narrative are deleted from the morning/midday facts path and the reviewer prompt; pace is now computed and surfaced from the first review whenever a horizon was pinned, read against current price structure each review (an early ratio is context, never on its own a reason to exit). The `/3` divisor was an inline literal, invisible to the number scanner and not ledgered, so no number-ledger change.
  - [x] session counts use the real market-holiday calendar already available in the codebase instead of a bare Mon-Fri assumption — reuses `broker.trading_sessions_held` (Alpaca's real calendar; falls back to the weekday counter only on a calendar-query failure)
detail: docs/BOARD_NOTES.md (item 165)

**174. Nobody is told when the cost circuit lets itself back in — filed 2026-09-23 with the 503/self-clear fix (write-up in `docs/INCIDENT_HISTORY.md`).** A hard latch alerts Telegram; the new transient self-clear writes an `auto_reset` event and a log line only, so the owner sees "desk suspended" and never sees it come back.

DONE WHEN:
  - [x] a self-clear reaches the owner on the same surface the suspension did — the auto-expiry now sends the same Telegram alert the suspension does (🟢 RESUMED, naming the forgiven trigger, when it cleared and why, every number read from the `auto_reset` event row), keeping the DB event and log; durable/retryable like the quota-recovery alert, and suppressed under `QAMC_REHEARSAL=1` at the notifier chokepoint
  - [x] the resume is PAIRED to a suspension the owner actually received — 2026-09-26: the auto-clear captures the suspension's `alert_state` at the last instant it is knowable (the same write wipes it), and a resume for a suspension that never reached him is resolved as unpaired instead of sent, so an outage that ends before he hears about it is zero messages, not a dangling "back live"
  - [x] reproducible offline — the fault harness gained a `server_error_mid_stream` kind (a pre-generation 503 is provably free and can never latch; only a mid-stream one can), and a morning rehearsal with it reproduced the latch, the suspension alert, the auto-expiry and the paired resume alert end to end
  - [ ] cooldown and allowance re-read against a real occurrence — STILL OPEN: production has had ZERO real `auto_reset` events, so the 15-min cooldown and the 19/day allowance remain unmeasured; a rehearsal cannot measure them because it sets the cooldown itself
detail: docs/BOARD_NOTES.md (item 174)

**177. Paid intraday tick: trigger, cadence and held-book context are ONE decision, filed 2026-09-23. Item 90 half two tranche one; do not re-file the pieces.** The trigger decides whether a tick is paid, the cadence how many, the held book what a paid one costs [measured 09-21/22; `docs/INCIDENT_HISTORY.md`].

DONE WHEN:
  - [ ] all 3 leave `status: arbitrary`, `MAX_ARBITRARY_ENTRIES` falls by 3
  - [ ] the cadence ledgered and test-covered
  - [ ] every intra-preamble job on its own schedule
  - [ ] spend and actions re-measured
detail: docs/BOARD_NOTES.md (item 177)

**179. The macro seat's paid heal hands on a plain dict where the desk expects the model object, and stores nothing — filed 2026-09-23.** The store-write half is now FIXED (2026-09-25): a paid macro heal is persisted to the macro store like the scheduled read. The "plain dict / zero nominations" half was investigated and is NOT a defect (dict is the canonical shape; nominations are collected before the heal runs). Left OPEN only for the `mechanical_heal_macro` dead-code OWNER call. See `docs/BOARD_NOTES.md` (item 179).

DONE WHEN:
  - [x] the dict path is made explicit and tested — the "plain dict / zero nominations" finding does NOT cause harm: `ctx.macro_analysis` is CANONICALLY a dict (its own type comment, and PM reads it via `.get()`), and macro nominations are collected once, inside `MorningResearchStage`, which finishes BEFORE the heal ever runs, so the healed macro's shape cannot change any nomination outcome; the sequencing limitation (a healed macro's nominations are never collected at all, because collection precedes the heal) is a deeper, separate question, not a shape bug
  - [x] the store write now exists — a successful paid macro heal is persisted to the macro store the same way the scheduled morning read is (KEEP WHAT COSTS MONEY), with a reproduction test that fails pre-fix
  - [x] `mechanical_heal_macro` (test-only dead code) either wired or removed — REMOVED 2026-09-25: verified no `src/` caller (its coercion helper `coerce_macro_shape` is already wired into every live macro consumption point, and the live heal path uses paid retries, not this unpaid HealResult wrapper — there was no intended fallback to wire it into), so the function and its four test-only cases were deleted; the still-live helpers `coerce_macro_shape`/`coerce_sector_guidance`/`describe_macro_parse_failure` and their tests are kept
detail: docs/BOARD_NOTES.md (item 179)

**180. The young-listing refusal fires on a calendar count, not on what the trade needs — filed 2026-09-23. Detail: `docs/BOARD_NOTES.md` ("item 180").** `LONGEST_INDICATOR_WINDOW` was both the MA200 window and a constructor refusal under 200 bars. **RESOLVED 2026-09-25 (owner ruling):** the constructor's bar-count young-listing refusal (`_require_sufficient_history`) was DROPPED. Indicators already degrade to available history (`compute_indicators` leaves `ma_200` = None under 200 sessions); a young listing is now judged on whether a defensible protective stop is readable, and a name too young to read ANY stop from is refused by the EXISTING stop-readability rule (`_derive_structural_stop_no_atr` / `no_structural_stop_and_no_volatility_reading`, item 80). 200 was NOT lowered to another number (owner ruled that out). `LONGEST_INDICATOR_WINDOW` re-sourced `arbitrary`→`sourced`, `MAX_ARBITRARY_ENTRIES` 141→140.

DONE WHEN:
  - [x] refuses on the missing input the plan needs (a readable stop), not on a bar count
  - [x] an MA200 thesis or exit still refuses (`exit_guard` returns UNPARSEABLE when `ma_200` is None — verified already present on main)
detail: docs/BOARD_NOTES.md (item 180)

**182. The de-levering ladder's rungs and cash-deficit cushion are made-up money numbers with no board item — filed 2026-09-25, TIER 1.** Item 90's half two (read each arbitrary number off its instrument), surfaced as its own board item so it stops hiding in `config/number_ledger.yaml` (owner: "nothing hides"). The gross de-lever ladder `GROSS_LADDER` (`src/risk/rules.py`) fires at round drawdowns and grants round leverage multiples — `-8% → 1.5x`, `-15% → 1.0x`, `-20% → 0.5x` — and the forced cash-deficit de-lever sells the T-bill vehicle at a flat 2% cushion (`_force_delever`, `deficit x 1.02`). All seven are `status: arbitrary`: none is read off the account or a cited source, and each decides how hard the book is cut in a drawdown. Item 118 fixed only the trim's ORDER TYPE and item 112 only the over-ceiling record — neither sources these rung values. **2026-09-25 (owner delegated to the adversary):** the six `GROSS_LADDER` numbers (−8/−15/−20 rungs and 1.5/1.0/0.5 multiples) were RATIFIED as owner-appetite — the deliberate never-liquidate loss defense that only trims, floors at 0.5×, and honours item 32; values unchanged, kept `status: arbitrary`+note per ledger convention. Two reformulations (Grossman-Zhou hard-zero, gap-survival re-derivation) were adversary-REJECTED. Item STAYS OPEN: `GROSS_LADDER_ALERT_PCT` and the forced cash-deficit 2% cushion are not yet resolved.

DONE WHEN:
  - [ ] each constant is sourced, measured, owner-ratified as appetite, or reformulated away

**183. Five order-placement gates are made-up money numbers with no board item — filed 2026-09-25, TIER 1.** Item 90's half two, surfaced for visibility. Whether an order fills, is skipped, or trades at all is decided by flat unsourced constants: the 40bps entry-slippage belt (`ExecutionConfig.max_entry_slippage_bps`), the $500 constructor minimum-order floor (`ConstructorConfig.min_order_usd`), the 0.5% minimum weight change before the desk bothers to trade (`ConstructorConfig.min_trade_weight_delta`), the entry-skip when the ask sits more than 2% above the slippage cap (`ExecutionStage._run_session`), and the 1% cash-reserve band (`CashSweepConfig.reserve_pct` — still live via the deployment-gap advisory even though the sweep itself is retired). All `status: arbitrary`, none read off a spread or a measurement. Distinct from item 138, which tracks the order-PRICE buffers (the 1% / 0.5% / 3% offsets), not these gates.

DONE WHEN:
  - [ ] each constant is sourced, measured, owner-ratified as appetite, or reformulated away
  - [ ] DEAD CONFIG, remove rather than source: the T-bill cash-sweep was retired 2026-09-17 (`cash_sweep.enabled: false`), so its still-`arbitrary` constants `_BUY_LIMIT_PAD` 1.001, `_SELL_LIMIT_PAD` 0.999, `_FUND_BUFFER_FRAC` 0.01, `_FUND_BUFFER_MIN_USD` 50 and `CashSweepConfig.min_order_usd` 500 are unreachable while the sweep is off and should be deleted, not re-derived

**185. Trailing-stop numbers are made-up money numbers with no board item — filed 2026-09-25.** Item 90's half two, surfaced for visibility. How much of a run-up the desk gives back is a flat 3x-ATR chandelier (`trailing.CHANDELIER_ATR_MULTIPLE`, called "the conventional setting" with no citation), a proposed stop must ratchet at least 2% above the live stop (`trailing.MIN_RATCHET_PCT`), and midday refuses a trail whose new stop sits below 50% of the current price as a likely model typo (`_midday_execute_llm_actions`). All `status: arbitrary`. The trailing pivot window is already tracked by item 55 and the range ratchets by item 142, so they are excluded here.

DONE WHEN:
  - [ ] each constant is sourced, measured, owner-ratified as appetite, or reformulated away

**186. Portfolio and cluster risk ceilings are made-up money numbers with no board item — filed 2026-09-25.** Item 90's half two, surfaced for visibility. What caps deployment and crowding is flat and unsourced: the 25% total at-risk portfolio ceiling (`RiskConfig.max_portfolio_risk_pct`), the 90% terminal sector-ceiling bound (`RiskConfig.SECTOR_HARD_CEILING_MAX`, whose definition site says it is "open for the owner to move"), the 40% share of total risk one correlation cluster may hold (`RiskConfig.max_cluster_risk_share_pct`), the 0.7 correlation cutoff that defines what counts as one cluster (`correlation.CLUSTER_CORRELATION_THRESHOLD`), the 1.5x short-side sizing haircut (`RiskConfig.short_gap_risk_multiple`), and the 5% resulting-weight cap on a BUY whose earnings filing is queued but unanalysed (`_clamp_queued_earnings_buys`). All `status: arbitrary`. The already owner-ratified ceilings (per-trade 5%, gross 2.0x, single-name 65% notional, sector soft/hard 75 / 90 on the constructor) are excluded — they are accepted appetite, not open debt. **2026-09-25 (owner delegated to the adversary):** `max_portfolio_risk_pct` (25), `SECTOR_HARD_CEILING_MAX` (90) and `max_cluster_risk_share_pct` (40) RATIFIED as owner-appetite (values unchanged, kept `status: arbitrary`+note). Item STAYS OPEN: `CLUSTER_CORRELATION_THRESHOLD` (0.7), `short_gap_risk_multiple` (1.5) and the queued-earnings BUY clamp (5%) are not yet resolved.

DONE WHEN:
  - [ ] each constant is sourced, measured, owner-ratified as appetite, or reformulated away

**187. FRED fetch reliability — the chronic `fetch_deadline_exceeded` failure and required series left un-fetched — filed 2026-09-25, carried out of item 175's retirement. Item 175's weekend/holiday overdue-date roll shipped and was retired; this is the separate, still-open half. Detail: `docs/BOARD_NOTES.md` ("item 187").** Every FRED failure in the retained log is `fetch_deadline_exceeded`; 4 of 12 runs reached full coverage, worst 5 of 15 [measured 09-17..23]. Owned by the approved fetch redesign.

DONE WHEN:
  - [ ] the `fetch_deadline_exceeded` rate is understood and either brought down or shown to recover cleanly inside the existing time ceiling, measured against real runs rather than a healthy mid-morning batch
detail: docs/BOARD_NOTES.md (item 187)

**Retired item numbers — never reuse.** 158, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 57, 58, 59, 60, 61, 62, 66, 68, 69, 71, 72, 73, 79, 80, 81, 82, 83, 84, 85, 87, 88, 89, 91, 92, 93, 94, 96, 97, 98, 100, 101, 102, 103, 104, 105, 106, 108, 110, 111, 113, 116, 117, 118, 120, 121, 122, 124, 126, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 141, 142, 144, 145, 146, 149, 151, 153, 156, 159, 160, 161, 162, 164, 166, 167, 168, 169, 171, 172, 175, 176, 178, 181, 184, 115, 140, 150, 123, 155 in this queue, and 1, 2, 3, 4, 5, 6, 7, 8 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md`. **This line takes the NUMBER ONLY — never a reason.** Every retirement's reason lives in `docs/INCIDENT_HISTORY.md`, which is append-only and merges entry-by-entry; re-narrating it here made this one line the thing every parallel retirement collided on, and the merge driver refuses prose it cannot reconcile (`scripts/resolve_doc_conflict.py::merge_text`), so two closures in flight at once could not both land. The numbers themselves merge as a union and never conflict. `tests/test_status_board.py` fails a change that adds a reason sentence here. The per-item reasons this line used to carry were moved to `docs/INCIDENT_HISTORY.md` on 2026-09-26, verbatim, losing nothing. Gate item 7 was moved, not closed: it is item 76. The two numbering schemes are separate — 3 is retired in BOTH, 20 is live here, and 40, 67 and 200 never existed [verified 2026-09-18 against this file's full git history]. The one piece of the items-135/136/137 PR NOT finished — the short-side guard living one layer out from where it belongs — was item 155, retired 2026-09-26. Residue of items 100 and 103 lives in items 106 and 115; item 89 was SHRUNK, not retired. The §11.2 ladder stays. The ladder's own unmeasurable-drawdown behaviour is a separate live question. **Next free number is 182 (165-171 filed above/below, 172 filed and closed in one change, 173-177 allocated, 178 the execution SELL loop's split staleness posture, 179 the macro heal's shape-and-storage gap, 180 young-listing, 181 retired 2026-09-24 — the short-side risk-budget over-size) — and note that this sentence has been stale more than once**, because a number is claimed on a branch before it reaches this file. Check the open branches, not just this line.

## Evidence-only follow-ups — reopen only on concrete production evidence

- news-narrative factual drift; `actual_provider` attribution oddity.
