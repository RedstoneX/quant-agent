# QAMC Current Work

## Active finish line

### Session start — read this first

**STANDING PRINCIPLE — NO ARBITRARY NUMBERS, EVER.** Every trade constant comes from real data, a cited source, or the instrument itself — never a flat count, round % or a number that "sounds prudent". **Approval does not make a flat number non-arbitrary.** Mark unmeasured numbers provisional, never settled.

**STANDING PRINCIPLE — MISSING DATA IS A DEFECT IN THE PRODUCING STEP (owner 2026-09-17).** Find why a required field is blank and fix that step so it actually produces the data. Never invent. Never make skip/drop/ignore-and-continue the product. A drop-the-name quarantine is temporary. Item 78 is the current instance; the rule is not limited to it. Fuller statement: `docs/OUTCOME.md`.

**This file holds only open work.** Finished work is written up in `docs/INCIDENT_HISTORY.md` (append-only, each entry opening with one plain-language line) and then deleted here, together with its `## item N` block in `docs/BOARD_NOTES.md` and its number added to the retired line. `tests/test_status_board.py` fails if this file grows, passes 100,000 bytes, or loses an item number without retiring it. Ratified architecture decisions go in `docs/QAMC_REMEDIATION_SPEC.md` as a numbered phase, not here.

## DECISIONS PENDING — CI FAILS WHEN ONE GOES OVERDUE

**Do not delete a line to pass the build — decide it, then remove it in the SAME commit.** Format: `- [ ] DECIDE BY YYYY-MM-DD — question` (`test_no_pending_decision_is_overdue` parses it). It exists because a deferred decision was forgotten in 2026-08 and cost a zero-trade day.

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
- Branch protection requires up-to-date branches: merge `main` in, wait for CI, merge, repeat. Never `--admin`.
- `gh pr edit` and `gh pr view` fail on this repo (deprecated Projects-classic field). Use `gh api repos/RedstoneX/quant-agent/pulls/N -X PATCH` (body from a file: see `AGENTS.md`).
- Agents stall on polling loops: give every agent an explicit polling budget, or poll yourself.
- **Never hand-resolve a conflict in `docs/WORK.md`, `docs/BOARD_NOTES.md` or `docs/INCIDENT_HISTORY.md`.** They use `scripts/resolve_doc_conflict.py` as a git merge driver (`.gitattributes` + `scripts/git_merge_driver_docs.sh`; one-time per-clone `git config` in README.md "### Install"). A clone without that config gets ordinary conflict markers — if you see markers instead of a clean resolve or `REFUSING TO WRITE`, check `git config --get merge.docsmerge.driver` before touching them. The resolver merges numbered ITEMS, refuses unless every item on either side survives exactly once, and stops on a number collision (a renumber, never a delete); refusing is correct. Hand-resolving is how five live items were deleted (item 68). The CLI (`--from-index`, or `--kind`/`--base`/`--ours`/`--theirs`/`--out`) works without the driver, including dry runs.

### Ordered backlog — RESUME POINT

## THE FUNNEL QUEUE — why trades do not happen, ranked by measured cost

**Top of the backlog. Work it in order; do not reorder from intuition.** Ranks 1-8 share one census: **68 entry proposals, 2026-08-18 to 2026-09-02, 15 filled (22%)**, 6 of 11 sessions with zero fills — from the pre-reset DB via read-only `scripts/blocked_proposals_census.py`. A census item is struck only when its fix is merged AND re-measured against the same 68.

---

**17. Backup alert channel — OWNER DECISION, deferred, no due date.** No notification channel exists beyond Telegram, so an alert that cannot reach Telegram reaches nobody. Deferred by the owner 2026-09-03. Recommendation: `docs/BOARD_NOTES.md` ("item 17"). Everything else here shipped.

**18. 70% of the PM's prompt was earnings-filing prose, not a conclusion — MEASURED 2026-09-02, PARTIALLY FIXED, core cause MERGED 2026-09-04 (PR #252), real follow-ons below.**

Closed bulk is in `docs/INCIDENT_HISTORY.md` (item 18a-18e). **Still open:**
  - Section reorder (BUY eligibility to the top) — not done; verifying it needs a paid benchmark, which owner decision 2026-09-15 forbids unless he asks.
  - R/R reaches `rank_verdicts` only as a within-tier tiebreak (`_reward_risk_sort_values`) and net evidence not at all; whether either joins the composite score is undecided. Not an owner call. Per-seat sizing weights stay refused.
  - A spend cap on the OpenRouter key itself — outside our code, the one unbuilt leg of the cost-circuit replacement (flagged in `src/cost_circuit.py`). Verify the provider's limit options first.

**19. The model's consistency is an ASSET — three uses. Do not start these before item 18.**

Measured: 5 blinded runs, two arms, quality identical to four decimal places; 5 post-fix runs failing the same check on the same two names. (a) Use it as a test instrument: any change in its answer proves a pipeline change reached the model. (b) Stop paying for repeats where the answer does not vary (the PM seat is ~93% of the bill); measure first. (c) Subtract the stable famous-name bias arithmetically in the ratified weighted composite; three prompt-wording fixes measured no-change. Also: where the model's knowledge and our stored evidence disagree, that is a data gap or a stale belief. Secondary to item 18.

**20. GATE THE DECISION ON EVIDENCE COVERAGE — owner's design, 2026-09-02. PARTLY BUILT 2026-09-14. Do not trade on partial evidence.**

Owner's ruling: a decision on incomplete evidence is fabricated, not degraded. The categorical half is live on morning and the intraday scan: refuse before the PM when any seat's answer was LOST (`src/evidence_gate.py`). Write-up: `docs/INCIDENT_HISTORY.md`, 2026-09-14 / 2026-09-16. **Still open: the counting half, the owner's and not an agent's.** A minimum count of usable reads was not invented or shipped; no published source gives one, and fitting one to the desk's history is forbidden. Settles with his ratified number, or a ruling that partial coverage never gates. Never ship a placeholder.

**32. Should the desk have ONE drawdown response or two — OWNER CALL, the only thing left on this item.**

The three loss alarms (today / 5 / 20 sessions, against a multiple of the held book's normal daily move) versus the §11.2 de-levering ladder (distance below the equity high-water mark, fixed percentages): merge, or keep two? **Recommendation: keep two**, because merging re-decides trip points already set and they now do different things (one halts, one trims). Nothing blocks on it. Context: `docs/INCIDENT_HISTORY.md`, 2026-09-14.

**39. Opportunity-cost rotation — owner-requested, LIVE but never yet fired. `src/rotation.py`.** The CATEGORICAL tier (a holding that fails today's entry rules) can execute (`execution.rotation_enabled: true` since 2026-09-12): one zero-size PM target on the identical path as any close — constructor, hard rules, AI Risk Manager, claim check, protected-sell discipline; no bypass (verified 2026-09-13). The RANKED-MARGIN tier is prompt text and never executes. **2026-09-17 showed why it has never fired:** its FIRST condition is "out of risk budget", and risk that day was 7.4% of a 25% limit, so rotation never even evaluated. Adversary finding the same day, unresolved: when it DOES fire it sells first while the replacement buy is still refusable downstream — a naked sale with only an alert behind it. Fix the sequencing before treating a live firing as proof. The adversary's conclusion was that the real defects are the exit path and the buying-power plumbing (item 85), not a new selling rule.

**39(a). The 25% rotation score margin is an invented constant — OPEN research question, not an owner call.** `ROTATION_MARGIN_PCT = 0.25`: the shape is sourced, the number is not. Question: how much better must a candidate's composite verdict score be than a holding's before a swap beats round-trip cost, in verdict-score units? **Ruled out, do not repeat:** Grinold & Kahn, FTSE Russell banding, rebalancing tolerance bands (Alpha Architect, Kitces, Morgan Stanley), backtesting; and, 2026-09-17, the models' own confidence scores (unverifiable numbers must never rank or size trades) and entry-price-versus-now (fitting). Today it gates only whether a comparison is PRINTED; do not promote it to an execution gate until answered. Settles with: a published study of rank-swap turnover versus decay by SIGNAL gap, or a breakeven from measured round-trip cost against a score-to-expected-return mapping the desk does not yet compute (building that is the real prerequisite).

**52. What, if anything, should gate an insider trade on its SIZE — REFRAMED 2026-09-13, no longer an owner call.**

`min_transaction_value_usd: 100000` and `external_min_transaction_value_usd: 250000` (`config/settings.yaml`) are invented flat cutoffs and the desk's only size test. Relative size (`holdings_fraction` / `holdings_fraction_band`) is reported on every observation since 2026-09-13. **Ruled out, with sources: `docs/INCIDENT_HISTORY.md`, 2026-09-13**, including a per-transaction holdings-ratio cutoff that was built and REMOVED: do not reintroduce one without a per-transaction source. Settles with a published study of a SINGLE transaction's predictive content by size, or the conclusion that size gates nothing and both dollar filters are deleted. Cost: the direction of the error is unknown.

**55. What IS a structural level — how many bars make a swing point, and how wide is a level's zone? OPEN, filed 2026-09-13.**

Touch count is settled: `MIN_TOUCHES = 2` is sourced (Tsinaslanidis 2012 §4.4, §4.6.1) and pinned by `tests/test_level_match_zone.py::test_min_touches_is_two_and_that_one_is_sourced` — do not tighten it. Open, both convention: (a) `PIVOT_WINDOW` = 3 in `src/risk/trailing.py` and 5 in `src/data/levels.py`; (b) `CLUSTER_TOLERANCE_PCT = 1.0` in `src/data/levels.py` (a 2% span). **Every ruled-out source and quote, and why harmonising the windows is not an answer: `docs/INCIDENT_HISTORY.md`, 2026-09-14. Do not re-search.** **What would settle it:** run Tsinaslanidis §4.5's bounce test on the desk's own universe and bars, sweeping tolerance 0.5/1/2/3/5% and window 3/5/10/25 bars. A reading, not a fit. If it comes out flat, prefer a zone equal to the span of the pivot bars, which needs no constant. Cost: 1% decides "the same level", hence whether a stop is level-backed, the ATR floor, R/R and position size.

**56. Is a stop too wide, and read off what? The THRESHOLD is still unread. OPEN, narrowed 2026-09-14.**

Question: at what probability of being touched inside the trade's own horizon does a stop stop being a stop? Settled half, ruled-out sources and their quotes: `docs/INCIDENT_HISTORY.md`, 2026-09-14, including that "the desk's stops are 2.5x too wide" is WITHDRAWN (an ATR multiple means nothing without a horizon). Live: `max_target_reach_atr_multiple` and `max_stop_width_reach_atr_multiple`, both 1.5, neither derived; the gate refuses at a constant 1.67% touch probability. Do not re-propose "no floor, no trade" (falsified 2026-09-12) or import a foreign default. **Try first, no threshold needed:** (i) refuse when the stop's touch probability is below the target's reach probability on the same instrument, replacing the arithmetic ratio; (ii) delete the width gate, since §2.1 sizing already answers a wide stop with a smaller position. Cost: small — it under-refuses.

**63. `signal_weight` cannot say "pay attention, and the sign is the other way" — OPEN, no source found, carried out of item 52.**

One scalar in `[0,1]` is both the ranking key and the dollar multiplier, with no direction. Scott & Xu (FAJ 2004): an insider sale under 10% of the holding earns +0.68% adjusted quarterly excess return yet gets weight 1.0, identical to dumping 80% (-0.81%). Ratio and band are already reported so the seat can read the sign; the question is whether the deterministic ranking should know it too. **Ruled out, with sources: `docs/INCIDENT_HISTORY.md`, 2026-09-13.** Settles with a published signed scoring scheme, or enough own outcome data to read a separation.

**64. The backtest still rations the risk budget alphabetically when the budget binds — OPEN.**

Every result prints binding-budget days over entry days, and says equal asks are served by ticker spelling. Live spends down `rank_verdicts`; this engine has none. **Ruled out, with reasons: `docs/INCIDENT_HISTORY.md`, 2026-09-16**, notably ranking by the engine's own reward:risk, which would silently change who gets capital. Remaining: a non-fitted score that is the live rule (needs verdicts this engine cannot replay), or an owner decision that practice runs cannot evaluate rationing. Do not close as a ranking fix.

**65. Four of the five analyst seats have no strength scale of their own — OPEN, opened 2026-09-13.**

Only Technical states a lean (`magnitude`); news, macro, smart_money and earnings report `NO_STATED_STRENGTH` (0.0) and rank on weighted conviction alone. Should those four get a real strength field in their schema, or is direction plus confidence all they can say? Ruled out: deriving lean from a field they already report (double-counted conviction, deleted 2026-09-13); borrowing Technical's `buy` rung; fitting (forbidden, and the conviction ledger is far short of `_CONVICTION_OUTCOME_MIN_N`, not wired); a published cross-seat spacing (none exists). Settles with a per-call strength field the analyst must state and justify, or the ledger clearing 20 resolved calls per seat. Do NOT pick a number, and do NOT drop the four seats from ranking.

**70. One underived `1.0` is doing two different jobs in the exit path, and neither is read off anything — OPEN, filed 2026-09-14.**

`NOISE_BAND_ATR_MULTIPLE = 1.0` (`src/risk/exit_guard.py`) sets when an adverse move stops being noise and is reused as the margin in `check_structural_protection`; `absolute_min_stop_atr_multiple: 1.0` (`config/settings.yaml`) sets how tight a stop may be. Same round number, two questions, no source, nothing tying them. The first job blocked 7 of 8 recorded discretionary exits (2026-08-31, 2026-09-01); item 60 closed the plumbing beside it without answering why. Settles with, for each independently, a published measurement of the quantity it bounds, or a decision to derive one from the other as a single named constant. How readily the desk should block a sale at all is the owner's appetite, not this item.

**74. One piece of news can cut the same holding twice in a day, and whether that is a fault is a design question — OPEN, filed 2026-09-14.**

A midday REDUCE on a hard trigger (e.g. bearish earnings) can repeat at the close on the same trigger; `src/pipeline.py` discloses it as a RESIDUAL GAP and only warns, and the position-reviewer prompt explicitly allows a second cut on a cited hard trigger. Nothing deduplicates by event; the exit claim check never tests earnings. A later session may legitimately read the filing as worse; the 2026-05-04 AMZN double cut is the harm on the other side; frequency is unmeasured. Settles with a decision on whether a hard trigger is spent once acted on for a symbol that day, and if not, what distinguishes a worse reading from a repeat.

**Unsourced numbers from the 2026-09-11 audit — inventory, not an item.** ~20 trade-governing numbers, re-checked 2026-09-14: all live, three owned by items 52/56/70, the rest by nothing. List: `docs/INCIDENT_HISTORY.md`, 2026-09-14. Each becomes an item when worked; never re-audit.

**75. The desk has no automatic profit-taking: its target never reaches the broker, a trim for profit is not an allowed exit reason, and the trail sits too loose — OPEN, filed 2026-09-14 after an owner question on ORCL.**

**The full ORCL narrative and every number is in `docs/BOARD_NOTES.md` ("item 75") and `docs/INCIDENT_HISTORY.md`.** Short version: the TA target was used for R/R, seen by the Risk Manager, and shown to the position reviewer every session as "soft — you manage exit", while the reviewer's sell gate refuses profit-taking. Selling at target was +9.1%; the only automatic exit the code allows filled below entry.
**Blind spots (each as of that day):** `take_profit` is never sent to the broker (no caller passes `take_profit_price`); the target-breach flag needs >150% progress (ORCL peaked at 122%); profit-taking, rejection off a high and upcoming earnings are not allowed SELL reasons (trigger-word gate); only the news seat emits `state_changes`, so a chart breakdown cannot unlock an exit; `thesis_invalid_if` is pinned at entry; the earnings fetcher reads only 10-Q/10-K, so an 8-K results release is invisible. Unsourced trail numbers: `CHANDELIER_ATR_MULTIPLE` 3.0, `MIN_RATCHET_PCT` 2.0, trailing `NOISE_BAND_ATR_MULTIPLE` 1.25, the 3-bar swing window, the 150% breach flag, the 4-day discretionary cooldown. A trailing take-profit order is not placeable at Alpaca (fetched).
**Answered 2026-09-14 [measured], and it closes "pure TA several times a day would have sold it":** no standard single technical sell rule gave a timely ORCL exit that also beat chance on a 10-name basket (nine cited rules, no tuning; ORCL baseline 62%, basket 48%). Catching ORCL with one rule would have been luck — it supports multi-signal confirmation, nothing more. Structural finding from the same work: **the desk never sees today's bar**, so every intraday indicator and level describes yesterday's close.
**Owner asked (2026-09-14):** show each target on the Mission Control chart, and debate using it (a week-long paper trade was argued against). Proposed, not ruled: shadow-track without orders, rules fixed before looking — sell all at target / half at target plus trail / target arms a tighter trail / current desk.
**What would settle it — a fix, not a patch, never fitted to ORCL:** exits confirmed by several independent signals (structure break on a close, volume, news/earnings, sector) so one wiggle cannot whipsaw; trail tightness read off the instrument or a cited source; a decision on whether the target and a confirmed breakdown may trigger an exit; the 8-K gap closed. One number changed alone is the patch this item exists to prevent.

**76. PM-input shape: the one open piece is whether the PM uses its new macro-audit channel. OPEN, moved out of the PM TEST GATE 2026-09-14.** Write-up: `docs/INCIDENT_HISTORY.md`, 2026-09-13/14. Settles with a before/after benchmark of whether the PM uses `reasoning_chain.macro_audit`, which needs the owner's go. Do NOT reopen as a size problem.

**77. Model selection: analyst seats re-tested, PM seat is the next open question. Pointer, 2026-09-14.** Detail: `docs/architecture/MODEL_ROUTING_POLICY.md`. Blocked by owner decision 2026-09-15 (no test-environment work unless he asks).

**78. Isolating a blank "I'll sell if" name is TEMPORARY — DEFECT (patch).** Instance of the missing-data standing principle. Permanent fix: Tech and PM actually produce a real falsifier on actionable ratings and opens/increases; heal, then one paid retry; still blank → refuse before the book, reason `soft-exit missing after retry`. Never invent. Catalyst stays optional. Delete the isolate when a live session proves never-blank.

**79. The fat-finger guard is applied to the STOP as well as the entry, and it killed a real trade — DEFECT, filed 2026-09-17.**

The unsourced 20% deviation limit is applied to the stop price, not only to the entry/limit price. It refused a legitimate FLNC short on 2026-09-17 *after* the analysis chain had been paid for. FLNC's normal daily swing is 9.6% [measured], so a flat 20% cap bans any stop wider than about 2.1x its normal range: a volatility question answered with a constant. Fix: scope the guard to the entry/limit price, where a fat finger lands; do not swap 20% for another invented number. **Separable second half, the WORDING:** a refusal must show the name's normal daily range beside the percentage, or the owner reads a correct refusal as a bug (owner, 2026-09-17).

**80. Stop provenance — every shipped stop must trace to a computed level, the signal bar, or the volatility band. OPEN; the REFUSAL path is contested, filed 2026-09-17.**

Tracing is agreed. What is not: what the desk does when no provenance can be produced. QAMC Beta's contested principle is "produce the missing data, never drop the name", which reads on the standing missing-data principle above and against refusing. Unresolved: the session where the producing step still cannot produce. Settles with one ruling covering both, not two mechanisms.

**81. Invented reward:risk thresholds still size trades — OPEN, carried past the closure of items 1 and 4 (2026-09-17).** Those closed the leftovers found then; others survive and still gate and size real trades. Inventory them first, then read each off the instrument. A replacement constant is not a fix.

**82. `setup_type` is classified twice, inconsistently, so the risk reviewer cut a breakout it is forbidden to cut — DEFECT, filed 2026-09-17.** Two paths disagree about the same trade and the reviewer's breakout exemption was decided off the wrong one. Fix: classify once and carry it.

**83. The review path works from stale position state — DEFECT, two instances, filed 2026-09-17.** (a) The intraday check never reconciles fills, so it reasons about orders whose outcome is already known. (b) The position reviewer reviews names already sold: paid analysis on a holding that is gone, and an exit decision on nothing. Same root: nothing refreshes broker state before a review runs.

**84. All six timers fire in the same second — DEFECT, filed 2026-09-17.** The morning run and the intraday check collide at 9:30, and two jobs ran a stop repair at the same instant. Proposal, not ratified: move the intraday check to :15/:45, keep its exemption for the circuit breaker, and defer only the repair step.

**85. The portfolio manager is told it has no deployable capital when it has thousands — DEFECT, LIVE MONEY, verified against the live broker 2026-09-17.**

The PM briefing shows only the cash balance, with "no margin" hardcoded beside it, and the margin section is blanked whenever margin is ENABLED. With cash at -$916 the manager was told it had negative capital and no margin, and correctly refused a confirmed buy — while the account held $7,754 of overnight buying power against a 2x ceiling, roughly $8,000 of room (equity $9,736, holdings $10,652, 9 positions) [measured, live broker]. Fix: show real buying power and the remaining room. NOT built. Carry the cost of that room with it: borrowing runs about 6.25% a year on the overnight debit balance [measured], so the leveraged part of the book must beat 6.25%, not zero.

**86. OWNER DECISION — the live-fill websocket has NEVER authenticated, and there is no configuration-only fix. IMPLEMENT NOTHING HERE WITHOUT THE OWNER. Filed 2026-09-17.**

**Cause, verified.** The process holds placeholder credentials (29 characters, prefix `plac`); a local credential-injecting proxy substitutes the real key on outbound REST, but the websocket does not go through it — it dials Alpaca directly and presents the placeholder. It has never authenticated in any session since it was built.
**Two independent blockers, confirmed by research 2026-09-17.** (i) The installed `alpaca-py` stream uses the deprecated legacy `websockets` client, which has no proxy support at all; that landed in `websockets` 15.0, and only in the asyncio and sync clients. (ii) Even with a proxy-capable client it could not work: Alpaca authenticates with an in-band websocket MESSAGE, not a handshake header, and the gateway injects headers only.
**Options, best fit first.** Systemd encrypted credentials (`LoadCredentialEncrypted`; the app reads the credentials directory, no key in `.env` or the environment): best fit for the owner's goal of not holding the raw key in a file. A local shim holding the real key and rewriting the auth frame: the only option that keeps the key out of the trading process, but it is custom credential-handling code, which this project's runbook previously rejected. Teach the gateway frame-level injection upstream: correct, does not exist, slow. Make `alpaca-py` proxy-capable: does not fix auth alone. Accept REST polling and switch the socket off — no credential change, loses fill latency, stops ~150 error lines a day.
**Trading is NOT harmed:** REST order placement works and fill detection falls back to polling. **The failure is currently SILENT to the owner:** the per-episode alert was part of stopped work.
**The process lesson this item carries.** Five passes (#287, #420, #431, #432, #447, plus the still-open #435) optimised the TIMING of a path that had never once succeeded. A 100% failure rate is not a race condition: before any work to make a path faster or more reliable, prove it has succeeded at least once. The adversary was briefed on the timing question and answered only the claim as filed, so the existence question was never asked — brief the adversary with the existence question, not just the design question.

**Retired item numbers — never reuse.** 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 33, 34, 35, 36, 37, 38, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 53, 54, 57, 58, 59, 60, 61, 62, 66, 68, 69, 71, 72, 73 in this queue, and 1, 2, 3, 4, 5, 6, 7, 8 in the PM test gate, were deleted once written up in `docs/INCIDENT_HISTORY.md`. Gate item 7 was moved, not closed: it is item 76. The two schemes are separate — 3 is now retired in BOTH, and 20 is live here; 67, 90, 101 and 200 never existed. Item 38's follow-up survives as item 52, whose residue is item 63. Item 53's overnight fractional-share gap is a STANDING BROKER LIMITATION, not an open item — do not re-file. Items 79-86 were filed 2026-09-17: the carry-over list, the buying-power defect and the websocket owner decision. Next free number is 87.

## Evidence-only follow-ups — reopen only on concrete production evidence

- news-narrative factual drift; `actual_provider` attribution oddity.
- `get_latest_price` omits `feed`: reconciled, not open.
