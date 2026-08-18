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

Commissioning is complete and accepted, all seven paper timers remain armed, and private operator access is now available through Tailscale under `redstone-vps`.

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
| ✅ DONE | Private operator access | Tailscale/Orca path recorded; `redstone-vps` serves private Mission Control. |
| ✅ ACTIVE | Scheduled Alpaca Paper soak | Autonomous paper schedule remains armed. |
| 🟨 NOW | Directionality forensic audit | Reconstruct Aug 17–18 bearish opportunities through Tech → PM → RM → deterministic gate → execution. |
| 🟨 NOW | Mission Control explainability | Separate SGOV sweep from risk exposure; add decision funnel / “why no trade?”; surface directional posture and missed opportunities. |
| ⬜ CONDITIONAL | Intelligence correction | Only if the forensic evidence proves the supported bearish path is being ignored or suppressed. |

## 📊 Mission Control

The cockpit is functionally rich but currently too much like a database viewer. The important evidence already exists underneath, but the operator should not need multiple drill-downs to answer basic questions.

The next UI tranche is therefore about **explanation, not decoration**:

- raw cash vs SGOV sweep vs deployable liquidity vs risk exposure;
- latest-run candidate → PM → RM → deterministic gate → execution funnel;
- concise “why no trade?”;
- latest available directional/regime posture;
- prominent missed opportunities in both directions.

The existing detailed run/candidate forensic views remain the source for deep inspection.

## 🖥️ DEPLOYMENT

- OVH VPS: Ubuntu 24.04
- Runtime account: `qamc`
- Development account: `dev`
- Administration account: `ubuntu`
- Private tailnet hostname: `redstone-vps`
- QAMC and OneCLI remain private.

## ⏭️ NEXT MOVES

1. Reconstruct the Aug 17–18 no-trade/bearish opportunity chain from runtime/database evidence.
2. Implement the already-justified read-only Mission Control explainability fixes.
3. Change PM/candidate intelligence only if the forensic evidence proves a real directional blind spot; keep deterministic risk/execution unchanged.

## 🚧 CURRENT BLOCKERS

No architecture blocker. The only evidence gap is that GitHub alone cannot prove the exact Aug 17–18 live runtime decisions; that part must be read from the VPS/database. The paper soak should continue while that evidence is collected.

_Last refreshed: 2026-08-18 EDT (America/Toronto) — active project view only; retired detail lives in Git history._
