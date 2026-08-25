# 🧭 QAMC Project Compass

> [!note] 👀 **Human dashboard — for me, not the agents**
> Fast plain-English project view. Machine authority is `OUTCOME.md` + `STATE.md` + `WORK.md` + relevant accepted architecture contracts.

## 🎯 What QAMC is

**QAMC is an autonomous AI-assisted Alpaca paper-trading experiment that acts like a small virtual trading desk.**

Specialist AI agents analyze the market, a Portfolio Manager synthesizes the evidence, an AI Risk Manager challenges the plan, and deterministic Python safety rules decide what is actually allowed to execute.

The experiment asks:

> **Does inexpensive modern AI add measurable out-of-sample trading value beyond ordinary deterministic signals?**

QAMC is not meant to require a rising market to have an opportunity set. Within the current architecture it can express bearish views through approved inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`). Direct stock shorting, options and margin are not currently part of QAMC.

Live-money trading is **not authorized**.

## 🚦 RIGHT NOW

### ▶️ Alpaca Paper soak is ACTIVE — Mission Control production-converged

Production runs the accepted trading-utility recovery, enriched Telegram output, and the full professional Mission Control cockpit (Tremor/TanStack UI, Lightweight Charts, decision-chain visualization, session-execution chart context) — live at `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/`. All seven paper timers remain armed.

A production-only defect surfaced this session: the chart's intraday timeframe controls (5m/15m/1h) were visible and clickable, but every symbol came back with zero bars. Traced to `src/execution/broker.py`'s intraday bar request never setting Alpaca's `feed` parameter — this account is IEX-entitled, not SIP, and the unset default was resolving to SIP for sub-daily bars only (daily bars aren't feed-gated the same way, which is why `1D` worked fine). Fixed with a 2-file, ~25-line change; 2031 backend + 55 frontend tests pass; pushed for ChatGPT/operator review — **not yet deployed to production**, and not live-verifiable from `dev` (no Alpaca credentials in this account boundary).

A temporary, Tailscale-only Vite hot-reload DEV preview is now a standing authorized workflow (separate from `branch_preview.py`'s static-build preview) for fast visual iteration on Dashboard work.

---

## 🗺️ PROJECT MAP

| Status | Stage / milestone | Result |
|---|---|---|
| ✅ DONE | 0–2 | Trading-engine audit, provider/model plumbing, isolated read-only Mission Control API. |
| ✅ DONE | 3 | Browser/iPad Trading Cockpit. |
| ✅ DONE | 4–5 | Specialist evidence, decision chain, journal and forensic search. |
| ✅ DONE | VPS deployment / hardening | Runtime deployed and separated from development. |
| ✅ DONE | OneCLI commissioning | Private credential gateway; Alpaca Paper/OpenRouter/FRED path verified. |
| ✅ DONE | Model routing | 8 seats on Gemini 2.5 Flash Lite; Risk Manager on Qwen3 235B via OpenRouter. |
| ✅ DONE | Decision-chain audit | PM/RM evidence flow and auditability reviewed without changing deterministic safety semantics. |
| ✅ DONE | Runtime commissioning | 37 PASS / 0 FAIL / EXIT 0, combined with prior green dev isolation evidence. |
| ✅ DONE | Private operator access | Tailscale/Orca path recorded; explicit VPS FQDN is `ovh-vps.wallaby-bowfin.ts.net`. |
| ✅ ACTIVE | Scheduled Alpaca Paper soak | Autonomous paper schedule remains armed. |
| ✅ DONE | Trading-utility recovery (PR #56) | Deployed to production, accepted as machinery. |
| ✅ DONE | Mission Control professional cockpit (PR #60) | Tremor/TanStack cockpit, data-truth/run-history/explainability — merged and deployed. |
| ✅ DONE | Session executions + intraday chart (PR #63) | Deployed to production; added the 5m/15m/1h/1D chart timeframe controls. |
| 🟡 NOW | Chart-timeframe data-path fix | Intraday bars (5m/15m/1h) returned empty for every symbol — missing Alpaca `feed` parameter. Fixed and pushed, awaiting external review. |

## 📊 Mission Control

The cockpit is functionally rich but currently too much like a database viewer. The important evidence already exists underneath, but the operator should not need multiple drill-downs to answer basic questions.

The active product direction is a substantially more coherent trading cockpit: honest liquidity/risk presentation, candidate/watchlist context, directional posture, a prominent decision chain, concise “why no trade?”, visible missed opportunities and progressive disclosure of model/cost/raw technical detail.

The prior Mission Control visual board is being made a durable repository reference so Claude can inspect the image directly, not just a textual summary.

## 🖥️ DEPLOYMENT

- OVH VPS: Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`
- Administration account: `ubuntu`
- Tailnet DNS name: `wallaby-bowfin.ts.net`
- Verified OVH VPS Tailscale machine name: `ovh-vps`
- **Canonical SSH / explicit MagicDNS host: `ovh-vps.wallaby-bowfin.ts.net`**
- `redstone-vps` is obsolete for current Tailscale access.
- QAMC and OneCLI remain private.

## ⏭️ NEXT MOVES

1. ChatGPT/operator review of the pushed chart-timeframe data-path fix branch; Claude does not merge its own work.
2. After merge and a governed deploy, confirm live that AAPL/SPY/etc. 5m/15m/1h now return real bars — this could not be verified from `dev` (no Alpaca credentials in this account boundary).
3. Continue natural Alpaca Paper validation of the full opportunity → decision → execution → management chain; do not force activity for the dashboard fix.

## 🚧 CURRENT BLOCKERS

No architecture blocker. The chart-timeframe fix is pushed and awaiting ChatGPT/operator review before it can be deployed and live-verified; the paper soak continues unaffected in the meantime.

_Last refreshed: 2026-08-24 — active project view only; retired detail lives in Git history._
