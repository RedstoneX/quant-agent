import { Badge, Text } from "@tremor/react";
import { AccountResponse, PositionItem } from "../api/client";
import { fmtMoneyCompact } from "../lib/format";

/* The "Total" tile is gone: raw cash + parked IS deployable cash, so it
 * was the same number under a second name — the miniature of the defect
 * that made "Deployable" show max(cash - reserve, 0) while the engine
 * sized against cash + parked, 1.58x apart on the same book.
 *
 * Item 9 (cockpit trader rework): this used to be six stat tiles (total
 * liquidity, raw cash, SGOV parked, deployable, reserve, directional
 * risk) — several of them just sums of the others, occupying a full panel
 * of prime real estate for numbers a trader reads once and never again.
 * Condensed to the single compact row it actually needs to be, and moved
 * out of the workspace tab strip into the header's secondary chrome
 * (App.tsx, directly under HeroBand) alongside the other portfolio
 * abstractions item 6 demotes. Same read-only account.liquidity data as
 * before, no new fetch. */

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <span className="flex items-baseline gap-1.5" title={note}>
      <span className="label-xs">{label}</span>
      <span className="font-mono text-[length:var(--fs-meta)] font-semibold tabular-nums text-ink">{value}</span>
    </span>
  );
}

export function LiquidityStrip({
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
      <div className="mx-3 mt-1.5 text-[length:var(--fs-meta)] text-dim">
        {accountError ? `Liquidity unavailable: ${accountError}` : "Loading liquidity…"}
      </div>
    );
  }

  const liq = account.liquidity;
  const directionalExposure = positions
    .filter((position) => !position.is_cash_equivalent)
    .reduce((total, position) => total + Math.abs(position.market_value || 0), 0);

  if (!liq) {
    return <div className="mx-3 mt-1.5 text-[length:var(--fs-meta)] text-dim">Liquidity breakdown unavailable.</div>;
  }

  return (
    <div
      className="mx-3 mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-panel-alt px-3 py-1.5"
      aria-label="Liquidity"
    >
      <Text className="uppercase tracking-wide">Liquidity</Text>
      <Stat
        label="Deployable"
        value={fmtMoneyCompact(liq.deployable_cash)}
        note="Cash plus the parked sweep vehicle, which is sold on demand to fund buys — the exact figure the trading engine sizes against"
      />
      <Stat label="Cash" value={fmtMoneyCompact(liq.raw_cash)} />
      <Stat
        label={liq.sweep_symbol || "SGOV"}
        value={liq.sweep_enabled ? fmtMoneyCompact(liq.sweep_parked_value) : "disabled"}
        note="Deterministic cash parking"
      />
      <Stat
        label="Cash after reserve"
        value={fmtMoneyCompact(liq.cash_above_reserve)}
        note="Raw cash spendable without selling the sweep vehicle first — conservative, and not what sizing uses"
      />
      <Stat
        label="Reserve"
        value={fmtMoneyCompact(liq.reserve_usd)}
        note="Sweep mechanic: the cushion park_excess leaves behind. Does not reduce deployable cash"
      />
      <Stat label="Directional" value={fmtMoneyCompact(directionalExposure)} note="Long + bearish hedge; SGOV excluded" />
      {accountError && (
        <Badge color="amber" size="xs" className="ml-auto">
          stale
        </Badge>
      )}
    </div>
  );
}
