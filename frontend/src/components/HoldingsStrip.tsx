import { useEffect, useState } from "react";
import { Badge, Text } from "@tremor/react";
import { PositionItem } from "../api/client";
import { fmtMoney, fmtNum, fmtPct, pnlClass } from "../lib/format";

/* "What do I hold, and what is it doing?" — the first question a trader
 * asks on arrival, answered in an always-visible strip rather than behind
 * a workspace tab. Cards keep their existing content and stay about this
 * size; they wrap at four per row on a normal desktop (fewer on a
 * narrower window) instead of sliding sideways. On desktop this lives in
 * a Dockview panel the operator can move, dock and resize; on iPad/phone
 * it stays a header strip that compact chrome collapses to a count/P&L
 * line. PositionsPanel remains
 * the full, sortable, column-complete view (now its own dockable panel);
 * this is the glance. Every figure here is the same broker-marked
 * PositionItem data that panel renders — no separate fetch, no
 * re-derivation.
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
      className={`flex items-center gap-2.5 rounded-lg border px-2.5 py-1.5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-accent/60 ${
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
  compact = false,
  variant = "page",
}: {
  positions: PositionItem[];
  error?: string | null;
  updatedAt?: Date | null;
  onSelectSymbol?: (symbol: string) => void;
  /* iPad/phone header: one summary line until expanded. Desktop Dockview
   * uses variant="panel" and always shows the wrap grid — the panel itself
   * is what the operator resizes. */
  compact?: boolean;
  variant?: "page" | "panel";
}) {
  const isPanel = variant === "panel";
  const [expanded, setExpanded] = useState(isPanel || !compact);
  useEffect(() => {
    setExpanded(isPanel || !compact);
  }, [compact, isPanel]);

  const directional = positions.filter((p) => !p.is_cash_equivalent);
  const unrealized = directional.reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const everLoaded = Boolean(updatedAt);
  const summary = (
    <>
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
    </>
  );

  return (
    <section
      className={isPanel ? "min-w-0 overflow-x-hidden" : "mx-3 mt-1 min-w-0 overflow-x-hidden"}
      aria-label="Holdings"
    >
      {compact && !isPanel ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="flex w-full min-w-0 items-center gap-2 rounded-lg border border-border bg-panel px-3 py-1 text-left hover:border-accent/60"
        >
          {summary}
          <span className="ml-auto text-xs text-dim flex-shrink-0" aria-hidden="true">
            {expanded ? "▾ hide" : "▸ show"}
          </span>
        </button>
      ) : (
        <div className="flex items-center gap-2 pb-1.5">{summary}</div>
      )}
      {expanded &&
        (positions.length === 0 ? (
          <div className="mt-1 rounded-lg border border-border bg-panel-alt px-3 py-2 text-[length:var(--fs-meta)] text-dim">
            {error && !everLoaded ? `Positions read failed: ${error}` : "No open positions."}
          </div>
        ) : (
          <div className="mt-1 grid w-full min-w-0 grid-cols-1 justify-items-start gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {holdingsOrder(positions).map((position) => (
              <HoldingChip key={position.symbol} position={position} onSelect={onSelectSymbol} />
            ))}
          </div>
        ))}
    </section>
  );
}
