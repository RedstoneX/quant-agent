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

### ▶️ Alpaca Paper soak is ACTIVE

Commissioning is complete and accepted, all seven paper timers remain armed, and private operator access is available through Tailscale. The tailnet DNS name is **`wallaby-bowfin.ts.net`** and the verified OVH VPS Tailscale machine is **`ovh-vps`**, giving the explicit MagicDNS FQDN **`ovh-vps.wallaby-bowfin.ts.net`**.

The trading-utility recovery (7 fixes: PM parse destruction, SGOV funding race, schema-complete decisions, invisible execution kills, unfunded-BUY loss, macro conservatism, sizing-cap provenance) is deployed to production and verified as machinery. It is not yet accepted as a working recovery — that needs natural Alpaca Paper sessions demonstrating the real discovery → decision → execution → exit chain, not forced trades.

### 🧭 Mission Control correctness tranche — implemented, awaiting external review

A bounded, read-side-only fix for six confirmed dashboard-truth defects (pricing/chart data truth, Cockpit run-history silently replacing what an operator was reviewing, Journal decision-ledger legibility, "why wasn't it purchased?" explainability, a real frontend/backend contract gap on execution-skip reasons, and Journal date discoverability) is implemented on `claude/mission-control-data-correctness`, backed by new backend + frontend tests, and pushed. **Awaiting ChatGPT/operator review and merge — Claude does not self-accept this.**

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
| ✅ DONE | Trading-utility recovery | 7 evidenced fixes deployed and verified as machinery; natural-validation evidence still pending. |
| 🟨 NOW | Mission Control data-truth tranche | Implemented + tested on `claude/mission-control-data-correctness`; pushed, awaiting external review/merge. |

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

1. ChatGPT/operator review the `claude/mission-control-data-correctness` diff and evidence, then merge if accepted — Claude does not merge its own work.
2. Continue observing natural Alpaca Paper sessions against the trading-utility recovery's validation criteria; do not force trades to manufacture evidence.
3. Preserve and commit the Mission Control visual board at `docs/visual/MISSION_CONTROL_VISION_BOARD.png` for future redesign reference.

## 🚧 CURRENT BLOCKERS

No architecture blocker. The Mission Control data-truth tranche is implemented, tested and pushed but not yet externally reviewed or merged — that review is the current gate, not a Claude-side blocker. Browser-rendered visual QA for that tranche could not be performed in this sandbox (the browser tool runs on infrastructure separate from this VPS and cannot reach a locally-bound dev server); verified instead via direct API calls against a seeded fixture DB, plus full backend/frontend test suites and a production build. The paper soak continues unaffected.

_Last refreshed: 2026-08-21 EDT (America/Toronto) — active project view only; retired detail lives in Git history._
