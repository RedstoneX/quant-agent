# QAMC UI Vision

## UX objective
A dense but readable “AI Trading Mission Control” optimized for desktop and iPad. The operator should understand account state, candidate analysis and the PM→Risk→execution chain without reading logs.

## Primary cockpit wireframe
```text
TOP: PAPER MODE | Equity | Day P&L | Buying Power | Market | Health | Pause/Kill (later)

LEFT                  CENTER                         RIGHT
Watchlist/Candidates  TradingView chart             Agent summaries
                      candles/volume/trade markers   PM proposal
                                                     AI Risk response
                                                     Deterministic result
                                                     Executed delta

BOTTOM LEFT           BOTTOM CENTER                  BOTTOM RIGHT
Portfolio/positions   Orders/Trades                  Activity/Journal feed
```

## Responsive behavior
Desktop: full multi-pane cockpit.
iPad landscape: retain chart + candidate + decision chain, allow lower panels to tab/collapse.
iPad portrait: stacked cards/tabs; never shrink critical text into unusability.

## Design principles
- truth before decoration;
- paper mode unmistakable;
- proposed versus executed visually distinct;
- agent disagreement easy to spot;
- costs/models visible but secondary to decisions;
- raw traces one click deeper, not dumped into daily journal;
- no fake live data;
- write controls visually and technically separate from read-only monitoring.

## Donor guidance
Use OpenTradex visual language/component primitives where cheap. Use Orallexa AI cards/decision semantics where useful. Do not imitate either donor so closely that QAMC inherits their backend architecture.
