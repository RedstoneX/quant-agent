## item 183 — detail moved from the board 2026-09-30

Item 90's half two, surfaced for visibility. Whether an order fills, is skipped, or trades at all is decided by flat unsourced constants: the 40bps entry-slippage belt (`ExecutionConfig.max_entry_slippage_bps`), the $500 constructor minimum-order floor (`ConstructorConfig.min_order_usd` — DELETED 2026-09-26, see below), the 0.5% minimum weight change before the desk bothers to trade (`ConstructorConfig.min_trade_weight_delta` — DELETED 2026-09-30, see below), the entry-skip when the ask sits more than 2% above the slippage cap (`ExecutionStage._run_session` — DELETED 2026-09-30, see below), and the 1% cash-reserve band (`CashSweepConfig.reserve_pct` — still live via the deployment-gap advisory even though the sweep itself is retired). All `status: arbitrary`, none read off a spread or a measurement. Distinct from item 138, which tracks the order-PRICE buffers (the 1% / 0.5% / 3% offsets), not these gates.


## item 183 — RETIRED 2026-09-30, all five order-placement gates resolved: the constructor $500 floor and the 0.5% weight-delta floor deleted, the 2% ask-skip deleted with its SHORT mirror, the 40bp entry-slippage belt ratified as owner appetite inside a measured indifference band, and the 1% cash-reserve band carried to item 190
2026-09-30. RATIFIED as owner appetite, not sourced and not changed: the 40bp belt
        (`ExecutionConfig.max_entry_slippage_bps`) is the last of this item's five gates and it is a dial. The
        2026-09-26 measurement leaves an indifference band of roughly 32bp to 390bp — the belt censors its own
        tail, every recorded slippage refusal sat 391-1466bp out, and no published reference for an acceptable
        entry-slippage bound on retail marketable limits exists — so every value in that band would have
        decided every observed case identically and the data cannot pick one. No replacement number was
        invented, because choosing again inside a measured indifference band is the same arbitrary act with a
        newer date. The ledger row now records the ratification and its reason. The two successor routes are
        NOT closed by this and are deliberately left as named routes rather than as an open criterion here:
        (a) reformulate the ceiling onto each name's own Corwin & Schultz half-spread, blocked until the
        reference-to-submission drift term the belt also absorbs has its own instrument-read basis (it was
        raised 25 to 40 in 2026-08 for exactly that drift); (b) re-measure the untruncated fill rate, newly
        possible because the deleted 2% ask-skip lets a too-tight entry rest and be recorded. Both belong to
        item 90's half-two re-derivation, not to a gate inventory.


