import { useEffect, useState } from "react";
import { Button, Card, Text } from "@tremor/react";
import { api } from "../api/client";

/** One thin row above the chart: company name (when the cache has one),
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
    <Card className="flex flex-shrink-0 !bg-panel-alt !p-1.5 !ring-border">
      <div className="flex min-w-0 flex-1 flex-nowrap items-center gap-2 overflow-hidden">
        {previousSymbol && previousSymbol !== symbol && onGoBack && (
          <Button
            type="button"
            variant="secondary"
            size="xs"
            color="cyan"
            onClick={onGoBack}
            title={`Back to ${previousSymbol}`}
            aria-label={`Back to ${previousSymbol}`}
          >
            &larr; {previousSymbol}
          </Button>
        )}
        {companyName && (
          <span className="min-w-0 truncate text-[length:var(--fs-body)] text-ink">{companyName}</span>
        )}
        <span className="flex-shrink-0 font-bold">{symbol || "Market"}</span>
        {!symbol && <Text>Market context; no selected-run candidate evidence.</Text>}
        {canOpenLifecycle && (
          <Button className="ml-auto flex-shrink-0" size="xs" variant="light" color="cyan" onClick={onOpenLifecycle}>
            Lifecycle &rarr;
          </Button>
        )}
      </div>
    </Card>
  );
}
