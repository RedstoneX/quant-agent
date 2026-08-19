import { AccountResponse, PositionItem } from "../api/client";
import { fmtMoney, fmtMoneyCompact } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { DonutMeter } from "./ui/DonutMeter";

export function LiquidityPanel({
  account,
  accountError,
  positions,
}: {
  account: AccountResponse | null;
  accountError?: string | null;
  positions: PositionItem[];
}) {
  if (!account) {
    return (
      <Panel title="Cash & risk exposure" status={accountError ? "error" : "loading"}>
        <StateMessage text={accountError ? `Account read failed: ${accountError}` : "Loading…"} error={Boolean(accountError)} />
      </Panel>
    );
  }
  const liq = account.liquidity;
  const longMv = positions.filter((p) => p.direction === "long").reduce((s, p) => s + (p.market_value || 0), 0);
  const hedgeMv = positions.filter((p) => p.direction === "bearish_hedge").reduce((s, p) => s + (p.market_value || 0), 0);
  const cashEquivMv = positions.filter((p) => p.is_cash_equivalent).reduce((s, p) => s + (p.market_value || 0), 0);

  // "held back" = the slice of raw cash the reserve floor keeps out of
  // deployable_cash — derived as raw_cash minus deployable_cash so the bar
  // can never disagree with deployable_cash's own math, rather than
  // rendering reserve_usd (a target floor) as if it were guaranteed to be
  // fully funded by cash alone.
  const heldBack = liq && liq.raw_cash !== null && liq.deployable_cash !== null ? Math.max(liq.raw_cash - liq.deployable_cash, 0) : 0;

  return (
    <Panel title="Cash & risk exposure" status={accountError ? "stale" : "ok"}>
      {accountError && (
        <div className="text-warn text-[0.72rem] bg-warn/10 border border-warn/30 rounded-md px-2 py-1.5 mb-2.5">
          Showing last known account data — a fresh fetch failed: {accountError}
        </div>
      )}
      {liq ? (
        <>
          <div className="mb-1">
            <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1.5">Liquidity — where the cash is</div>
            <DonutMeter
              formatValue={fmtMoneyCompact}
              centerLabel="Total liquidity"
              centerValue={fmtMoneyCompact(liq.total_liquidity ?? (liq.raw_cash ?? 0) + (liq.sweep_parked_value ?? 0))}
              segments={[
                { label: "Deployable cash", value: liq.deployable_cash ?? 0, tone: "pos" },
                { label: "Reserve (held back)", value: heldBack, tone: "warn" },
                {
                  label: `Sweep parked${liq.sweep_symbol ? ` (${liq.sweep_symbol})` : ""}`,
                  value: liq.sweep_enabled ? liq.sweep_parked_value ?? 0 : 0,
                  tone: "dim",
                },
              ]}
            />
          </div>
          {liq.sweep_enabled && (
            <div className="state-message">
              {liq.sweep_symbol} is deterministic cash-equivalent sweep parking, not a Portfolio Manager
              investment thesis — excluded from risk exposure below.
            </div>
          )}
        </>
      ) : (
        <StateMessage text="Liquidity breakdown unavailable." />
      )}

      <div className="mt-3.5">
        <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1.5">Positions — real risk exposure</div>
        {longMv + hedgeMv + cashEquivMv > 0 ? (
          <DonutMeter
            formatValue={fmtMoneyCompact}
            centerLabel="Positions"
            centerValue={fmtMoneyCompact(longMv + hedgeMv + cashEquivMv)}
            segments={[
              { label: "Long", value: longMv, tone: "pos" },
              { label: "Bearish hedge", value: hedgeMv, tone: "hedge" },
              { label: "Cash-equivalent", value: cashEquivMv, tone: "dim" },
            ]}
          />
        ) : (
          <StateMessage text="No open positions to visualize." />
        )}
      </div>
    </Panel>
  );
}
