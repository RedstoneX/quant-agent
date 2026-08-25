import { Card, Grid, Metric, Text } from "@tremor/react";
import { AccountResponse, PositionItem } from "../api/client";
import { fmtMoneyCompact } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";

function Kpi({ label, value, note }: { label: string; value: number | null; note?: string }) {
  return (
    <Card className="!bg-panel-alt !p-3 !ring-border">
      <Text className="uppercase tracking-wide">{label}</Text>
      <Metric className="mt-1 font-mono text-xl text-ink">{fmtMoneyCompact(value)}</Metric>
      {note && <Text className="mt-1 text-xs leading-snug">{note}</Text>}
    </Card>
  );
}

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
      <Panel title="Liquidity & directional risk" status={accountError ? "error" : "loading"}>
        <StateMessage text={accountError ? `Account read failed: ${accountError}` : "Loading…"} error={Boolean(accountError)} />
      </Panel>
    );
  }

  const liq = account.liquidity;
  const directionalExposure = positions
    .filter((position) => !position.is_cash_equivalent)
    .reduce((total, position) => total + Math.abs(position.market_value || 0), 0);

  return (
    <Panel
      title="Liquidity & directional risk"
      subtitle="SGOV is cash parking. It is excluded from directional exposure and investment P&L."
      status={accountError ? "stale" : "ok"}
    >
      {accountError && (
        <div className="mb-3 rounded-md border border-warn/30 bg-warn/10 px-2 py-1.5 text-xs text-warn">
          Showing last known account data — fresh fetch failed: {accountError}
        </div>
      )}
      {!liq ? (
        <StateMessage text="Liquidity breakdown unavailable." />
      ) : (
        <Grid numItems={2} numItemsSm={3} className="gap-2.5">
          <Kpi label="Total liquidity" value={liq.total_liquidity} note="Raw cash plus parked cash equivalent" />
          <Kpi label="Raw cash" value={liq.raw_cash} />
          <Kpi
            label={`${liq.sweep_symbol || "SGOV"} parked`}
            value={liq.sweep_enabled ? liq.sweep_parked_value : 0}
            note="Deterministic cash parking"
          />
          <Kpi label="Deployable now" value={liq.deployable_cash} note="Immediately available after reserve" />
          <Kpi label="Reserve" value={liq.reserve_usd} note="Held outside deployable cash" />
          <Kpi label="Directional risk" value={directionalExposure} note="Long + bearish hedge; SGOV excluded" />
        </Grid>
      )}
    </Panel>
  );
}
