## item 177

**Moved from WORK.md (2026-09-24) —** Questions: the 3 `IntradayScanConfig` ledger entries. Cadence: 3 code sites. Stop coverage is separately scheduled and free; the intra preamble (fills, stop-outs, loss check, drains) is not.

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
2. **Every intra-preamble job on its own schedule.** **DONE 2026-10-01.** The free safety work no longer depends on the paid tick: it is one shared method with two callers — the paid `intra_check` tick (unchanged) and a new free `intra_safety` mode with its own systemd service and timer. Additive, not a move, so there is no window in which protection is not restored; both callers take the same broker-write flock and the same blocking-owner check (item 127), so concurrent firing serialises rather than races, and each tick now has two independent chances at the safety work instead of one. The new timer's interval is the already-ledgered `INTRA_CHECK_TICK_MINUTES` (the cadence this work runs on today, so latency is unchanged by construction) and its phase is the midpoint of the two phases already in use on the box, which is the only one at that interval colliding with no existing unit; both are pinned by `tests/test_intra_safety_schedule.py`. **Nothing here licenses choosing a different interval** — the input that would, the observed distribution of how long a position actually stays unprotected, cannot be computed at all yet — `pending_protection_restores` records when the intent was written but the drain deletes the row on success, so nothing records when protection came back [measured 2026-10-01 against the production DB, read-only]. A cleared-at record has to exist before an interval can be chosen on evidence. **Consequence for the rest of this item:** cutting the paid cadence is now a pure spend decision and no longer trades against loss-protection latency.
3. **A correction the ledger needs and this change could not make** (`config/number_ledger.yaml` is held by another change): the `source:` on `src.config.INTRA_CHECK_TICK_MINUTES` names `src/scheduler.py:56` as "the authority for how often the intraday control actually fires". That is **false in production** — `src/scheduler.py` is the `--mode live` path and the box runs the systemd timer. The row's status can stay `sourced`; the source text should name `scripts/systemd/quant-agent-intra_check.timer` crossed with `run_if_et_window.sh`'s window, with `src/scheduler.py` as the live-mode mirror, and cite `tests/test_systemd_units.py` for the pin that now holds all three sites together.


### 2026-10-01 — what the paid tick actually wastes, MEASURED

The owner asked what the half-hourly `intra_check` session is losing. Measured
read-only against the production DB on 2026-10-01, across 116 recorded
half-hourly checks: 32 no-opportunity, 27 no-trades, 18 paid-analysis-suspended,
17 `evidence_gate_skip`, 9 `intraday_scan_crashed`, 1 `intraday_analysis_error`,
1 rejected, 11 executed. The session is 63% of lifetime model spend ($13.93 of
about $22) and sourced 37 of the desk's 80 trades, so the question is only about
the wasted ticks, not about the tick itself.

**ONE cause explains 9 of the 9 crashes, and it is not a defect in this repo.**
Every one of the 9 `intraday_scan_crashed` payloads carries the same error:
OpenRouter HTTP 402, "This request requires more credits, or fewer max_tokens.
You requested up to 16000 tokens, but can only afford 843/811/775". First
occurrence 2026-09-28 18:20 ET, last 2026-09-29 19:47 ET; eight of the nine fall
inside a single afternoon once the research balance ran down. The provider
refused before generating, so the refused call itself billed nothing — the loss
is the free setup work the tick does before reaching the paid call, repeated
every half hour while the balance stayed empty.

The 1 `intraday_analysis_error` is a DIFFERENT and unrelated cause: a
`pm_grounding_error` on 2026-09-25 where the PM's own output claimed news
coverage that did not exist and mislabelled a macro stance. That is the
grounding check doing its job and refusing an ungrounded decision, not a fault.

**FIXED here:** the naming. Reporting an exhausted research account as "the scan
for movers crashed" is a false statement about the desk's own state, which this
desk treats as a root-cause defect in its own right. A payment refusal out of
the scan is now reported as `intraday_scan_out_of_credit` with its own plain-words
line in the owner feed ("the research account is out of credit"), while staying
in exactly the same unhealthy, non-deciding, owner-visible class as the crash it
replaces. Nothing is swallowed, no retry/backoff/timeout was added, and no
number was introduced.

**STILL PRESENT, and deliberately not fixed here:** the tick keeps re-entering
the scan every half hour while the account is empty, doing its free setup work
and reaching a refusal each time. The cost-circuit latch that would stop that
(`provider_out_of_credit`) is self-clearing by design, so it re-arms rather than
holding. Pre-checking the remaining balance before the tick's work would need a
threshold — how little credit is too little — and this desk does not invent
numbers, so that is recorded as an owner appetite question, not picked here.

**NOT REPRODUCIBLE / not applicable:** nothing. Both causes are fully explained
by their recorded payloads.
