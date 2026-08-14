# QAMC Current Work

Status: **ALPACA PAPER SOAK ACTIVE — OBSERVE AND EVALUATE**

## Goal

Observe scheduled **Alpaca Paper** operation and use actual trading evidence to decide what deserves improvement next. Do not insert speculative intelligence/model/dashboard work ahead of evidence from the running soak unless operation exposes a genuine defect.

## Commissioning is closed

The final `qamc` live commissioning run against current `main` passed on 2026-08-14:

- **37 passed / 0 failed / 0 warned / 1 skipped**
- `COMMISSIONING ACCEPTANCE: PASS`
- `EXIT=0`

The single SKIP is the intentionally `dev`-only off-account credential-isolation check. The earlier green `dev` run already proves that boundary. Full commissioning is accepted by union of the two account runs.

## Paper soak activation complete

On 2026-08-14 the operator enabled all seven existing `qamc` user timers. Systemd confirmed all seven `enabled` and scheduled:

- six trading-stage timers check every 30 minutes and self-gate to their intended ET windows;
- their first scheduled post-activation tick was **18:30 UTC / 14:30 ET** on 2026-08-14;
- `quant-agent-daily.timer` is the P&L CSV export and is scheduled **Mon–Fri 09:00 America/New_York**.

This is the authorized Alpaca **Paper** soak. Live trading remains prohibited.

## Immediate work

Observe and validate the first real paper sessions through the existing runtime and Mission Control. Prioritize evidence about:

1. positions selected, rejected or missed;
2. specialist conclusions and disagreement;
3. Portfolio Manager synthesis;
4. AI Risk Manager changes/vetoes;
5. deterministic Python blocks and sizing;
6. orders, fills and exits;
7. model/provider attribution, latency and cost;
8. P&L/performance and missed opportunities;
9. runtime failures or recovery behaviour;
10. Mission Control usability during actual operation.

Do not interpret "no trade" as a defect by itself; inspect the recorded candidate/decision/risk evidence first.

## Deferred until evidence justifies it

Unless the running soak exposes a blocker, do **not** prioritize:

- more model benchmarking;
- more agent/prompt intelligence work;
- additional decision-chain redesign;
- richer dashboard charts or visual polish;
- speculative feature expansion.

These are valid post-start improvements, but their priority should come from observed soak evidence.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign without a new contract.
- No broker-write Mission Control controls.
- No public services.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Keep upstream OneCLI as the credential layer.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.