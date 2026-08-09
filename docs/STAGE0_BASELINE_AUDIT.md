# Stage 0 — Baseline & Integration-Seam Audit

Status: **complete**. Executed 2026-08-09 against `RedstoneX/quant-agent`.

This document is the Checkpoint A deliverable required by `docs/MILESTONES.md`:
verified source map, baseline test result, integration-seam report, donor
inventory and discrepancy report. **No feature code was added and no trading
behavior was changed.** Every claim below was checked against source; opinions
are confined to the clearly-marked final section.

---

## 1. Repository state (verified)

| Item | Value |
|---|---|
| Remote `origin` | `https://github.com/RedstoneX/quant-agent` |
| Working branch | `claude/qamc-stage-0-audit-xn0r6q` |
| HEAD | `0fd004bd36b0e10ca2dc7d640547cbdac07eba6b` |
| Default branch `main` | `0fd004bd36b0e10ca2dc7d640547cbdac07eba6b` (identical to HEAD) |
| Divergence `main..HEAD` | none at audit start |
| Working tree | clean |
| Other remote branches | `bootstrap/qamc-source-of-truth` = `e02b788691b46fc9170e906402616255f5c83f9c`; `bootstrap/qamc-source-of-truth-v2` = `6fc3cf14…` |

### Upstream relationship (verified)

- `PROJECT_COMPASS.md` / `UPSTREAM_INTEGRATION.md` claim the bootstrap baseline is
  upstream commit `6fc3cf14f4e6f9fde5f6c10fbe4a8d51e3d0f4e7`. **Verified correct.**
- That SHA is `HEAD~1` in this fork, and is also the **current `main` HEAD of
  `yebof/quant-agent`** (checked 2026-08-09 against a fresh clone of the public
  upstream). The fork is therefore *exactly level with upstream*, not behind it.
- Total QAMC divergence from upstream: **one commit** — `0fd004b`
  `docs(qamc): establish source of truth and staged implementation plan`,
  20 files, +598 lines, **all under `docs/` plus root `AGENTS.md`. Zero
  changes to `src/`, `tests/`, `config/`, `scripts/` or `main.py`.**
- Upstream mergeability at Stage 0 is therefore unencumbered.

Notes: the working clone is **shallow** (grafted at `ed124381`, 51 commits);
`git blame`/`bisect` beyond that needs `--unshallow`. No `upstream` remote is
configured locally (see discrepancy **D-6**).

---

## 2. Baseline test suite (verified)

```
python -m pytest tests/ -q
1431 passed, 1 warning in 31.49s
```

- **1431 passed / 0 failed / 0 skipped / 0 errors.** No pre-existing failures
  to document.
- Only warning: a `DeprecationWarning` from `websockets.legacy`, raised by a
  transitive dependency of `alpaca-py`, not by project code.
- The suite is **hermetic**: `tests/conftest.py` chdirs every test into a
  temp cwd, monkeypatches `requests.get` to raise, and clears `OPENAI_BASE_URL`
  / `OPENAI_CA_BUNDLE`. No `.env`, no API keys and no network were required
  or used.
- 66 test modules covering agents, prompts, risk rules, broker, pipeline
  stages, DB, scheduler, trading calendar, evolution and the bash ET-window
  wrapper.

### Environment caveat (not a repo defect)

The Claude-Code cloud container ships no project dependencies, and
`pip install -e ".[dev]"` **fails** with the system interpreter: Debian's
patched setuptools raises `AttributeError: install_layout` while building the
`ta` sdist. Installing into a clean `python -m venv` succeeds. Any CI or
session-start automation must create a venv rather than use system pip.

### What could not be exercised

`/data/` is gitignored and absent. There is **no SQLite database, no
`data/evolution/` artifacts, no checkpoints and no historical `agent_logs`
in this repository or container.** Every finding below is a static-source
finding. No production record was inspected, and no claim is made about
observed runtime data.

---

## 3. AI / provider architecture (verified)

Single routing layer: `src/agents/base.py` (907 lines). All nine agents
subclass `BaseAgent`; none construct their own client.

### 3.1 Provider & model selection

- Routing is **prefix-based on the model id**, decided once in
  `BaseAgent.__init__` (`base.py:438-487`):
  `deepseek-*` → DeepSeek (OpenAI SDK + `base_url=https://api.deepseek.com`);
  `gpt-` / `o1-` / `o3-` / `o4-` → OpenAI; everything else → Anthropic.
  Helpers `_is_openai_model` / `_is_deepseek_model` (`base.py:251-256`).
- **There is no `provider` field anywhere in configuration.** Provider is a
  derived property of the model string.
- Per-agent model selection: nine flat `str` fields on `LLMConfig`
  (`src/config.py:36-48`), plus nine optional `*_max_tokens` overrides
  resolved by `LLMConfig.get_max_tokens()`. Current `config/settings.yaml`
  sets all nine to `gpt-5.5` with `max_tokens: 128000`.
- `AppConfig._check_llm_provider_keys` (`config.py:286-323`) buckets the nine
  model ids by provider and fails config load if the matching key is absent.
- `OPENAI_BASE_URL` re-points all OpenAI traffic at a relay; `OPENAI_CA_BUNDLE`
  supplies a private trust anchor for it. Both read explicitly in
  `__init__`, scoped to the OpenAI client only.

### 3.2 Retries, timeouts, concurrency

| Control | Location | Value |
|---|---|---|
| Attempt budget | `_max_retries()` | 7 (`QUANT_AGENT_MAX_RETRIES`) |
| Backoff | `_retry_backoff_seconds()` | `[2^n, 2·2^n)` — exponential floor + full jitter |
| Wall-clock deadline | `_retry_deadline_s()` | 480 s (`QUANT_AGENT_RETRY_DEADLINE_S`) |
| Server hint | `_retry_after_hint_seconds()` | honored, capped at 120 s |
| Retryability | `_is_retryable()` | 429/5xx + transient classes retry; **402 and other 4xx fast-fail** |
| Per-request HTTP timeout | `_LLM_HTTP_TIMEOUT` | 300 s, passed to both SDKs |
| SDK-internal retries | client ctor | `max_retries=0` — the agent loop is the sole retry owner |
| In-flight cap | semaphores | OpenAI/relay 3 (`QUANT_AGENT_MAX_CONCURRENT_LLM`), Anthropic 4 |

Two degenerate-response classes are raised rather than returned as success:
`LLMEmptyResponseError` (HTTP 200 with no content) and
`LLMStreamInterruptedError` (stream ended with no `finish_reason`). Both are
classified retryable. The OpenAI path is deliberately **streamed** so a
Cloudflare-fronted relay cannot 524 a long generation.

### 3.3 Fallback behavior

`_execute()` → `_try_failover()` (`base.py:576-590`, `682-710`):

- Fires only when the primary is **OpenAI or DeepSeek** *and* a
  `fallback_api_key` is configured. A Claude primary is a deliberate no-op.
- Target is hardcoded: `_FALLBACK_MODEL = "claude-opus-4-7"`.
- **Single shot, no retries.** On failure the original primary error re-raises.
- Truncation (`max_tokens` / `length` / `insufficient_system_resource`) never
  triggers failover.
- `src/pipeline.py` passes `fallback_api_key=config.api_keys.anthropic` to all
  nine agents.

### 3.4 Token, cost and result accounting

- Usage extraction: `_extract_anthropic_usage` (sums `input` +
  `cache_creation` + `cache_read`) and `_extract_openai_usage`, both routed
  through `_coerce_token_count` which returns 0 for anything not a real `int`.
- If a relay omits the `include_usage` chunk, `_call_openai` falls back to a
  `chars/4` estimate and logs a warning — deliberately preferring a soft
  number to a `0/0` that would be recorded as cost-unknown.
- `estimate_cost(actual_model, …)` (`src/cost_table.py`) resolves rates in the
  order pinned → LiteLLM cache → hardcoded fallback → one-time live lookup, and
  returns `None` (rendered `$?.??`) rather than fabricating a price.
- `AgentResult` (`base.py:317-339`) carries `raw_text`, `tokens_used`, `model`
  (**actual**), `user_message`, `input_tokens`, `output_tokens`, `cost_usd`,
  `finish_reason`, `truncated`.

### 3.5 Attribution findings

**F-1 — the recorded model is the *requested* model, not the actual one.
(Verified. Directly contradicts DECISION #12.)**

All nine `insert_agent_log(...)` call sites pass
`model=config.llm.<agent>_model`:

`pipeline_stages.py:280` (macro), `:328` (tech), `:483` (PM), `:699` (RM);
`pipeline.py:4704` (news), `:6109` (position_reviewer), `:6296` (earnings),
`:6654`/`:6668` (evening), `:7050` (meta_reflector).

`AgentResult.model` — the field that holds the model that actually answered —
is read in exactly **one** place in the codebase (`tech_analyst.py:193`, an
internal chunk merge) and is **never persisted**. Consequence: when
cross-provider failover fires, the row reads
`model='gpt-5.5'` while `cost_usd` was priced at `claude-opus-4-7` rates. The
record is internally inconsistent and the fallback is silent — precisely what
DECISION #12, `MODEL_PROVIDER_ARCHITECTURE.md` ("Required contract") and
`ACCEPTANCE_CRITERIA.md` ("Actual model/provider used is recorded for every
LLM invocation") forbid.

**F-2 — fields the contract requires that have no storage at all. (Verified.)**
`agent_logs` columns are: `agent_name, run_id, input_summary, input_message,
output_summary, full_response, model, tokens_used, input_tokens,
output_tokens, cost_usd, timestamp`. There is **no** `provider`, no
`requested_model`, no `latency`, no `prompt_version`, no `finish_reason`, no
`truncated`. A repo-wide grep confirms `latency`, `prompt_version` and
`prompt_hash` appear only in comments. `truncated`/`finish_reason` are
computed and logged but never written.

**F-3 — tech_analyst is one log row over N HTTP calls. (Verified.)**
`analyze_batch` chunks the universe and merges chunk results, keeping only
`last_model` (`tech_analyst.py:193, 204`). Per-invocation attribution is
structurally unavailable for this agent: a run where chunk 1 succeeded on
OpenAI and chunk 2 failed over to Anthropic is unrepresentable even in memory.

**F-4 — relay attribution has a ceiling. (Verified by design reading.)**
With `OPENAI_BASE_URL` set, the recorded model id is what QAMC *asked the relay
for*. Nothing verifies what the relay actually served. For an experiment whose
central question is "can inexpensive modern AI models add measurable trading
value", this is an upper bound on attribution integrity that no amount of
QAMC-side plumbing can raise.

### 3.6 Least-invasive seam for provider abstraction & attribution — *recommendation only, not implemented*

Ordered cheapest-first:

1. **Attribution (≈9 lines, behavior-neutral).** Change
   `model=config.llm.<agent>_model` → `model=<result>.model` at the nine call
   sites. The correct value is already in hand and discarded.
2. **Schema (additive, existing mechanism).** `db._migrate()` already has an
   idempotent `_ensure_column(table, column, ddl)` helper used for 18 prior
   columns. Adding nullable `requested_model`, `provider`, `latency_ms`,
   `finish_reason`, `truncated`, `prompt_version` to `agent_logs` needs no
   migration framework and cannot break old readers.
3. **Producer.** `_execute()` already knows the provider (three explicit
   branches) and can time itself; add `provider` and `latency_ms` to
   `AgentResult`. ~10 lines.
4. **Provider abstraction.** The only provider-aware code is
   `BaseAgent.__init__`'s client construction plus `_call_openai` /
   `_call_deepseek` / `_call_anthropic`. A `Provider` strategy object with
   `build_client()` and `call()` is the minimal shape, and it leaves
   `run()`/`_execute()` — the hardened retry/deadline/failover/truncation loop
   — untouched. **Recommend explicitly that Stage 1 not refactor `_execute()`.**
   Every constant in it is a scar from a dated production incident.
5. **Correlation IDs.** `RunContext.run_id` (`{session}-{uuid8}`,
   `pipeline_context.py:80-87`) is already written to both `agent_logs.run_id`
   and `trades.run_id`. Run-level correlation exists today and needs nothing.
   The genuine gap is **decision-level**: no id links a PM target → constructor
   decision → order → trade. Cheapest sufficient addition is a single
   `decision_id` column on `trades`.

---

## 4. Trading decision chain (verified)

Composition for morning is explicit in `src/pipeline_stages.py`; midday/close
and evening are single-stage flows on `TradingPipeline`.

```
MorningResearchStage        ThreadPoolExecutor(4): macro | news | tech | earnings
  │                         each branch failure isolated → data_status[k]='failed'
  ▼
DecisionStage               8 memory layers + PMFacts → PortfolioManager.decide()
  │                         → PortfolioConstructor.construct_orders() → decisions
  ▼
RiskStage                   ① _filter_supported_symbols   (deterministic)
  │                         ② _clamp_queued_earnings_buys (deterministic)
  │                         ③ correlation matrix build
  │                         ④ _filter_hard_risk_decisions (DETERMINISTIC GATE #1)
  │                         ⑤ RiskManager.review()        (AI, advisory/veto)
  │                         ⑥ _apply_risk_modifications + _apply_scale_all_buys
  │                         ⑦ _filter_hard_risk_decisions (DETERMINISTIC GATE #2,
  │                            re-run whenever ⑥ changed anything)
  ▼
ExecutionStage              HOLD audit → SELLs → wait fills → refresh →
                            daily-loss re-check → cash-sweep funding → BUYs →
                            post-fill protective stops
```

### Where AI judgment occurs
`macro_analyst`, `news_analyst`, `tech_analyst`, `earnings_analyst` (research);
`portfolio_manager` (targets + conviction); `risk_manager` (approve / modify /
`scale_all_buys`); `position_reviewer` (midday + close, sell-side only);
`evening_analyst` (reflection); `meta_reflector` (quarterly).

### Where deterministic authority occurs
- `src/risk/rules.py:RiskRuleEngine.check()` — position %, net exposure,
  daily loss, stop-loss required, correlation cluster, cash-only, sector %.
- `pipeline.py:HARD_BLOCK_RULES` (`:61`) — the six rules whose violation
  *blocks*, as opposed to being passed to the RM as advisory context.
- `pipeline.py:_filter_hard_risk_decisions()` (`:448`) — the gate itself.
- `pipeline.py:_force_delever()` (`:5231`) — biggest-loser-first forced selling
  when cash < `MARGIN_DEFICIT_FLOOR_USD`; runs before LLM involvement.
- `pipeline.py:run_intra_check()` (`:6331`) — zero-LLM circuit breaker.
- `pipeline_stages.py:918-928` — pre-BUY daily-loss re-check after research
  latency.
- `pipeline_stages.py:988-1105` — 5 % entry-staleness skip, 1×ATR stop-distance
  floor, post-geometry-change R/R ≥ 1.2 re-check, risk-budget share sizing
  (0.5 % of equity), `estimated_cost > available_cash` skip.
- `broker.submit_order()` — `_quantize_price()` tick normalization and a 20 %
  fat-finger deviation guard that returns `rejected_outlier`.

### Fail-closed behavior (verified, and unusually thorough)
`RiskRuleEngine.check()` returns a synthetic `max_total_position_pct` violation
— i.e. a **hard block** — rather than an empty list when it cannot compute:
non-finite/`<=0` `total_value` (`rules.py:70`), non-finite position
`market_value` (`:102`), non-finite `cash` (`:119`). `check_daily_loss` logs
loudly and returns `None` on non-finite inputs, with `_force_delever` as the
downstream net. `_apply_risk_modifications` **drops** a decision whose
RM-proposed modification fails validation, rather than executing the
unmodified original. `decision_checkpoint.mark_consumed()` falls back to
`unlink()` if rewrite fails, because a surviving checkpoint is the unsafe
direction.

### Paths that could bypass the shared deterministic gate

Two, both verified, both deterministic and neither LLM-reachable:

- **`src/execution/cash_sweep.py:306` — `SWEEP_BUY`.** Parks idle cash in
  `SGOV`. Does **not** pass through `RiskRuleEngine.check()` or
  `_filter_hard_risk_decisions`, and is deliberately submitted with
  `stop_loss_price=None`. This is coherent — the vehicle is treated as
  cash-equivalent everywhere (hidden from every LLM view, credited to cash in
  the cash-only rule, exempt from stop-coverage audits, liquidated first by
  `_force_delever`) — but it means the statement "no BUY reaches the broker
  without passing the hard risk filter" is **false as written**. The accurate
  statement is that `SWEEP_BUY` is governed by its own deterministic bounds
  (`enabled`, `reserve_pct`, `min_order_usd`, config-fixed symbol) rather than
  by the shared gate. QAMC's safety documentation should say so explicitly.
- **`_apply_risk_modifications` can loosen, not only tighten.** `modifiable_fields`
  is `{allocation_pct, entry_price, stop_loss, take_profit}` with no direction
  check, so the AI Risk Manager can raise an allocation or widen a stop.
  Allocation increases *are* caught: gate #2 re-runs the full hard filter
  whenever modifications were applied. A **widened `stop_loss`** is not
  re-audited for R/R, because `geometry_changed` in `pipeline_stages.py:1065`
  compares execution's stop against the already-RM-modified value. `require_stop_loss`
  still enforces `stop_loss > 0`. This is a narrow AI-loosens-its-own-audit gap,
  not a hard-control bypass; recorded for completeness.

No path was found by which a *frontend* or any non-trading process could reach
the broker — there is no server, no API and no writable surface in the codebase
today.

---

## 5. Alpaca order & protection lifecycle (verified)

`src/execution/broker.py` (1634 lines), `AlpacaBroker`.

- **Construction** — `TradingClient(key, secret, paper=config.alpaca.paper)`;
  `_install_http_timeout()` injects a 30 s HTTP timeout into every SDK call.
- **Entry** — `submit_order()` quantizes prices (≥$1 → $0.01, <$1 → $0.0001),
  applies the 20 % fat-finger guard, then submits a `DAY` limit or market order.
  It returns a dict echoing `id, status, symbol, side, qty, limit_price,
  stop_loss_price, pending_stop_price`. `status` is unwrapped via
  `getattr(order.status, "value", …)` because `str(enum)` would defeat the
  rejection check.
- **Entry protection** — deliberately **not** an OTO leg. `StopLossRequest`
  carries no TIF of its own and would inherit the parent's `DAY`, so the broker
  expired every BUY-attached stop at 16:00 ET and positions sat naked overnight
  (2026-07-16 audit, reproduced in production on VST). Instead
  `place_entry_protection()` waits for the entry to reach terminal, **cancels
  any still-working remainder** so no share can fill unwatched, re-reads the
  actual `filled_qty` and submits a separate **GTC stop-limit** sized to the
  real fill with a 3 % limit buffer below the stop.
- **Exit** — every SELL path funnels through
  `pipeline._submit_protected_sell()` (`pipeline.py:928`), which enforces the
  three-step discipline: `cancel_protective_stops()` with a write-ahead record →
  submit → `_order_accepted()` → restore stops on rejection. Callers:
  ExecutionStage, ex-dividend handling, auto-take-profit, midday emergency
  liquidation, midday LLM actions, `_force_delever`, intra_check, cash-sweep
  funding. **No SELL path bypasses it.**
- **Post-sell finalize** — `_finalize_pending_protections` →
  `_finalize_protection_after_sell_core` (`:1105`) resolves protection against
  the **actual fill quantity**: 0 filled → restore originals; partial →
  reprotect the real residual; full → clean exit. A non-terminal order is
  force-cancelled first so finalize does not race the broker over
  `held_for_orders`.
- **Durable recovery** — anything finalize cannot repair in-process is written
  to the `pending_protection_restores` table by
  `_persist_orphaned_protection_restore` (`:1539`) and replayed at the start of
  the next session by `_drain_pending_protection_restores` (`:1604`). Partial
  restores persist only the *failed* specs, avoiding duplicate live stops.
- **Reconciliation** — `_reconcile_fills` (`:1958`) updates
  `fill_status/fill_qty/fill_price/fill_reconciled_at`;
  `_reconcile_orphan_pending_submits` (`:2024`) sweeps write-ahead
  `pending_submit` rows whose submit crashed, matching broker activity by
  symbol + qty + time window; `_reconcile_stop_coverage` (`:769`) /
  `_repair_stop_coverage` (`:852`) audit held-vs-covered quantities and rebuild
  missing stops.
- **Write-ahead discipline** — every BUY inserts a `pending_submit` trades row
  *before* the broker call, then `confirm_trade_submitted` or
  `mark_trade_submit_failed`. A SIGKILL mid-submit leaves a recoverable row
  rather than a phantom.
- `broker.close_position()` exists but has **no caller** in `src/`.

---

## 6. Persistence & learning (verified)

### SQLite — `data/quant_agent.db` (`src/storage/db.py`)

| Table | Role |
|---|---|
| `trades` | every decision & order incl. HOLD audit rows; `run_id`, `broker_order_id`, fill columns, `stop_loss`, `take_profit` |
| `positions` | current book snapshot |
| `agent_logs` | per-agent call record: `input_message`, `full_response`, `model`, token split, `cost_usd`, `run_id` |
| `daily_pnl` | date-keyed NAV, daily P&L, `equity_close` |
| `insights` | evening output: outlook, lessons, suggested actions, risk rating, bias/conviction/key-risks, `sell_grades_json`, `buy_grades_json`, `missed_opportunities_json` |
| `pending_protection_restores` | durable stop-restore queue |

Schema evolution is by idempotent `_ensure_column` ALTERs (18 in place) plus
`CREATE INDEX IF NOT EXISTS` on the prune columns. All writes go through a
`_lock` with a 5-attempt backoff on `locked`/`busy`. `save_evening_snapshot`
writes `daily_pnl` + `insights` in one `BEGIN`/`COMMIT`.

### Filesystem (all under gitignored `/data/`)

`data/evolution/{period}/{digest,reflection,proposed_edits}.json`,
`data/checkpoints/{et_date}-{session}.json[.status]`,
`data/earnings/{SYMBOL}/analysis_*.md`, `data/news`, `data/macro`, `data/tech`,
`data/evening_replays`, `data/pricing_cache.json`.

### Reflection & Meta Reflector

- **Evening** (`run_evening`, `pipeline.py:6469`) — 7-step CoT, persists
  structured `sell_grades`/`buy_grades` and `missed_opportunities`;
  `_build_trade_grade_summary(lookback=14)` feeds them back into
  `position_reviewer`; `outlook_calibration` scores the analyst's own prior bias.
- **Quarterly meta** (`run_quarterly_meta_reflection`, `:6947`) — deterministic
  `build_quarterly_digest` (90-day facts + `agent_prompts_snapshot`) → 7-step
  `meta_reflector` LLM → `persist_reflection` → `PromptEditor.apply_reflection`.
  Gated on `broker.is_last_trading_day_of_quarter` unless `--force`.
- **Editor guardrails** (`src/evolution/prompt_editor.py`) — mode is
  `enabled × dry_run`, logged loudly at entry. Live config is
  `enabled: true, dry_run: true` = **STAGE-ONLY**: proposals are written to
  `proposed_edits.json` and **no prompt file is modified**. Four guards:
  FIFO cap (10/agent), Jaccard dedup (0.6), prohibited-words regex, git
  auto-commit for one-command rollback. `risk_manager` and `position_reviewer`
  are excluded twice — by the `MetaReflectionAgentName` schema literal and by
  `evolution.protected_agents`.
- Replay harness: `src/replay.py` + `scripts/replay_decision.py` re-feed a
  stored `agent_logs.input_message` through the *current* prompt+model and diff
  the decisions. This is the seam that makes prompt changes testable.

### Can QAMC use this memory architecture without a parallel system? — **Yes.**

The canonical record is complete enough to reconstruct a trading day: `trades`
(+ fill columns) gives proposed→executed, `agent_logs` gives every agent's full
input and output joined by `run_id`, `insights` gives the narrative, `daily_pnl`
gives the outcome, and `data/evolution/` gives the learning history. A journal
read-model and a search index can be **derived and rebuilt** from these with no
new authoritative store — consistent with DECISIONS #18/#19.

Two honest caveats: (a) the attribution columns in **F-1/F-2** are missing, so
a "model scoreboard" built today would be wrong on any failover day; (b) the
`run_id` join is run-level only, so "follow one candidate end-to-end"
(Stage 4's checkpoint) needs the `decision_id` addition noted in §3.6.

---

## 7. Scheduler & runtime (verified)

Two independent execution paths:

- **`--mode live`** — `src/scheduler.py:TradingScheduler`, APScheduler
  `BlockingScheduler(timezone=ET)`, six jobs. Every `CronTrigger` carries
  `timezone=ET` explicitly (APScheduler does *not* inherit the scheduler's tz
  into pre-built triggers). `intra_check` uses an `OrTrigger` built
  programmatically from `SESSION_WINDOWS`. `_run_safe` skips non-trading days,
  catches everything, and notifies in `finally`.
- **OS timer (the documented production path)** — a 30-minute timer invokes
  `scripts/run_if_et_window.sh <mode>`, which checks the **ET** wall clock
  against a window table pinned to `src/trading_calendar.SESSION_WINDOWS` by
  `tests/test_trading_calendar.py`, applies a per-mode once-per-ET-date
  last-run guard, and takes a cross-mode `mkdir`-mutex session lock
  (stale after 1800 s). `intra_check` is explicitly exempt from **both** the
  last-run guard and the session lock, because a stateless circuit breaker that
  can be starved by a long morning is pointless.

Windows: `earnings_preprocess` 08:00–09:15, `morning` 09:30–12:00,
`intra_check` 09:30–16:00, `midday` 13:00–14:30, `close` 15:30–16:00,
`evening` 20:00–22:00 (Mon–Fri ET). Each is ≥ the 30-minute tick interval.

**Timeout ladder:** 30 s Alpaca HTTP → 300 s LLM HTTP → 480 s LLM retry
deadline → `timeout --kill-after=30 1200` in the wrapper → `TimeoutStartSec`
in the systemd unit.

**Failure/restart:** `main.py` exits non-zero on `broker_error` / `fetch_error`
/ `analysis_error` so the wrapper does *not* write its last-run marker and the
next tick retries. `decision_checkpoint` lets a killed morning re-enter at
RiskStage with ~2 LLM calls instead of ~8, never skipping the preamble and
always re-running the RM. Telegram notification hooks live in `finally` blocks
in both `main.py` and `_run_safe`, and notifier failure can never fail a
session.

**Gap (D-4):** the repo ships only `scripts/systemd/quant-agent-daily.{service,timer}`.
The six per-session units — `quant-agent@.service` / `quant-agent@.timer`,
which `README.md:237` says "the repo ships" — are **not in the repository**.
The one shipped unit also hardcodes `WorkingDirectory=/home/yebo/quant-agent`.

---

## 8. Donor & AgentLens feasibility (bounded review)

### OpenTradex — inspected

`deonmenezes/opentradex` @ **`30b23f5ec3ad59ceecdd0335af2c5513c4137d36`**
(2026-07-07). **MIT**, © 2026 Deon Menezes. Dashboard stack is
React 18 + Vite 5 + Tailwind 3 + TypeScript 5 — an exact match for DECISION #13.

All five components named in `DONOR_COMPONENTS.md` exist:

| File | Lines | Non-React imports |
|---|---|---|
| `packages/dashboard/src/components/Resizable.tsx` | 51 | none (wraps `react-resizable-panels`) |
| `…/HarnessStatusBadges.tsx` | 181 | `type AgentContext` |
| `…/RunsAuditPanel.tsx` | 89 | `type SkillRun` |
| `…/AgentConsole.tsx` | 189 | `type SkillRun` |
| `…/FlowVisualizer.tsx` | 238 | `type Skill, SkillRun, InvokeResult` + `categoryStyle` |
| `…/ConfirmModal.tsx` | 112 | `type Skill` |
| `…/TopBar.tsx` | 414 | `type HarnessStatus, WsMeta, AgentContext` |
| `…/LeftSidebar.tsx` | 331 | `type Position, Trade, Market` |

Coupling is **type-only** in every case — adaptation means redefining a type,
not rewriting logic. `useHarness` is correctly identified for discard.

Two honest qualifications:
- `Resizable.tsx` is a 51-line wrapper over `react-resizable-panels`. Depending
  on the library directly is almost certainly cheaper than vendoring the donor.
- OpenTradex's domain vocabulary is **Skills / SkillRuns / Harness / agent
  chat**, i.e. a prediction-market agent harness — *not* a stock-portfolio
  cockpit. `AgentConsole` and `RunsAuditPanel` present *skill-run* orchestration,
  which is not what QAMC's `AgentCard` means (a per-analyst recommendation card).
  Only `LeftSidebar` touches `Position`/`Trade`/`Market`. The realistic donation
  is **visual language and layout primitives**, not the agent-semantics
  components. `UI_COMPONENT_MAP.md` currently over-promises here.

### TradingView Lightweight Charts — reachable, not inspected

`tradingview/lightweight-charts` resolves. No component-level inspection was
performed; it is a published library, and pulling it as a dependency (with its
attribution obligation) is the whole integration.

### Orallexa — **cannot be inspected: not identified in governed docs (D-2)**

`DONOR_COMPONENTS.md` names Orallexa as "primary multi-agent UI donor" and
`UI_COMPONENT_MAP.md` sources `AgentCard` and `DisagreementPanel` from it, but
**no repository, URL, license or commit is recorded anywhere in `docs/`.** A
web search surfaces a plausible candidate,
`alex-jb/orallexa-ai-trading-agent` (reachable), whose description matches the
concepts cited. **This is an inference, not a verified identification**, and
auditing an unconfirmed repository is exactly the unbounded donor work
`AGENTS.md` forbids. Requires operator confirmation before any Stage 3/4
inspection.

### AgentLens — **cannot be assessed: the name is ambiguous (D-3)**

`docs/architecture/AGENTLENS.md` makes specific commitments about "upstream
AgentLens" — pilot as-is, defer the fork, "low upstream maturity" — while
naming no repository. At least **three distinct** GitHub projects use the name
(`Soufianeazz/agentlens`, `agentkitai/agentlens`, `tranhoangtu-it/agentlens`),
plus an unrelated Salesforce product. They differ fundamentally (quality
scoring vs. tamper-evident audit log vs. tool-call tracing). The maturity
judgement in the architecture doc cannot be verified against any of them, and
Stage 6 is `OPTIONAL / BLOCKED` regardless. **Recommend deferring** until the
target is pinned by SHA.

---

## 9. Discrepancies (source vs. governed documentation)

| # | Severity | Discrepancy |
|---|---|---|
| **D-1** | **High** | **Actual model is never recorded.** All nine agent-log writes persist the configured model while cost is priced from the actual one, so a failover produces a silently-wrong, internally inconsistent record. Contradicts DECISION #12, `MODEL_PROVIDER_ARCHITECTURE.md` and `ACCEPTANCE_CRITERIA.md`. (§3.5 F-1/F-2.) |
| **D-2** | High | **Orallexa is unidentified.** Named as a primary donor and sourced for two UI components, with no repo/license/commit anywhere in `docs/`. Stage 0's "inspect donor code at current commits" is unsatisfiable for it. |
| **D-3** | High | **AgentLens is unidentified and the name is ambiguous** across ≥3 unrelated projects; `AGENTLENS.md`'s maturity claims are unverifiable. |
| **D-4** | Medium | **Runtime units are not in the repo.** `README.md:237` says the repo ships `quant-agent@.service`/`.timer`; only `quant-agent-daily.*` exists, and it hardcodes `/home/yebo/quant-agent`. DECISION #27 depends on this operational model. |
| **D-5** | Medium | **`alpaca.base_url` is dead configuration.** It is read nowhere in `src/`, `main.py` or `tests/` — paper vs. live is decided solely by `alpaca.paper` passed to `TradingClient`. `settings.yaml` presents two knobs where only one is live, against `ACCEPTANCE_CRITERIA.md` "paper/live configuration cannot be casually confused". (Current values agree and the environment is paper.) |
| **D-6** | Low | **No `upstream` remote configured**, contrary to `UPSTREAM_INTEGRATION.md`. Harmless now (fork is level with upstream) but the policy is unmet. |
| **D-7** | Low | **DECISION #9 wording vs. live config.** #9 says "Auto-Evolve disabled initially"; `settings.yaml` is `enabled: true, dry_run: true`, which the editor treats as STAGE-ONLY (proposals written, no prompt file touched). Behaviourally consistent with the decision, but `enabled: true` reads as "on". |
| **D-8** | Low | **`.env.example` is incomplete.** It omits `DEEPSEEK_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_CA_BUNDLE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `QUANT_AGENT_RETRY_DEADLINE_S`, `QUANT_AGENT_MAX_CONCURRENT_LLM`, all documented in `CLAUDE.md`/`README.md`. Stage 1 touches provider config and will trip over this. |
| **D-9** | Informational | **"Nothing bypasses the hard risk filter" needs one carve-out.** `cash_sweep` `SWEEP_BUY` reaches the broker outside `_filter_hard_risk_decisions`, deliberately and deterministically. `SAFETY_BOUNDARIES.md` should state it rather than leave it implicit. |
| **D-10** | Informational | `broker.close_position()` has no caller. |

---

## 10. Unresolved risks

1. **No runtime data was available.** `/data/` is gitignored and empty here, so
   nothing about actual failover frequency, real cost, or record quality could
   be measured. **D-1's practical severity is unknown** — it depends entirely on
   how often failover fires in production, which only the live DB can answer.
   A single query over the operator's real `agent_logs` would size it.
2. **Relay attribution ceiling (F-4).** Unfixable QAMC-side.
3. **tech_analyst chunking (F-3).** Per-invocation attribution needs either
   per-chunk log rows or an accepted known-imprecision.
4. **No external dead-man's switch.** Observability is push-on-completion plus
   evening's internal missing-session probe; a host outage or a failed evening
   is invisible. `CLAUDE.md` already flags this; QAMC inherits it.
5. **Donor identification (D-2/D-3)** blocks Stages 3/4/6 planning fidelity.
6. **Shallow clone** limits historical forensics without `--unshallow`.

---

## 11. Independent architectural assessment — **opinion, advisory only**

Everything above is verified source. Everything below is judgement, offered
because Stage 0 asked for it. None of it was implemented.

**What is genuinely sound.** The two-layer risk model is real, not
decorative — the deterministic gate runs *before* the AI risk manager and
*again* after any AI modification, and the fail-closed discipline in
`rules.py` (synthetic hard violations on non-finite inputs) is better than most
production trading code. The SELL protection lifecycle — cancel → submit →
verify → finalize-on-actual-fill → durably queue what couldn't be repaired —
is the strongest part of the codebase and is clearly the product of real
incidents. Choosing to keep this engine and build around it is the correct
call; QAMC's core premise is right.

**Risks I think the plan under-weights.**
*First*, the plan treats attribution as Stage 1 plumbing, but D-1 means every
`agent_logs` row written **before** the fix has unknown provenance on failover
days. The experiment's central question is model comparison, and the historical
record cannot be repaired retroactively. I'd treat the nine-line fix as urgent
independent of Stage 1's scope.
*Second*, `_execute()` is 130 lines where nearly every constant is a dated
production scar (480 s deadline, 300 s HTTP, 7 retries with jitter, 402
fast-fail, streaming-to-defeat-CF-524). A "minimal provider abstraction" that
touches it will regress something subtle that no test covers, because these
values were tuned against outages, not unit tests. The abstraction boundary
should be *below* `_execute()`, and I'd say so explicitly in the Stage 1 spec.
*Third*, `pipeline.py` is 7174 lines with ~90 methods. A read-only API is safe,
but Stage 8's writable controls will need to reach into it, and that is where
upstream mergeability will actually break — not in Stage 1.

**Unnecessary complexity.** Stage 6 (AgentLens) is the clearest candidate for
deletion rather than deferral: the target is unidentified, the name collides
three ways, and the system already persists full prompt and response text per
call plus a replay harness. The marginal forensic value over `agent_logs` +
`scripts/replay_decision.py` looks small. Stage 5's NL→structured-filter search
is also a large build over a dataset that will hold, at a guess, low tens of
thousands of rows; SQLite FTS5 plus a filter form covers it.

**Simplifications worth considering.** Fold the D-1 attribution fix into a
Stage 0.5 hotfix rather than Stage 1. Consider merging Stage 2 and Stage 3 —
freezing an API schema with no consumer tends to produce the wrong schema, and
building the cockpit against the read model will reshape it anyway. Defer the
provider abstraction until there is evidence a second provider is actually
wanted: the code already routes three providers by prefix, and the real gap is
recording, not routing.

**Documentation assumptions the source does not support.** `MODEL_PROVIDER_ARCHITECTURE.md`
says "quant-agent already has … a centralized base-agent provider-routing layer"
and implies attribution mostly exists — it does not (D-1/D-2 in §3.5).
`UI_COMPONENT_MAP.md` maps `AgentCard`/`DisagreementPanel` to Orallexa
"concepts" that no one has verified exist. `DONOR_COMPONENTS.md` expects
OpenTradex's agent components to transfer; the useful ones are the layout
primitives, and its `AgentConsole` solves a different problem.

**Where quant-agent already solves a QAMC problem.** Learning and memory
(DECISION #18 correctly recognises this). Correlation: `run_id` already spans
`agent_logs` and `trades`. Cost tracking: `cost_table.py` with LiteLLM refresh
and honest `None`-not-zero semantics is better than most bespoke attempts.
Proposed→executed delta: the `trades` table already carries HOLD audit rows,
`pending_submit`/`submitted`/`filled` status and fill quantities — the delta is
a query, not a feature. Prompt-change evaluation: `scripts/replay_decision.py`
exists and Stage 7 does not mention it.

**Disproportionate-maintenance candidates.** AgentLens (above). A second
`useHarness`-style data layer if donor components are imported with their type
vocabulary intact. Any writable operational control that has to model Alpaca's
real cancel/replace semantics — `SAFETY_BOUNDARIES.md` #10 already anticipates
this, and I'd read it as a reason to cap Stage 8 hard.

**What I would change before Stage 1.** Fix D-1. Pin or drop the unidentified
donors (D-2/D-3). Commit the six systemd units and de-hardcode the path (D-4).
Delete or wire `alpaca.base_url` (D-5). Add the missing `.env.example` keys
(D-8). Run one query against the operator's real `agent_logs` to size how often
failover actually fires — that number decides whether D-1 is a footnote or the
most important thing in this report.

---

## 12. Files changed during Stage 0

Documentation only. No source, test, config or script file was modified.

- `docs/STAGE0_BASELINE_AUDIT.md` (new — this file)
- `docs/MILESTONES.md` (Stage 0 status)
- `docs/knowledge/PROJECT_COMPASS.md` (current state)
- `docs/DECISIONS.md` (discrepancy reconciliation entries, per `DOCUMENTATION_GOVERNANCE.md`)
- `docs/DONOR_COMPONENTS.md` (exact files/commit for OpenTradex; unidentified-donor flags)
- `docs/UPSTREAM_INTEGRATION.md` (baseline verification result)

## 13. Checkpoint A acceptance

| Criterion | Status |
|---|---|
| Existing behavior unchanged | ✅ no non-doc file modified |
| Existing tests pass, or every pre-existing failure documented | ✅ 1431 passed, 0 failed, 0 skipped |
| Integration-seam report completed | ✅ §3.6, §6, §7 |
| Donor inventory completed | ⚠️ **partial** — OpenTradex inventoried at an exact commit; Orallexa and AgentLens are unidentified in governed documentation (D-2, D-3) and cannot be inspected without operator confirmation |
| No feature code added | ✅ |

**Stage 0 is substantively complete with one bounded exception**: the donor
inventory cannot be finished for two of four donors until the operator names
the repositories. STOP for human review.
