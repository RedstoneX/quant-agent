import { AccountResponse, PositionItem } from "../api/client";
import { fmtMoney } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { SegmentedBar } from "./ui/Meter";

export function LiquidityPanel({
  account,
  positions,
}: {
  account: AccountResponse | null;
  positions: PositionItem[];
}) {
  if (!account) return <Panel title="Cash & risk exposure" status="loading"><StateMessage text="Loading…" /></Panel>;
  if (account.error) {
    return (
      <Panel title="Cash & risk exposure" status="degraded">
        <StateMessage text={`Account read failed: ${account.error}`} error />
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
    <Panel title="Cash & risk exposure" status="ok">
      {liq ? (
        <>
          <div className="mb-1">
            <div className="text-[0.68rem] text-dim uppercase tracking-wide mb-1.5">Liquidity — where the cash is</div>
            <SegmentedBar
              formatValue={fmtMoney}
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
          <SegmentedBar
            formatValue={fmtMoney}
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
