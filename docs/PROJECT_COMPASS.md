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

The first real soak review has already produced useful evidence:

- the dashboard makes deterministic SGOV cash parking look like an AI-selected investment;
- a real morning run considered candidates but the top-level cockpit did not explain why no trade resulted;
- the recent market decline creates a useful test of whether the existing inverse-ETF bearish path is actually being considered rather than merely existing in code.

That evidence is enough to authorize the next tranche without stopping the soak.

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
| 🟨 NOW | Autonomous product improvement | Directionality evidence, cockpit redesign, explainability and justified intelligence fixes. |

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

1. Preserve and commit the Mission Control visual board at `docs/visual/MISSION_CONTROL_VISION_BOARD.png`, then use it as an explicit redesign reference.
2. Execute the authorized autonomous product-improvement tranche through a meaningful implementation checkpoint, not another audit-only stop.
3. Push the implementation branch for independent ChatGPT review; Claude does not merge its own work.

## 🚧 CURRENT BLOCKERS

No architecture blocker. Runtime/database evidence and the newly staged visual board are available to the VPS-side development workflow; the paper soak should continue while improvements are built and verified.

_Last refreshed: 2026-08-18 EDT (America/Toronto) — active project view only; retired detail lives in Git history._
