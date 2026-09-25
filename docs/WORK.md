# QAMC Current Work

## Active finish line

### Session start — read this first

**STANDING PRINCIPLE — NO ARBITRARY NUMBERS, EVER.** Every trade constant comes from real data, a cited source, or the instrument itself — never a flat count, round % or a number that "sounds prudent". **Approval does not make a flat number non-arbitrary.** Mark unmeasured numbers provisional, never settled.

**STANDING PRINCIPLE — MISSING DATA IS A DEFECT IN THE PRODUCING STEP (owner 2026-09-17).** Find why a required field is blank and fix that step so it actually produces the data. Never invent. Never make skip/drop/ignore-and-continue the product. A drop-the-name quarantine is temporary. Item 78 is the current instance; the rule is not limited to it. Fuller statement: `docs/OUTCOME.md`.

**This file holds only open work.** Finished work is written up in `docs/INCIDENT_HISTORY.md` (append-only, each entry opening with one plain-language line) and then deleted here, together with its `## item N` block in `docs/BOARD_NOTES.md` and its number added to the retired line. `tests/test_status_board.py` fails if this file passes 100,000 bytes, if one change grows it by more than the shrinking growth budget its current fullness allows (owner ruling 2026-09-17: recording a genuine new defect must not be blocked just because nothing is finished yet to prune — see `work_md_growth_budget` in `scripts/status_board.py`), if a self-declared-finished item is left sitting on the board, or if it loses an item number without retiring it. Ratified architecture decisions go in `docs/QAMC_REMEDIATION_SPEC.md` as a numbered phase, not here.

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
detail: docs/BOARD_NOTES.md (item 17)

**18. 70% of the PM's prompt was earnings-filing prose — MEASURED 2026-09-02, PARTIALLY FIXED, core cause MERGED 2026-09-04 (PR #252), and these three follow-ons are all that is left. Closed detail: `docs/INCIDENT_HISTORY.md` (items 18a-18e).** (a) The BUY-eligibility section reorder — BLOCKED: it needs a paid benchmark the owner has forbidden unless he asks.
detail: docs/BOARD_NOTES.md (item 18)

**19. The model's consistency is an ASSET — three uses. Do not start these before item 18.** Measured: 5 blinded runs, two arms, quality identical to four decimal places.
detail: docs/BOARD_NOTES.md (item 19)

**20. GATE THE DECISION ON EVIDENCE COVERAGE — owner's design, 2026-09-02; the COUNTING half is all that is left, and it is his, not an agent's. Do not trade on partial evidence. Detail: `docs/BOARD_NOTES.md` ("item 20").** His ruling is that a decision on incomplete evidence is fabricated, not degraded.
detail: docs/BOARD_NOTES.md (item 20)

**55. What IS a structural level — how many bars make a swing point, and how wide is a level's zone? OPEN, filed 2026-09-13.** Touch count is settled and pinned by a test: two touches, sourced (Tsinaslanidis 2012) — do not tighten it.
detail: docs/BOARD_NOTES.md (item 55)

**56. Is a stop too wide, and read off what? The THRESHOLD is still unread. OPEN, narrowed 2026-09-14.** At what probability of being touched inside the trade's own horizon does a stop stop being a stop?
detail: docs/BOARD_NOTES.md (item 56)

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN (structure shipped, magnitude calibration still open), carried out of item 52.** One scalar in [0,1] is both the ranking key and the dollar multiplier, with no direction. **STRUCTURE FIX 2026-09-25:** a derived `SmartMoneyObservation.signal_direction` channel now carries the sign (buy +1, sale/exchange/unknown 0), and both deterministic ranking keys in the smart-money analyst multiply value*weight by it, so a contra/bearish sale can no longer rank or size as a bullish buy of equal magnitude; buys keep their exact former contribution. The desk is long-only on smart-money admission (admission requires direction=="buy"), so a sale is safely neutralised, never counted as bullish. **open_question (owner-appetite/research):** the magnitude→sign boundary that would let a large sale (Scott & Xu's sourced >50%-of-holdings band, already on the row as `holdings_fraction_band`) score bearish (-1) versus a small sale's mild-positive — no published SIGNED scoring scheme exists; ruled out pending a source or enough own outcome data. Do NOT pick that number.
detail: docs/BOARD_NOTES.md (item 63)

**64. The backtest still rations the risk budget alphabetically when the budget binds — OPEN.** Live spends down the ranked verdicts; this engine has none, so equal asks are served by ticker spelling. **Ruled out, with reasons: `docs/INCIDENT_HISTORY.md`, 2026-09-16**, notably ranking by the engine's own reward:risk, which would silently change who gets capital.
detail: docs/BOARD_NOTES.md (item 64)

**65. Four of the five analyst seats have no strength scale of their own — OPEN, opened 2026-09-13.** Only Technical states a lean; news, macro, smart_money and earnings report no stated strength and rank on weighted conviction alone.
detail: docs/BOARD_NOTES.md (item 65)

**70. One underived `1.0` is doing two different jobs in the exit path, and neither is read off anything — OPEN, filed 2026-09-14.** The noise-band ATR multiple sets when an adverse move stops being noise and is reused as the margin in the structural-protection check; a separate absolute minimum stop multiple, also 1.0, sets how tight a stop may be.
detail: docs/BOARD_NOTES.md (item 70)

**74. One piece of news can cut the same holding twice in a day, and whether that is a fault is a design question — OPEN, filed 2026-09-14.** A midday REDUCE on a hard trigger can repeat at the close on the same trigger; the pipeline only warns, and the position-reviewer prompt explicitly allows it.
detail: docs/BOARD_NOTES.md (item 74)

**75. The desk has no automatic profit-taking: its target never reaches the broker, a trim for profit is not an allowed exit reason, and the trail sits too loose — OPEN, filed 2026-09-14 after an owner question on ORCL.**
detail: docs/BOARD_NOTES.md (item 75)

**76. PM-input shape: the one open piece is whether the PM uses its new macro-audit channel. OPEN, moved out of the PM TEST GATE 2026-09-14.** Write-up: `docs/INCIDENT_HISTORY.md`, 2026-09-13/14.
detail: docs/BOARD_NOTES.md (item 76)

**77. Model selection: the PM seat is the next open question. Pointer, 2026-09-14.** Detail: `docs/architecture/MODEL_ROUTING_POLICY.md`.
detail: docs/BOARD_NOTES.md (item 77)

**78. Delete the blank-falsifier isolate once Tech and the PM demonstrably produce a real falsifier — DEFECT (patch), instance of the missing-data standing principle.** The isolate is live and declares itself TEMPORARY: `_isolate_empty_soft_exit_entries` (`src/pipeline_stages.py:2817`) drops any constructed BUY/SHORT whose falsifier is blank.
detail: docs/BOARD_NOTES.md (item 78)

**86. The live-fill `trade_updates` websocket has never once authenticated — cause found and fix SHIPPED 2026-09-18 (#517), item stays OPEN until a live attempt proves it.** 1,017 failures across three days, zero successes [measured 2026-09-18].

DONE WHEN:
  - [ ] one live log line records `trade_updates websocket authenticated`
detail: docs/BOARD_NOTES.md (item 86)

**90. Unsourced trade-governing numbers — the GATE now exists; re-deriving the numbers does NOT. TIER 1, half shipped 2026-09-18, item stays OPEN.** **Half one, DONE:** every numeric definition site in scope must carry a `config/number_ledger.yaml` entry saying where it came from, or `pytest` fails.
detail: docs/BOARD_NOTES.md (item 90)

**95. DELEGATED TO THE ORCHESTRATOR — may the portfolio manager plan against borrowed money? Filed 2026-09-17, ownership moved 2026-09-18. Detail: `docs/BOARD_NOTES.md` ("item 95").** Borrowing costs ~6.25% a year on the overnight debit balance [measured], so the leveraged part of the book must beat 6.25%, not zero.
detail: docs/BOARD_NOTES.md (item 95)

**99. Analyst-seat prompt audit and enforcement gap — TIER 2, filed 2026-09-18; not yet placed in the owner's priority order. Detail: `docs/BOARD_NOTES.md` ("item 99").** **(d) Enforcement: build the check at the DELETION site** — when a mechanism is removed, grep its symbol name across every prompt and every Python-assembled agent string.
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

NO CRITERIA: whether a failed de-lever should also alert or act is, by this audit's own framing, a live-selling-path decision for the owner, not a self-authorised patch.
detail: docs/BOARD_NOTES.md (item 112)

**107. Prompt drift the new check cannot see, and prompt-only numbers. Filed 2026-09-17.** Reasoning and what was ruled out: `docs/INCIDENT_HISTORY.md`, 2026-09-17.
detail: docs/BOARD_NOTES.md (item 107)

**109. One mandate question and the dead-weight prose the prompt-truth pass surfaced. Filed 2026-09-17. Prompts corrected; NO gate touched.** Part (b) was removed as fixed (PR #489, verified on main 2026-09-18).
detail: docs/BOARD_NOTES.md (item 109)

**114. The revisable take-profit refuses rather than re-derives after a big run, by design — filed 2026-09-18 with the change that created it.** `src/risk/target_revision.py` holds the ENTRY PRICE and the PINNED HORIZON fixed across a re-derivation, so `horizon_reach` is still measured from entry over the whole original horizon.
detail: docs/BOARD_NOTES.md (item 114)

**115. The run-detail popup still shows raw machine evidence verbatim — filed 2026-09-18, split out of item 106 so it is not audited away as a duplicate.** Owner-facing output, not a trading-path defect — same class as item 89.
detail: docs/BOARD_NOTES.md (item 115)

**119. The economics feed can leave required series un-attempted at the open, and the re-derived fix is only measured mid-morning — OPEN, filed 2026-09-18.** Re-filed out of PR #435 (closed unmerged).

DONE WHEN:
  - [ ] every required series demonstrably gets a real attempt inside the existing ceiling, proven against open-like conditions rather than a healthy mid-morning batch
  - [ ] a series still missing after the bounded re-ask is named, the economist is not paid on the holes, and a later repair pays only once the set is complete
detail: docs/BOARD_NOTES.md (item 119)

**123. The unit-drift checker never looks at `.path` units, and one is tracked — OPEN, filed 2026-09-18. Pre-existing gap found while reviewing PR #435; NOT caused by it.** `scripts/check_unit_drift.py` now sets `UNIT_SUFFIXES = (".service", ".timer", ".path")` (was `(".service", ".timer")`): PR #642 (2026-09-24) added `.path` to the hand-maintained suffix tuple, so all four buckets — untracked, modified, undeployed, not-enabled — now see a `.path` unit, and a `.path` unit's `paths.target` enablement is handled the same as a `.timer`'s `timers.target` because `parse_wanted_by` reads the target from the unit. Test coverage for the four buckets on a `.path` unit was added afterwards (was missing when #642 shipped). STILL OPEN on the item's own DONE WHEN: the suffix set is still a hand-maintained tuple, not derived from the tracked files, and no run has yet compared the real `quant-agent-status-board.path` on the box against the checkout.

DONE WHEN:
  - [ ] the checker inspects every unit suffix the repo actually tracks, derived from the tracked files rather than from a hand-maintained list — NOT met: `.path` was added to the hardcoded tuple, not derived
  - [ ] the tracked `.path` unit's installed state is compared against the checkout at least once, so the gap closes with an observation and not only with code — NOT met: no real-box observation recorded
detail: docs/BOARD_NOTES.md (item 123)

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
  - [ ] all five sites carry a ledger entry with a source, or the open question and what the desk pays meanwhile, with no value changed in the same pass

**139. Roughly 74 of the repo's 90 registered git worktrees are session scratch under `/tmp` — filed 2026-09-18, housekeeping, pre-existing, nobody's current task.** CORRECTION to the filing brief: none of them is stale in git's sense — every registered path still exists, so `git worktree prune` removes nothing [verified 2026-09-18].

DONE WHEN:
  - [ ] finished sessions remove their own worktree, or a swept-on-a-schedule rule exists and is recorded
detail: docs/BOARD_NOTES.md (item 139)

**140. Nothing mechanically prevents a fifth duplicate board filing — filed 2026-09-18.** Four duplicate items were filed in one day by parallel agents hours apart, each writing up the same finding in different words without reading the board first; three have since been collapsed by hand.

DONE WHEN:
  - [ ] a check, not a rule, flags a new item that restates an existing one before it can merge
detail: docs/BOARD_NOTES.md (item 140)

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
  - [ ] both inline literals carry a ledger entry or are moved to a scanned definition site, with no value changed in the same pass
  - [ ] the correlation window's current value has a recorded reason, or is named as arbitrary like the threshold beside it
detail: docs/BOARD_NOTES.md (item 148)

**150. The live-capital pre-flight checklist exists NOWHERE in the repo as an actual gate — filed 2026-09-18, TIER 1.** The conditions that must hold before real money is switched on live in memory and prose, not in code or in a test, so nothing would stop or even notice a live-capital switch taken with an unmet condition.

DONE WHEN:
  - [ ] the pre-flight conditions exist as a single checked artefact that names each condition and its current state, and the owner has seen it before any live-capital activation

**152. A research seat's answer coming back unreadable has no board item — filed 2026-09-18, from the log-health report; the technical-seat half is SETTLED by #538 (2026-09-19), news seat still open.** A parse failure means the call was paid for and thrown away with nothing to show for it; measured on the retained logs: 11 on the news seat, 79 on the technical seat [measured 2026-09-18 against `quant_agent.log` and its five rotations].

DONE WHEN:
  - [ ] the news-seat parse-failure rate is understood and either brought down or shown to already recover cleanly on retry
detail: docs/BOARD_NOTES.md (item 152)

**154. A research seat being unreachable, with the work going ahead short-handed, has no board item — filed 2026-09-18, from the log-health report.** `Morning research degraded` fired 14 times across the retained logs.

DONE WHEN:
  - [ ] a decision made short-handed this way is marked as such wherever the desk records it, or the missing seat is shown not to change the decision
detail: docs/BOARD_NOTES.md (item 154)

**155. The item-135 short-side guard is a deliberate duplicate sitting one layer out from where it belongs — filed 2026-09-18, from PR #528's own objection record.** `_revert_entry_size_increases` in `src/pipeline_stages.py` re-does, for BUY and SHORT together, what `_apply_risk_modifications` guard 1b in `src/pipeline.py` already does for BUY alone; it was placed there only because `src/pipeline.py` was locked by another workstream at the time, not because two enforcement points are the right shape.

DONE WHEN:
  - [ ] guard 1b inside `_apply_risk_modifications` covers SHORT as well as BUY, `_revert_entry_size_increases` is deleted, and the no-op test is retired with it
detail: docs/BOARD_NOTES.md (item 155)

**157. The technical seat has no enforced answer format on either route, so a malformed row still needs salvaging after the fact — filed 2026-09-19, from #538's write-up.** #538 made a broken row recoverable, not prevented.

DONE WHEN:
  - [ ] a live call confirms whether the Google route enforces a sent response schema
  - [ ] a decision is recorded on whether the schema change is worth it given row-salvage already ships
detail: docs/BOARD_NOTES.md (item 157)

**158. The technical seat's per-stock drop reasons live only in the log and as a count, not per stock in the database — filed 2026-09-19.** #538 logs each dropped stock with symbol and reason and counts it, but writes nothing against the stock's own database row, so a later reader cannot tell why a name is absent without the log.

DONE WHEN:
  - [ ] the per-stock drop reason is stored alongside the stock it was dropped for, not only in the log

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
  - [x] a self-clear reaches the owner on the same surface the suspension did — the auto-expiry now sends the same Telegram alert the suspension does (🟢 RESUMED, stating the forgiven trigger and that it auto-expired), keeping the `auto_reset` DB event and log; durable/retryable like the quota-recovery alert
  - [ ] cooldown and allowance re-read against a real occurrence
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
  - [ ] `mechanical_heal_macro` (test-only dead code) either wired or removed — separate OWNER call, not touched here
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

**Retired item numbers — never reuse.** 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 57, 58, 59, 60, 61, 62, 66, 68, 69, 71, 72, 73, 79, 80, 81, 82, 83, 84, 85, 87, 88, 89, 91, 92, 93, 94, 96, 97, 98, 100, 101, 102, 103, 104, 105, 106, 108, 110, 111, 113, 116, 117, 118, 120, 121, 122, 124, 126, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 141, 142, 144, 145, 146, 149, 151, 153, 156, 159, 160, 161, 162, 164, 166, 167, 168, 169, 171, 172, 175, 176, 178, 181, 184 in this queue, and 1, 2, 3, 4, 5, 6, 7, 8 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md` — every retirement's reason lives there; this line is deliberately not re-narrated. Gate item 7 was moved, not closed: it is item 76. The two numbering schemes are separate — 3 is retired in BOTH, 20 is live here, and 40, 67 and 200 never existed [verified 2026-09-18 against this file's full git history]. Item 151 never sat on this board (filed and closed in the same change). The one piece of the items-135/136/137 PR NOT finished — the short-side guard living one layer out from where it belongs — is item 155, still OPEN work, not history. Item 38's follow-up survives as item 52, whose residue is item 63; item 53's overnight fractional-share gap is a STANDING BROKER LIMITATION, not an open item — do not re-file it. Residue of items 100 and 103 lives in items 106 and 115; item 89 was SHRUNK, not retired. Item 141 (live technical-seat ranking ties breaking alphabetically) was retired 2026-09-20; reason in `docs/INCIDENT_HISTORY.md`. Item 126 was retired 2026-09-20; residue is items 170 (disclosure-lag) and 169 (cockpit chart). Item 120 was retired 2026-09-23 — its SIZING half (new-name buy/short share counts dividing by a mid or stale price) kept it open past the 2026-09-20 rendering fix, so the earlier "retired 2026-09-20" claim was premature; reason in `docs/INCIDENT_HISTORY.md`, residue item 181. Item 111 (the earliest-trimmed symbol in a multi-symbol de-lever left naked) was retired 2026-09-20; reason in `docs/INCIDENT_HISTORY.md`. Item 168 was retired 2026-09-20 (upstream-history claim now rendered from `trading.lookback_days`, not a hand-typed number); reason in `docs/INCIDENT_HISTORY.md`. Items 164 and 171 were retired 2026-09-23; reasons in `docs/INCIDENT_HISTORY.md`. 171's intent survives as item 99(g). Item 32 was retired 2026-09-20 by owner instruction, not by a fix: he removed the whole account-level loss alarm — daily halt and 5d/20d BUY-halving brakes — instead of answering the one-response-or-two question. The §11.2 ladder stays. Items 92 and 144 were VOIDED with it, not answered: both asked about the daily-loss trigger and its 5d/20d rungs, which are deleted. The ladder's own unmeasurable-drawdown behaviour is a separate live question. Item 172 (an unreadable protective stop reaching no owner alert) was FILED AND CLOSED inside the same change that caused it, 2026-09-23: it never sat on this board as open work, and was fixed rather than filed because per-position stops became the only loss protection in the same commit. Item 181 (a SHORT's risk-budget divisor using the analyst's stale entry instead of the today print, inflating `qty_by_risk`) was retired 2026-09-24 — the risk-budget path now sizes off the print via a separate `risk_sizing_price`, the allocation path and the BUY path are unchanged; reason in `docs/INCIDENT_HISTORY.md`. Item 132 (the definition-of-done gate's blind commit range on a shallow checkout) was retired 2026-09-24; reason in `docs/INCIDENT_HISTORY.md`. Item 159 (dead Form 4 peek-ahead functions with no live caller) was retired 2026-09-24; reason in `docs/INCIDENT_HISTORY.md`. Item 81 (the reward:risk inventory's residue) was retired 2026-09-24 — `RiskConfig.min_reward_risk_after_widening`, `ConstructorConfig.min_reward_risk_after_widening` and the dead `SUBFLOOR_SIZE_CAPPED_STATUS` were deleted as zero-reader dead code, but `REWARD_RISK_FLOOR` was NOT deleted: it is still read by `ops/model_policy/deterministic_selection.py`'s model-selection benchmark; reason in `docs/INCIDENT_HISTORY.md`. Item 93 (twelve `docs/INCIDENT_HISTORY.md` entries mis-headed at `##` instead of `###`, invisible to the merge driver) was retired 2026-09-25 — the headings were promoted to `###` and a lint (`tests/test_incident_history_headings.py`) now fails the build if a dated entry sits at `##` again; this was fixed on main in PR #665 but the board entry was never removed, so WORK.md kept reporting it open; reason in `docs/INCIDENT_HISTORY.md`. Item 130 (the number-ledger's `SCOPED_PATHS` excluding the broker order path) was retired 2026-09-25 — shipped via #544 on 2026-09-19: `src/execution/broker.py`, `src/execution/stop_repair.py` and `src/coverage_watchdog.py` are already in `SCOPED_PATHS` (`src/number_sources.py`), verified still true on current main; reason in `docs/INCIDENT_HISTORY.md`. Item 108 (the position reviewer's 2% minimum stop-raise stated as a hard rule but not enforced) was retired 2026-09-25 — enforced via #641: `_midday_execute_llm_actions`'s TRAIL_STOP validation now reads the live broker stop and rejects an under-`MIN_RATCHET_PCT` ratchet (`src/pipeline.py`, constant single-sourced from `src.risk.trailing`), covered by `tests/test_exit_quality.py`; reason in `docs/INCIDENT_HISTORY.md`. Item 131 (the standalone coverage sweep leaving no record that it ran) was retired 2026-09-25 — closed via #547: `record_sweep_run` (`src/coverage_watchdog.py`) writes a `specialist_evidence` row every run and `sweep_log_line` writes the greppable log line, both called from `scripts/alert_heartbeat.py`; reason in `docs/INCIDENT_HISTORY.md`. Items 97, 116, 124 and 133 were retired 2026-09-25 on verification against current main — 97's pace already reads the horizon pinned at entry off the trade row (moot), 116's morning/midday/close/intra-check/pre-earnings answers are all persisted (shipped), 124's cluster fact is stamped on every row before truncation so within-symbol crowd-out cannot starve the seat (moot), and 133's held-name accounting is reconciled so a full held book is not read as a jam (shipped); reasons in `docs/INCIDENT_HISTORY.md`. Items 96, 102, 122, 129 and 162 were retired 2026-09-25 on verification against current main — 96 is stale (`veto_contradicted_exit` in `src/risk/exit_guard.py` already runs this check and is wired into the live path), 102 shipped (`src/pipeline.py`'s `partially_filled` branch now records the cumulative fill qty/price), 122 shipped (`scripts/merge_and_deploy.sh` copies changed units, daemon-reloads and enables them, showing a diff before overwriting a hand-edited copy), 129 shipped (`_is_terminal_broker_rejection` in `src/execution/broker.py` classifies a genuine rejection by status code so only a real transient failure spends the retry burst, and both retry constants are recorded as arbitrary with an open question in `config/number_ledger.yaml`), and 162 shipped (both of its own DONE WHEN boxes are checked, resolved 2026-09-23/25); reasons in `docs/INCIDENT_HISTORY.md`. Item 39 (opportunity-cost rotation) was retired 2026-09-25 — its last open thread, the categorical tier abandoning the whole rotation when its single worst below-bar holding was structurally protected, is fixed: the tier now walks the whole below-bar cull set worst-first and culls the first sellable name, abandoning only when all are unsellable; the ranked-margin tier stays OFF by owner mandate and 39(a) was already dissolved; reason in `docs/INCIDENT_HISTORY.md`. Item 80 (a model-typed stop with no computed level, signal bar or volatility band behind it) was retired 2026-09-25 (owner ruling) — the earlier REFUSAL-on-missing-ATR path was overruled; a missing ATR now derives the stop from price structure (nearest computed level on the protective side, else the signal/prior bar) and holds the position, skipping the name only when no structural level is readable or the readable one breaches the stop-distance sanity bound; reason in `docs/INCIDENT_HISTORY.md`. **Next free number is 182 (165-171 filed above/below, 172 filed and closed in one change, 173-177 allocated, 178 the execution SELL loop's split staleness posture, 179 the macro heal's shape-and-storage gap, 180 young-listing, 181 retired 2026-09-24 — the short-side risk-budget over-size) — and note that this sentence has been stale more than once**, because a number is claimed on a branch before it reaches this file. Check the open branches, not just this line.

## Evidence-only follow-ups — reopen only on concrete production evidence

- news-narrative factual drift; `actual_provider` attribution oddity.
