# QAMC Remediation Specification

**Author:** Claude (Opus 5), with Rex Redstone
**Date:** 2026-08-27
**Status:** Owner-ratified. This document supersedes conflicting statements in `docs/OUTCOME.md`, `docs/STATE.md`, and `docs/WORK.md`.

---

## 0. The Mandate (corrected)

QAMC exists to **make money**. It is not a research experiment into whether cheap models add trading value. `docs/OUTCOME.md` currently states the latter; that framing was authored by coding agents and is wrong.

**The edge, stated so it is testable:**

> **Breadth × consistency × asymmetry.** Underwrite ~100 liquid names daily across technicals, fundamentals, news, macro and insider flow — coverage no individual sustains. Act identically every time, long or short. Risk a bounded fraction per idea, cut losers at pre-defined structure, and let winners run. Be right ~51% of the time and make roughly twice as much when right as is lost when wrong.

**Operating parameters:**

| Parameter | Value |
|---|---|
| Horizon | Swing — days to weeks |
| Direction | Long **and** short (real shorting, Alpaca margin) |
| Autonomy | Fully hands-off. The system never asks for trade approval. |
| Capital deployment | **Fully deployed.** No idle T-bill parking unless nothing is worth owning. |
| Max risk per trade | **5% of equity** (ceiling, not target) |
| Min risk per trade | **0.5%** — below this, don't trade |
| Max total at risk | **25% of equity**, correlation-aware |
| Environment | Alpaca **Paper** until explicitly authorized otherwise |

**Governing principle: agents decide, deterministic code bounds.**
The desk chooses what to buy, how much conviction it carries, where the stop belongs, when to exit. Deterministic Python enforces only the outer envelope — the limits that must survive a model having a bad day.

---

## Phase 0 — Gates (blocking; nothing else merges until done)

Nothing below should be merged until there is a working test gate. 94 PRs have already merged ungated; adding trading-logic changes to that pile would be reckless.

**0.1 — Enable CI.**
`.github/workflows/test.yml` exists but has **never executed**. Root cause is almost certainly that **GitHub disables Actions by default on forked repositories**.
- Enable Actions: repo Settings → Actions → General → Allow all actions. *(Owner action.)*
- Update the workflow to reflect current test/build requirements.
- Prove it runs green on `main`.
- **Deliberately push a failing test and confirm the merge is blocked.** This is the only step that constitutes proof. Do not skip it.
- Add branch protection on `main` requiring the check to pass. A gate that reports but does not block is not a gate.

**0.2 — Ratification rule.**
Root cause of QAMC's drift: coding agents write the documents that define "accepted truth," and `STATE.md` declares independent review *"optional evidence, not permission and not a blocking gate."* Scope decisions made for engineering convenience (e.g. dropping short selling) laundered into requirements that every later agent treated as owner intent.

- Amend `AGENTS.md`: agents may **propose** changes to `STATE.md` / `WORK.md` / `OUTCOME.md`; only the owner may **accept** them. Proposals live in the PR description until ratified.
- Any existing constraint in those documents is **unverified** until the owner confirms it.

**0.3 — Commit the production config drift.**
Live `config/settings.yaml` has uncommitted edits. `STATE.md` claims exactly one intended delta. Reconcile and commit, so what runs is what is in git.

---

## Phase 1 — Tech Analyst returns real structure — IMPLEMENTED (branch `feat/tech-analyst-structural-levels`)

Everything downstream depends on this. Stops, targets, thesis progress and pace are all currently derived from *invented* numbers because the analyst supplies no levels.

**Status note (2026-08-27):** the substance of 1.1–1.4 is implemented, with two departures from this text worth recording:

- The spec names a new `suggested_stop_price` field on the Tech Analyst's output. The implementation reuses the existing `stop_loss` field on `TechAnalysisResult` (`src/models.py`) instead of adding a differently-named one; `suggested_stop_price` remains a distinct field elsewhere, on the Portfolio Manager's `TargetPosition`, which `PortfolioConstructor._resolve_stop` still checks first.
- `reference_target` was made **mandatory** for every actionable rating (not "only where structure supports one" as originally written) — the validator in `src/models.py` requires it, `setup_type`, `expected_horizon_sessions`, and at least one support/resistance level together, or the result is rejected. For a `"breakout"` setup, `reference_target` is documented (`config/prompts/tech_analyst.md`) as a measured-move projection rather than a defended level.
- Two things were added beyond this section's original text: a standalone deterministic levels module (`src/data/levels.py`, new) that finds support/resistance from OHLCV bars via bad-print filtering, swing-pivot detection, zone clustering, distance/recency-weighted ranking; and raising `trading.lookback_days` from 320 to 1800 (~5 years, `config/settings.yaml`) so those levels are computed over real multi-year history rather than the ~1-year window the 320-day figure implied.

**1.1 — Require structural levels. DONE.**
The Tech Analyst must return, per candidate:
- `support_levels[]`, `resistance_levels[]` — actual prior consolidation / swing structure. **Done** — computed by `src/data/levels.py` over the full fetched history and required by the validator for actionable ratings.
- `suggested_stop_price` — derived from structure, never from a volatility multiple. **Done, via the existing `stop_loss` field** rather than a new field of this name (see status note above).
- `setup_type` — see 1.2. **Done** — `Literal["range", "breakout"]` on `TechAnalysisResult`, required for actionable ratings.
- `reference_target` — only where structure supports one. **Done, but made unconditionally required** for actionable ratings rather than conditional (see status note above).
- `expected_horizon_sessions` — the analyst's own estimate of time-to-resolution. **Done**, required for actionable ratings.

**1.2 — Classify the setup.** Two types, managed differently. **`setup_type` field is implemented and required; the differentiated exit-management behavior in the table below (fixed target vs. trailing-only, progress/pace enable/disable) is NOT implemented — that is Phase 3 work and remains pending.**

| Type | Definition | Stop | Exit management |
|---|---|---|---|
| **A — Range / level** | Clear overhead resistance exists | Below structural support | Fixed target; progress & pace metrics valid |
| **B — Breakout / trend** | No overhead resistance (all-time highs, clean break) | Below the breakout level or prior consolidation high | **Trailing only. No fixed target. Progress and pace metrics DISABLED.** |

**1.3 — No levels, no trade. DONE.** If the analyst cannot identify structure, the candidate is rejected: the model validator requires at least one support/resistance level for any actionable rating, and `PortfolioConstructor._resolve_stop` rejects (returns `None`) a BUY with no structural stop. The `2 × ATR` and `5%` fallbacks are deleted from `src/portfolio_constructor.py` (and the `default_stop_atr_multiple` / `fallback_stop_pct` config fields removed with them). `atr_14` is retained on `TechAnalysisResult` for a future noise-band use (Phase 3); it is no longer read as a stop source anywhere in `portfolio_constructor.py`.

**1.4 — Delete the invented target. DONE.** `entry × (1 + 2 × stop_gap_pct)` is removed from `portfolio_constructor.py`; a BUY with no `reference_target` from the analyst is now rejected rather than assigned a synthesized one, so a Type B (breakout) position without a real reference is a declined trade today rather than "no target, handled as a first-class state" — the latter is Phase 3 exit-rework territory (disabling progress/pace for breakouts) and remains pending.

**Phase 1b — market context. DONE, not in the original spec text.** Structural levels alone left the Tech Analyst without most of what a technical analyst actually reasons about — it had moving averages, RSI, MACD, Bollinger bands, ATR and a single volume-change percentage, and nothing else. `src/data/context.py` (new) computes, deterministically from bars already fetched, and renders via `format_context_block()` ahead of the levels block in the prompt:

- Relative strength vs a benchmark drawn from the same batch (SPY, else QQQ, else IWM — no extra fetch), over 1m and 3m.
- Returns over 1w / 1m / 3m / 6m / 12m.
- Position within the trailing 52-week range.
- ATR as a percentage of price, its percentile against its own trailing year, and an `expanding` / `contracting` / `stable` volatility state.
- MA20 / MA50 / MA200 slopes over a 10-session lookback — trend direction of the averages, not merely price's position against them.
- Consolidation detection: a candidate window must be **both** narrow (≤8% high/low spread over 15 sessions) **and** low-drift (net move ≤50% of the range) to be flagged — a narrow window alone does not distinguish a base from a slow steady trend.
- 20-day average dollar volume and 20-day up/down volume ratio (accumulation vs. distribution).
- Unfilled price gaps (≥2%, up to 3 most recent).

`src/agents/tech_analyst.py` wires this in per symbol and accepts an optional `days_to_earnings` kwarg to render an earnings-proximity warning line. **Not wired end to end**: `src/data/market.py` adds `MarketDataProvider.get_next_earnings_date()`, which estimates trading sessions to the next scheduled earnings report (approximate — calendar days × 5/7, not a precise trading-calendar count) — the first place in the system that has ever known a *future* earnings date, as distinct from `src/data/earnings.py`, which is retrospective (it finds filings already on EDGAR). This method exists and is tested but **nothing in the pipeline calls it or passes `days_to_earnings` to the Tech Analyst** — the kwarg is accepted and handled but always empty in practice today. `tests/test_context.py` (new, 27 tests) covers the module.

**Phases 2–7 below are still pending — none of this work has been implemented.** Only Phase 1, as described above, is done.

---

## Phase 2 — Risk-based sizing and correlation-aware budgeting

**Status note (2026-08-27, commit `c89e957`, branch `feat/risk-metrics-and-pm-correlation`, not yet merged):** landed as "Phase 2a" is the deterministic risk arithmetic this phase depends on — it closes four `AGENT_ROLE_AUDIT.md` audit findings (§1.1–§1.4), not this section's own numbered items:

- The drawdown-halve (audit §1.1) moved from a PM-prompt instruction into `src/risk/rules.py::apply_drawdown_scale`, with `drawdown_buy_cap` as a hard-block backstop. The PM prompt no longer pre-applies it.
- The correlation matrix (audit §1.2) is now built before PM decides (`TradingPipeline._ensure_correlation_matrix`) and PM's Quantitative Facts render the measured clusters (`src/data/correlation.py::correlation_clusters`).
- Portfolio heat (audit §1.3) — budget risk and open risk — is computed in `src/risk/metrics.py` and rendered to PM and RM with headroom under a new `risk.max_portfolio_risk_pct` config field (default 25). **That ceiling is reporting-only** — nothing gates on it yet.
- R-multiple (audit §1.4) is computed against the entry stop and rendered to the Position Reviewer.
- `src/risk/metrics.py`'s budget-risk arithmetic already implements §2.3's release condition below (a stop at or above entry zeroes that position's budget contribution) and the module docstring cites §2.3 directly — but this is exposed only as a reported figure. No gate consumes it, so the book does not yet actually expand/contract on it.

**Still NOT started:** §2.1 (risk-based position sizing formula, replacing percent-of-portfolio notional), §2.2 (correlation-aware budget ceiling enforced as a live gate, with a per-cluster share cap), and §2.4 (retire the fixed position-count concept — depends on §2.1/§2.2 existing as gates, not reports). This is Phase 2b.

**2.1 — Conviction is expressed as risk, not notional.**
The PM currently emits "BUY OKLO 3%" — percent of portfolio, which is risk-blind. A 3% position with a 10% stop risks 0.3% of equity; the same position with a 2% stop risks 0.06%.

Replace with a risk allocation in `[0.5%, 5.0%]` of equity. Share count is then derived:

```
shares = (equity × risk_pct) ÷ |entry_price − stop_price|
```

A wider stop yields a smaller position rather than a rejected trade. This eliminates the entire "stops too tight" failure class — risk is never controlled by squeezing the stop.

**2.2 — Correlation-aware portfolio budget.**
Total at-risk ceiling is **25% of equity**, but correlated names consume **one bet's** budget, not several.
- Cluster by sector *and* thematic exposure. `OKLO / CEG / VST / CCJ` is one nuclear bet. `NVDA / AMD / AVGO / MU / TSM / SMH / SOXX` is one semiconductor bet.
- Budget consumed by a cluster = the **sum** of its members' risk, and a single cluster may not exceed a defined share of the total (suggest 40%).
- Rewards genuine diversification; refuses to pay for fake diversification.

**2.3 — Risk is released, not static.**
Once a position's trailing stop sits **at or above entry**, its risk contribution drops to zero (or negative) and it stops consuming budget. The book therefore expands when trades are working and contracts when they are not — automatically, with nobody choosing a position count.

**2.4 — Retire the fixed position-count concept.** There is no target number of positions. The correlation-aware budget determines it dynamically.

---

## Phase 3 — Exit rework

This is where the money has been going.

**3.1 — Kill the pace feedback loop. (Highest priority defect in the system.)**
`pipeline.py:6318` computes `pace = progress ÷ (days_held ÷ avg_hold_days)` where `avg_hold_days` is drawn from the system's **own rolling 30-day realized-trade calibration** (currently ≈2.0 days). Selling quickly shrinks that figure, which makes every position look stalled, which drives more selling. It is a self-tightening noose.

- Replace `avg_hold_days` with the **`expected_horizon_sessions` pinned at entry** from the Tech Analyst (Phase 1.1).
- Never derive a trade's expected horizon from the system's own past behaviour.
- Do not evaluate pace until at least **one third** of the pinned horizon has elapsed. Before that the metric is mathematically meaningless.
- Disable progress and pace entirely for Type B (trend) positions.

**3.2 — Give the reviewer memory.**
`_build_own_recent_decisions` (`pipeline.py:6369-6409`) discards HOLDs and never passes prior metric values. The reviewer sold EPD for "not progressing" when progress had risen 16% → 20% and distance-to-stop had *improved*.
- Persist each review's metrics per position.
- Pass the previous snapshot into the next review.
- Deterministically reject a deterioration verdict when the deltas are positive. A model may not claim a position is stalling while its own numbers improve.

**3.3 — Close the first-sale-of-the-day loophole.**
The hard-trigger gate currently applies only to symbols already trimmed that day, so a position's *first* sale executes on soft reasoning unchecked — which is almost every sale. Apply the gate to **every** exit.

**3.4 — Route exits through AI Risk.**
`run_position_review` (`pipeline.py:6412`) calls only `position_reviewer`, then executes. This violates the `AGENTS.md` contract: `Specialists → Portfolio Manager → AI Risk → deterministic Python → broker`. Exits must pass the Risk Manager like entries do.

**3.5 — Upgrade the reviewer's model.**
`position_reviewer` runs on `google/gemini-2.5-flash-lite` — the weakest model in the stack — while the PM runs GPT-5.5. The consequential, loss-generating decision is on the cheap seat with no second opinion. Move it to a strong model; the cost is negligible against the losses.

**3.6 — ATR noise band on exits.**
An adverse move inside ~1 × ATR of entry is noise, not thesis failure. OKLO was sold at 0.67 × ATR. Reuse the existing 1.25 × ATR ratchet-band machinery.

**3.7 — Trailing stops, broker-resident.**
Trailing is arithmetic and belongs in deterministic code, not in an LLM's discretion:
- **Type A:** trail only after the target is exceeded.
- **Type B:** trail from entry — under each successive higher low (structural), with a chandelier stop as fallback where structure is unclear.
- Ratchet upward only, never down. Keep the existing ≥2% ratchet threshold and cooldown.

**3.8 — Exits the reviewer may still make on judgment.** The reviewer retains full authority to exit on **new information**: adverse news, earnings miss, macro regime shift, sector shock, correlation breach, thesis invalidation. Price movement alone is not new information.

---

## Phase 4 — Evidence symmetry and feed repair

**4.1 — Unblindfold the intraday buy path.**
`portfolio_manager.py:83-84` hard-returns a technical-only evidence registry when `session_type == "intra_check"`, even though the morning's macro is already loaded in memory. The 13:04 reviewer sees macro the 11:02 buyer was denied — which is precisely how OKLO was bought and killed within two hours on evidence that existed at purchase time.
- Pass the most recent macro/news/earnings state to the intraday PM, **clearly labelled with its age.**
- Stale-but-labelled beats structurally absent. The PM can discount an old read; it cannot discount one it never received.

**4.2 — Repair the data feeds.**
Production logs show Reuters Business 404, AP Business 403, repeated FRED timeouts, 28 incomplete tech batches, and 11 `Portfolio decision failed deterministic grounding` errors (the PM inventing holdings).
- Replace or re-point the dead news feeds.
- Add retry/backoff for FRED.
- **Add a feed-health gate:** if macro or news coverage is unavailable, the desk must know it is operating degraded, and that fact must appear in the Telegram alert and the dashboard.

---

## Phase 5 — Short selling

Owner-authorized. Currently **absent** from `src/execution/broker.py` — not disabled, not implemented. `allow_margin: false` is unrelated (it governs buying beyond cash).

- **Owner action:** switch the Alpaca paper account to a **margin** account. Shorting will not function on a cash account.
- Implement short entry/exit in the broker layer.
- Add borrow-availability and hard-to-borrow checks before submitting.
- **New risk math:** a short's loss is unbounded. Deterministic risk must treat shorts differently from longs — a stop is mandatory, gap risk is materially worse, and the position must be force-covered on breach.
- Teach the PM that the inverse ETFs already in the universe (`SH`, `SDS`, `PSQ`, `SQQQ`) are bearish instruments. Nothing in the codebase currently conveys this, so even the previously "sanctioned" bearish mechanism was never wired up.
- Short candidates flow through the same Specialists → PM → AI Risk → deterministic chain.

---

## Phase 6 — Cost circuit and transparency

**6.1 — Dollar-based budget with an afternoon reserve.**
`max_paid_sessions_per_mode_per_day: 2` tripped at 12:00 ET with **$0.59 of the $1.50 daily budget unused**, leaving four scheduled afternoon slots structurally dead.
- Cap on **dollars**, not session count.
- Reserve capacity for the afternoon so a quiet morning cannot consume the day.
- Suppress repeat suspension alerts — one notice, then silence until state changes.

*Note: prompt caching is deliberately disabled for a legitimate documented reason (it would make the circuit's pre-call reservations incomparable to actual spend) and would save essentially nothing here — one PM call per session, sessions 30 minutes apart, caches live ~5–10 minutes. Leave it off.*

**6.2 — Make the dashboard answer "why."**
The reasoning is already persisted; it is simply never shown.
- Add a `position_id` to `trades`, assigned when a BUY opens a flat position and inherited by every subsequent SELL / REDUCE / TRAIL_STOP against it until flat. Currently nothing links a buy to the sell that closed it.
- Add `GET /positions/{position_id}/history` returning the ordered chain: entry thesis, stop, each interim review decision, exit reason, realized P&L, hold duration.
- Render `reasoning` in the trades table (it is fetched and discarded today).
- Fix `PositionsPanel` so clicking a position opens its **entry** thesis, not today's re-evaluation.
- Add an `exit_reason_category` so exits are classifiable rather than free text.

**6.3 — Report the real number.** Telegram showed −$1.22 at 15:00 ET on 2026-08-26; the day actually closed **−$46.58**. The alert stream must carry a true end-of-day figure, and P&L should be expressed against risk capital as well as total equity.

---

## Phase 7 — Measurement

**7.1 — Build a backtester.** `src/replay.py` replays past decisions; it is not a strategy backtest. Without one, every change is a guess evaluated against one noisy live day, and a bad change is indistinguishable from a bad week. 419 commits in 17 days were merged into that blindness.

**7.2 — Calibrate conviction.** Log each trade's allocated risk against its realized outcome. If the desk's conviction predicts results, conviction-weighted sizing amplifies the edge. If it does not, flat sizing is superior — and that must be discovered from data, not assumed.

**7.3 — Track the edge directly.** Surface win rate, average win ÷ average loss, expectancy per trade, and average hold duration. The edge claim in §0 is a hypothesis until these numbers confirm it.

---

## Phase 8 — Documentation correction

- Rewrite `docs/OUTCOME.md` to the mandate in §0. The current "research experiment" framing is the root cause of the 99%-cash, 3%-starter-position behaviour — the system was built correctly for the wrong brief.
- Amend `docs/STATE.md`: remove the claim that the full chain applies to exits (it does not, until Phase 3.4), remove health claims contradicted by the failing feeds, and correct the "exactly one config delta" statement.
- Amend `AGENTS.md` with the Phase 0.2 ratification rule.
- Repo hygiene, low priority: delete the 83 merged-but-undeleted branches; triage the 26 orphans, rescuing the two abandoned VPS-security branches.

---

## Invariants (must hold at all times)

1. Alpaca **Paper** only. Live capital requires separate explicit authorization.
2. Deterministic Python risk and broker protections are **final authority and fail closed**.
3. Every position carries a **broker-resident stop** from the moment it is opened.
4. No single trade risks more than **5% of equity**; total at-risk never exceeds **25%**, correlation-adjusted.
5. `Specialists → Portfolio Manager → AI Risk → deterministic Python → broker` — for **exits as well as entries**.
6. No trade without analyst-supplied structural levels.
7. The system never blocks awaiting human approval.
8. Agents may propose changes to governance documents; only the owner accepts them.

---

## Execution order

```
Phase 0  ──▶  Phase 1  ──▶  Phase 2  ──▶  Phase 3
(gates)      (levels)     (sizing)      (exits)
                              │
                              ├──▶ Phase 4 (evidence + feeds)
                              ├──▶ Phase 5 (shorting)
                              ├──▶ Phase 6 (cost + transparency)
                              └──▶ Phase 7 (measurement)

Phase 8 (docs) — alongside, as each phase lands.
```

Phases 1 → 3 are the sequence that stops the bleeding. Phase 0 must precede all of it: none of this should be merged into a repository with no working test gate.
