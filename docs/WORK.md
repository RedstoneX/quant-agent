# QAMC Current Work

Status: **ALPACA PAPER SOAK ACTIVE — DIRECTIONALITY + MISSION CONTROL EXPLAINABILITY TRANCHE AUTHORIZED**

## Goal

Use actual paper-soak evidence from the Aug 17–18 market decline to determine why QAMC deployed no meaningful risk, verify that the existing bearish/inverse-ETF path is genuinely usable, and make Mission Control explain the answer without requiring raw-log archaeology.

This is evidence-driven post-start iteration. It is no longer speculative dashboard polish: the operator has observed both a significant market move and a dashboard that does not clearly explain why candidates produced no trade.

## Product decision now explicit

QAMC is **not intended to be structurally long-only**. Within the currently supported instruments and existing safety architecture, it should be able to express bullish, bearish or neutral/cash views.

Current approved bearish expression is through the inverse ETFs already in the universe (`SH`, `SDS`, `PSQ`, `SQQQ`). This tranche does **not** authorize direct stock shorting, options/theta strategies, margin, or a deterministic risk/execution redesign.

SGOV is also explicitly **cash-equivalent sweep parking**, not a PM investment decision. It must not be presented to the operator as if the AI chose to allocate the portfolio defensively to bonds.

## Workstream A — forensic directionality audit

Reconstruct the relevant Aug 17–18 scheduled paper sessions from authoritative runtime/database evidence. For each meaningful morning run, follow the actual chain:

1. market/regime evidence, including SPY/QQQ context;
2. Tech ratings for `SPY`, `QQQ`, `SH`, `SDS`, `PSQ`, `SQQQ` where available;
3. Macro and News stance;
4. candidates that reached the Portfolio Manager;
5. PM targets/reasoning and whether inverse ETFs were considered;
6. AI Risk Manager verdict/modifications;
7. deterministic Python blocks, if any;
8. order/execution result;
9. evening missed-opportunity assessment.

The audit must distinguish among at least these possibilities rather than assuming “no trade” is wrong:

- no technically qualified bearish setup;
- bearish signals existed but never reached candidate/PM consideration;
- PM recognized the decline but intentionally stayed neutral;
- inherited long-participation priors or prompt framing created directional bias;
- RM vetoed/scaled the plan;
- deterministic risk blocked it;
- data/runtime failure suppressed an otherwise valid decision.

Do not change intelligence because the move is obvious in hindsight. A behavioural correction requires evidence that the supported bearish path was systematically ignored, contradicted or inaccessible.

## Workstream B — Mission Control explainability

Improve the existing read-only cockpit using existing authoritative sources/derived read-only aggregations. Keep the API/UI non-critical to trading.

### Required UX outcomes

1. **Liquidity is honest at a glance.** Distinguish:
   - raw broker cash;
   - SGOV cash-sweep parking;
   - deployable liquidity;
   - real risk-asset exposure / directional exposure.

   SGOV may still appear in a detailed positions view for broker truth, but it must be visibly labeled cash-equivalent/sweep rather than an AI-selected risk position.

2. **Latest run answers “WHY NO TRADE?”** The main page must provide a compact decision funnel for the most recent substantive trading run, using recorded evidence where available. At minimum show:
   - candidates considered;
   - PM targets/proposed actions;
   - RM approvals/rejections/modifications;
   - deterministic blocks;
   - executed trades;
   - a concise dominant no-trade reason when the funnel ends at zero execution.

   The existing detailed run/candidate modal remains the forensic drill-down; the new top-level summary is an index into it, not a second source of truth.

3. **Directional posture is visible.** Surface the latest available market/regime stance and whether bearish/inverse-ETF opportunities were actually considered. Do not fabricate a market regime when no authoritative run evidence exists.

4. **Missed opportunities are visible.** Journal/evening data should make notable missed UP and DOWN moves easy to see, including symbol, move direction/magnitude and classification when recorded.

5. **Existing detail remains accessible.** PM reasoning, RM reasoning, specialist disagreement, provider/model/cost/latency and proposed-vs-executed evidence must remain available through the existing read-only drill-down.

## Workstream C — conditional intelligence correction

Only after Workstream A establishes a real blind spot, make the smallest correction inside the existing accepted architecture.

Allowed examples:

- prompt wording that requires explicit inverse-ETF consideration when current Macro/Tech evidence is materially bearish;
- candidate-selection plumbing that accidentally excludes already-approved inverse ETFs;
- clearer separation of “swing/position horizon” from “must be long” so multi-day bearish opportunities can be expressed;
- removal or rebalance of an inherited prior if current-account evidence shows it is distorting decisions.

Not allowed in this tranche:

- direct short positions or negative target weights;
- options/theta infrastructure;
- margin enablement;
- new broker/execution semantics;
- weakening stop/cash/daily-loss/position/sector protections;
- changing models simply because a decision was conservative;
- new authoritative UI storage or a second trading-memory system.

Any intelligence change must include a regression fixture showing the prior failure mode and the corrected behaviour without weakening deterministic safety.

## Verification / acceptance

Before this tranche can be accepted:

- forensic findings must state **finding → evidence → decision → change (if any) → remaining uncertainty**;
- the full automated test suite must pass;
- dashboard changes must be browser/runtime verified against representative real or sanitized soak data, with screenshots under the existing verification convention;
- SGOV must be visibly differentiated from risk positions;
- a real no-trade run must be explainable from the main page and traceable into the existing run/candidate detail;
- no deterministic risk/execution semantics may change;
- Alpaca remains Paper-only;
- no secrets may appear in Git, UI evidence, screenshots or logs;
- paper-soak timers remain active unless a genuine runtime defect requires a bounded operational stop.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign.
- No direct shorting, options trading or margin in this tranche.
- No broker-write Mission Control controls.
- No public services; Mission Control remains tailnet/private.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Keep upstream OneCLI as the credential layer.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.

## Handoff

Begin with the runtime/database forensic reconstruction, while the non-behavioural Mission Control presentation fixes may proceed in parallel because their defects are already established. Stop and reconcile only if the evidence indicates a material architecture change beyond the allowed inverse-ETF/prompt/candidate path.
