import { Badge, Text } from "@tremor/react";
import { PositionItem } from "../api/client";
import { fmtMoney, fmtNum, fmtPct, pnlClass } from "../lib/format";

/* "What do I hold, and what is it doing?" — the first question a trader
 * asks on arrival, answered in one always-visible row rather than behind a
 * workspace tab. Deliberately a dense chip strip and not a second table:
 * PositionsPanel remains the full, sortable, column-complete view (now its
 * own dockable panel); this is the glance. Every figure here is the same
 * broker-marked PositionItem data that panel renders — no separate fetch,
 * no re-derivation.
 *
 * Cash parking (SGOV) is kept visible but visually demoted and excluded
 * from the P&L total, matching the exclusion rule LiquidityPanel and
 * HeroBand already state. */

export function holdingsOrder(positions: PositionItem[]): PositionItem[] {
  return [...positions].sort((a, b) => {
    if (a.is_cash_equivalent !== b.is_cash_equivalent) return a.is_cash_equivalent ? 1 : -1;
    return Math.abs(b.market_value || 0) - Math.abs(a.market_value || 0);
  });
}

/** Unrealized P&L as a percentage of cost basis, or null when the entry
 * basis is unknown/zero — never a fabricated 0%. */
export function unrealizedPct(position: PositionItem): number | null {
  const basis = (position.avg_entry || 0) * Math.abs(position.qty || 0);
  if (!basis || !Number.isFinite(basis)) return null;
  return ((position.unrealized_pnl || 0) / basis) * 100;
}

function HoldingChip({ position, onSelect }: { position: PositionItem; onSelect?: (symbol: string) => void }) {
  const cash = position.is_cash_equivalent;
  const pct = unrealizedPct(position);
  return (
    <button
      type="button"
      onClick={() => onSelect?.(position.symbol)}
      aria-label={`Chart ${position.symbol}`}
      className={`flex shrink-0 items-center gap-2.5 rounded-lg border px-2.5 py-1.5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-accent/60 ${
        cash ? "border-border bg-panel-inset opacity-80" : "border-border bg-panel-alt hover:border-accent"
      }`}
    >
      <span className="flex flex-col">
        <span className={`font-bold leading-tight ${cash ? "text-dim" : "text-accent"}`}>{position.symbol}</span>
        <span className="font-mono text-[length:var(--fs-micro)] leading-tight text-dim">
          {fmtNum(position.qty)} @ {fmtMoney(position.avg_entry)}
        </span>
      </span>
      <span className="flex flex-col text-right">
        <span className="font-mono text-[length:var(--fs-meta)] leading-tight text-ink">
          {fmtMoney(position.current_price)}
        </span>
        {cash ? (
          <span className="text-[length:var(--fs-micro)] leading-tight text-dim">cash parking</span>
        ) : (
          <span className={`font-mono text-[length:var(--fs-micro)] leading-tight ${pnlClass(position.unrealized_pnl)}`}>
            {fmtMoney(position.unrealized_pnl)}
            {pct === null ? "" : ` (${fmtPct(pct)})`}
          </span>
        )}
      </span>
    </button>
  );
}

export function HoldingsStrip({
  positions,
  error,
  updatedAt,
  onSelectSymbol,
}: {
  positions: PositionItem[];
  error?: string | null;
  updatedAt?: Date | null;
  onSelectSymbol?: (symbol: string) => void;
}) {
  const directional = positions.filter((p) => !p.is_cash_equivalent);
  const unrealized = directional.reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const everLoaded = Boolean(updatedAt);

  return (
    <section className="mx-3 mt-3" aria-label="Holdings">
      <div className="flex items-center gap-2 pb-1.5">
        <Text className="uppercase tracking-wide">Holdings</Text>
        <Badge color="slate" size="xs">
          {directional.length} open
        </Badge>
        {directional.length > 0 && (
          <span className={`font-mono text-[length:var(--fs-meta)] font-semibold ${pnlClass(unrealized)}`}>
            {fmtMoney(unrealized)} unrealized
          </span>
        )}
        {error && (
          <Badge color="amber" size="xs" className="ml-auto">
            {everLoaded ? "stale" : "unavailable"}
          </Badge>
        )}
      </div>
      {positions.length === 0 ? (
        <div className="rounded-lg border border-border bg-panel-alt px-3 py-2 text-[length:var(--fs-meta)] text-dim">
          {error && !everLoaded ? `Positions read failed: ${error}` : "No open positions."}
        </div>
      ) : (
        <div className="flex gap-2 overflow-x-auto pb-0.5">
          {holdingsOrder(positions).map((position) => (
            <HoldingChip key={position.symbol} position={position} onSelect={onSelectSymbol} />
          ))}
        </div>
      )}
    </section>
  );
}
