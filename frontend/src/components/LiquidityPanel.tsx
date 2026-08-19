import { AccountResponse, PositionItem } from "../api/client";
import { fmtMoney } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";

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
  const hedgeMv = positions
    .filter((p) => p.direction === "bearish_hedge")
    .reduce((s, p) => s + (p.market_value || 0), 0);

  return (
    <Panel title="Cash & risk exposure" status="ok">
      {liq ? (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-2">
            <Figure label="Raw cash" value={fmtMoney(liq.raw_cash)} />
            <Figure
              label={`Sweep parked${liq.sweep_symbol ? ` (${liq.sweep_symbol})` : ""}`}
              value={liq.sweep_enabled ? fmtMoney(liq.sweep_parked_value) : "disabled"}
            />
            <Figure label="Reserve floor" value={fmtMoney(liq.reserve_usd)} />
            <Figure label="Deployable cash" value={fmtMoney(liq.deployable_cash)} />
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
      <div className="grid grid-cols-2 gap-3 mt-3">
        <Figure label="Long exposure" value={fmtMoney(longMv)} />
        <Figure label="Bearish-hedge exposure" value={hedgeMv > 0 ? fmtMoney(hedgeMv) : "none"} />
      </div>
    </Panel>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[0.68rem] text-dim uppercase tracking-wide">{label}</div>
      <div className="text-[1.05rem] font-bold tabular-nums">{value}</div>
    </div>
  );
}
