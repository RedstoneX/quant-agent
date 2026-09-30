# Board notes — owner-facing prose for the desk status board

**This file is NOT the source of truth.** `docs/WORK.md` is: it alone
records what this desk is doing — an item's number, its title, its status,
its ordering. `docs/board_notes/` supplies nothing but the words a trader
reads about each item: a plain-language explanation, a worked example, and
where a ruling is needed, the decision plus a recommendation. If this file
disagrees with `docs/WORK.md` about whether something is open, done, or even
still exists, `docs/WORK.md` wins, always.

**Who writes this file.** The orchestrating assistant, drafting explanation
for the owner in plain English — never the owner, and never a mechanical
process. Nothing here is generated, inferred, or summarised out of
engineering notes. An item with no entry below renders on the board with an
explicit "not yet explained in plain language" marker — that is the honest
state, and it is never papered over with invented prose.

**No size cap.** `docs/WORK.md` is mechanically capped at 100,000 bytes on
purpose (see
`tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`)
because it must stay a short, current, agent-facing backlog — not an
append-only prose archive. This file carries none of that risk. It can grow
without threatening the backlog's cap, so it has no size limit of its own.

`scripts/status_board.py` reads this file for prose and `docs/WORK.md` for
everything else, every time the board is built. See that script's module
docstring ("The prose problem, and the convention that solves it") for the
full mechanical account.

## How an entry is keyed

Each block below is introduced by a heading naming the exact item it
explains, by its NUMBER and SECTION — never by its title. A title can be
reworded in `docs/WORK.md` at any time; a prose block keyed to words would
silently go stale, or orphaned, the moment that happened. Keying on the
number and section survives a rename intact.

  * `## item N` — a funnel-queue item, N being its rank under
    `## THE FUNNEL QUEUE` in `docs/WORK.md`.
  * `## gate item N` — a PM-test-gate item, N being its rank under
    `## PM TEST GATE` in `docs/WORK.md`. The funnel queue and the gate both
    number from 1, which is why "gate item" and "item" are distinct keys —
    see `_SOURCE_REF_LABEL` in `scripts/status_board.py`.
  * `## decision due YYYY-MM-DD` — a pending `- [ ] DECIDE BY ...` line in
    `docs/WORK.md`, keyed by its due date because a decision carries no
    number of its own.

These are exactly the identifiers the board already shows the owner, and
that he quotes back to refer to something ("item 32", "gate item 4",
"decision due 2026-09-16") — see `QueueItem.ref` and `PendingDecision.ref`
in `scripts/status_board.py`. The heading is matched case-insensitively and
with any amount of whitespace, but the words and the number must be there.

Under each heading, each field on its own line, in the same shape
`scripts/status_board.py`'s `parse_prose` has always read:

    **Plain language —** what this is, in words a trader understands.
    **Example —** a concrete case that makes it tangible.
    **The decision —** what the owner specifically has to rule on.
    **Recommendation —** what we think he should do, stated as a
    recommendation.
    **Why only you —** the one reason this decision cannot be made without
    him: money, mandate, risk appetite, or public disclosure. Never a
    market-structure number — those are researched, not decided, and an
    item like that writes `**The decision —** None for you` instead.

The label must start the line, the surrounding `**` is optional, and the
separator may be an em dash, a hyphen or a colon. A block ends at the next
label, the next heading, or a blank line — one paragraph per label. Nothing
is mandatory: an item can carry only a `Plain language` line and nothing
else, and any field left out renders as absent, never guessed.

**"Waiting on you" on the board is built from two fields together.** An
item shows there only when it carries BOTH a live `The decision` (not
starting "None", "Not yet" or "Possibly") AND a `Why only you`. Writing a
decision without a reason, or a reason without a decision, leaves it where
it already was — in the running order, not promoted. See
`_is_live_owner_ask` in `scripts/status_board.py`.

## Worked example — illustrative only, not a real backlog item

The heading below uses the letter `N` in place of a number so it can never
collide with a real item in `docs/WORK.md`. It exists only to show the
shape; delete nothing above this section when adding real entries below it.

```
## item N

**Plain language —** The desk refuses a trade unless the likely gain is at
least one and a half times what it is risking.
**Example —** You want to buy at $100 with a stop at $98 and a target at
$105: risking $2 to make $5. The desk moves the stop to $95 on its own,
recalculates it as risking $5 to make $5, and refuses the trade.
**The decision —** Should a stop you can point at on the chart always be
honoured, however tight it is?
**Recommendation —** Yes. Pad the stop only when there is no real level to
put it at.
```

---

Real entries for the current backlog are drafted separately and go below
this line, one heading per item.

### What exists today [all measured 2026-09-26 against the production DB `/home/qamc/quant-agent/data/quant_agent.db`, read-only copy]

A paid intraday tick exists and is ON. `IntradayScanConfig.enabled` defaults False, but the deployed `config/settings.yaml` turns it on (the 2026-09-14 verification already corrected a stale comment claiming otherwise) and the cost circuit proves it: **211 `intra_check` sessions, 106 of them paid, $13.93 total — 62.8% of the desk's entire recorded model spend of $22.18** (`llm_budget_sessions`, 2026-08-25..2026-09-25). Morning, by comparison, is 31 sessions and $7.66. Per-day, intra_check outspends morning roughly eightfold.

TRIGGER. `move_threshold_pct` (flat 3% since the prior close, from one bulk snapshot call), then `cooldown_hours` (3h, same symbol), then `max_candidates_per_scan` (5 movers). Held investable names are added *on top of* the cap and consume no cooldown.

CADENCE. **Production is the systemd timer, not the APScheduler trigger** — verified on the box 2026-09-26 (`quant-agent-intra_check.timer` last fired 06:45, next 07:15). `OnCalendar=*:15,45` (moved off the shared `*:0/30` tick 2026-09-17) crossed with `run_if_et_window.sh`'s 09:30-16:00 ET window gives **13 ticks a day** — and the cost circuit records exactly 13 `intra_check` sessions on every trading day 2026-09-21..25, and 14 on every day before the timer moved. The `intra_check` mode is deliberately exempt from the once-per-day guard and the cross-mode session lock.

HELD BOOK. Measured on paid ticks joined to `specialist_evidence`: the symbol count is bimodal — 66 paid ticks analysed ≤5 symbols (movers only) and 39 analysed ≥11. **A tick carrying the held book costs 1.54x a movers-only tick** ($0.167 mean vs $0.108, n=39/66). The book stood at 10-12 names through the measured window, so held coverage roughly triples the tech payload of a capped scan.

### What the record says about each part

- **The flat trigger does not discriminate.** Across 253 `selected` rows (2026-09-02..25), the median move of a selection that produced a BUY or SHORT was **3.50%**, against **3.67%** for one that produced nothing. By bucket: 3-4% → 154 selections, 7 traded; 4-5% → 34/1; 5-6% → 25/0; 6-7% → 26/0; 7-8% → 6/2; 8-9% → 8/1. Raising the threshold would have removed the only band with volume and kept two bands that produced nothing. **Re-picking the flat number in either direction has no basis in the desk's own record.**
- **The ATR-relative form was unmeasurable.** The ledger's open question asks what move matters *relative to the name's own ATR*, and `intraday_evaluations.detail` stored `move_pct=` and nothing else — the denominator was never recorded. Fixed 2026-09-26: the row is now upserted after `compute_indicators` with `atr_pct=` and `move_atr=`, on bars the scan already paid to fetch, changing no behaviour and adding no row (`_record_intraday_trigger_atr_context`, `src/pipeline.py`). The question becomes answerable once enough rows accumulate. Coverage limit, stated: only names that already passed the flat 3% are stamped, so the record can show the trigger firing too *loosely* for a quiet name, never that it fired too tightly for a volatile one.
- **The cap binds, and binds silently.** Movers ledgered per run: 1→28 runs, 2→18, 3→16, 4→11, **5→20**. The cap of 5 is hit on 20 of 93 runs (21.5%), and the candidates dropped past it leave no record — exactly the `cost_while_unanswered` the ledger row predicted.
- **The yield.** 85 of 106 paid ticks (80%) produced no order at all. Across the whole record, paid intraday ticks are attributable to 21 new-position decisions (18 BUY + 3 SHORT), i.e. **$0.66 of model spend per position opened**. Over the retained report window, `intraday_no_trades` was 27 ticks for $4.39 and `intraday_executed` 10 ticks for $2.07.
- **Skips are already visible.** `paid_analysis_suspended`, `intraday_scan_lock_contended`, `intraday_scan_disabled`, `intraday_scan_crashed` and `intraday_scan_no_opportunity` all carry plain-English renderings in `src/notifier.py` and routing in `src/trader_feed.py`, and every return path is persisted to `intra_check_reports`. The doctrine that a budget-skipped tick must be visibly skipped is **already met**; no defect here.

### How the three parts interact — why they cannot be settled apart

The trigger sets how many movers *qualify*; the cap decides how many of those are paid for; the cadence multiplies whatever survives by 13; and the held book adds a fixed ~54% surcharge to every one of those 13 regardless of how many movers there were. So a day's paid intraday bill is roughly `13 × (movers-under-cap + held-book) × per-symbol cost`, and **the held book is the only term that is neither capped nor cooled down**. Loosening the trigger without touching the cap changes nothing (the cap already binds a fifth of the time). Tightening the trigger without touching the cadence saves little, because 13 ticks × held-book coverage is paid whether or not anything moved. Cutting the cadence is the only lever that scales all three at once — and it is also the only one that directly trades away loss-protection latency, because the same tick carries the free preamble.

### Still open, and exactly why

1. **The 3 `IntradayScanConfig` ledger rows.** `move_threshold_pct` cannot leave `arbitrary` yet: the measurement that would source it started on 2026-09-26 and has no rows. `max_candidates_per_scan` and `cooldown_hours` are **owner-appetite**, not research questions — see below.
2. **Every intra-preamble job on its own schedule.** Not attempted. Splitting fill reconcile, stop-out reconcile, protection-restore drain and repeg drain out of `run_intra_check` onto their own timers is a real infrastructure change across `src/pipeline.py`, `scripts/run_if_et_window.sh` and four new unit pairs, and it is the *precondition* for ever cutting the paid cadence — today the free safety work and the paid scan are welded to one 13-tick schedule.
3. **A correction the ledger needs and this change could not make** (`config/number_ledger.yaml` is held by another change): the `source:` on `src.config.INTRA_CHECK_TICK_MINUTES` names `src/scheduler.py:56` as "the authority for how often the intraday control actually fires". That is **false in production** — `src/scheduler.py` is the `--mode live` path and the box runs the systemd timer. The row's status can stay `sourced`; the source text should name `scripts/systemd/quant-agent-intra_check.timer` crossed with `run_if_et_window.sh`'s window, with `src/scheduler.py` as the live-mode mirror, and cite `tests/test_systemd_units.py` for the pin that now holds all three sites together.

## items 182 / 183 / 185 / 186 — consolidation check against item 90 (2026-09-30)
**Verdict: all four KEPT, none retired.** Each opens with "item 90's half two, surfaced for visibility", but each carries its own DONE WHEN criteria that item 90 does not own: 182 the ladder alert and cash-deficit cushion, 183 the order gates and the dead cash-sweep config removal, 185 the ATR-eligibility question and its two inherited rows, 186 three open owner-appetite answers. Retiring any would lose those criteria. The defect found was in item 90 itself: it claimed the four carry one word-for-word shared criterion, which was false (checked against each block). Item 90's line now names the four tranches and what each covers. No constant, threshold or value was chosen or changed.
