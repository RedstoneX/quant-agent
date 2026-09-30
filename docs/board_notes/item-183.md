## item 183 — detail moved from the board 2026-09-30

Item 90's half two, surfaced for visibility. Whether an order fills, is skipped, or trades at all is decided by flat unsourced constants: the 40bps entry-slippage belt (`ExecutionConfig.max_entry_slippage_bps`), the $500 constructor minimum-order floor (`ConstructorConfig.min_order_usd` — DELETED 2026-09-26, see below), the 0.5% minimum weight change before the desk bothers to trade (`ConstructorConfig.min_trade_weight_delta` — DELETED 2026-09-30, see below), the entry-skip when the ask sits more than 2% above the slippage cap (`ExecutionStage._run_session` — DELETED 2026-09-30, see below), and the 1% cash-reserve band (`CashSweepConfig.reserve_pct` — still live via the deployment-gap advisory even though the sweep itself is retired). All `status: arbitrary`, none read off a spread or a measurement. Distinct from item 138, which tracks the order-PRICE buffers (the 1% / 0.5% / 3% offsets), not these gates.


