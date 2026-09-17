import { useEffect, useState } from "react";
import { api } from "../api/client";

/** One short row above the chart: company name (when the cache has one),
 * ticker, Lifecycle. No holding grid, no decision strip — those live on
 * Positions and inside Lifecycle. A missing name stays missing; nothing
 * here invents a title. */
export function ChartSymbolBar({
  symbol,
  previousSymbol,
  onGoBack,
  canOpenLifecycle,
  onOpenLifecycle,
}: {
  symbol: string | null;
  previousSymbol?: string | null;
  onGoBack?: () => void;
  canOpenLifecycle: boolean;
  onOpenLifecycle: () => void;
}) {
  const [companyName, setCompanyName] = useState<string | null>(null);

  useEffect(() => {
    if (!symbol) {
      setCompanyName(null);
      return;
    }
    let cancelled = false;
    setCompanyName(null);
    api
      .company(symbol)
      .then((row) => {
        if (cancelled) return;
        setCompanyName(row.symbol === symbol ? row.name : null);
      })
      .catch(() => {
        if (!cancelled) setCompanyName(null);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  return (
    <div className="flex h-7 min-w-0 flex-shrink-0 flex-nowrap items-center gap-2 overflow-hidden px-1">
      {previousSymbol && previousSymbol !== symbol && onGoBack && (
        <button
          type="button"
          className="flex-shrink-0 text-[length:var(--fs-meta)] text-accent hover:underline"
          onClick={onGoBack}
          title={`Back to ${previousSymbol}`}
          aria-label={`Back to ${previousSymbol}`}
        >
          &larr; {previousSymbol}
        </button>
      )}
      {companyName && (
        <span className="min-w-0 truncate text-[length:var(--fs-body)] text-ink">{companyName}</span>
      )}
      <span className="flex-shrink-0 font-semibold">{symbol || "Market"}</span>
      {!symbol && (
        <span className="truncate text-[length:var(--fs-meta)] text-dim">Market context; no selected-run candidate evidence.</span>
      )}
      {canOpenLifecycle && (
        <button
          type="button"
          className="ml-auto flex-shrink-0 text-[length:var(--fs-meta)] text-accent hover:underline"
          onClick={onOpenLifecycle}
        >
          Lifecycle
        </button>
      )}
    </div>
  );
}
