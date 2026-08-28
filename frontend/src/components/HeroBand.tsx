import {
  Badge,
  Card,
  Grid,
  Metric,
  ProgressBar,
  SparkAreaChart,
  Text,
  Title,
} from "@tremor/react";
import { AccountResponse, PositionItem, RunFunnelResponse } from "../api/client";
import { fmtMoney, fmtMoneyCompact, fmtPct, pnlClass } from "../lib/format";
import { LevelBar } from "./ui/Meter";
import { Pill } from "./ui/Pill";

function equityHistorySeries(account: AccountResponse | null): { date: string; equity: number }[] {
  if (!account?.history?.length) return [];
  return [...account.history]
    .reverse()
    .map((p) => ({ date: p.date, equity: p.equity_close ?? p.total_value ?? 0 }))
    .filter((p) => p.equity > 0);
}

function RegimeCard({ funnel }: { funnel: RunFunnelResponse | null }) {
  const macro = funnel?.macro_context;
  if (!macro?.regime) {
    return (
      <Card className="!bg-panel !p-3.5 !ring-border h-full">
        <Text className="uppercase tracking-wide">Market regime</Text>
        <Title className="mt-2 text-ink">Awaiting macro evidence</Title>
        <Text className="mt-2 leading-relaxed">This reads once a run&rsquo;s macro specialist reports.</Text>
      </Card>
    );
  }

  const outlookTone =
    macro.equity_outlook === "bullish" ? "text-pos" : macro.equity_outlook === "bearish" ? "text-neg" : "text-dim";
  const confidenceTone = macro.confidence === "high" ? "pos" : macro.confidence === "medium" ? "warn" : "dim";

  return (
    <Card decoration="top" decorationColor="violet" className="!bg-panel !p-3.5 !ring-border h-full">
      <Text className="uppercase tracking-wide">Market regime</Text>
      <div className="mt-2 flex items-center gap-2 flex-wrap">
        <Pill text={macro.regime} />
        <span className={`text-sm font-bold tracking-wide ${outlookTone}`}>
          {(macro.equity_outlook || "unknown").toUpperCase()}
        </span>
      </div>
      {macro.confidence && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-xs text-dim">
            <span>Confidence</span>
            <Badge color={macro.confidence === "high" ? "emerald" : macro.confidence === "medium" ? "amber" : "slate"} size="xs">
              {macro.confidence}
            </Badge>
          </div>
          <LevelBar level={macro.confidence} tone={confidenceTone} />
        </div>
      )}
      {macro.summary && <Text className="mt-3 leading-snug line-clamp-2">{macro.summary}</Text>}
    </Card>
  );
}

export function HeroBand({
  account,
  accountError,
  positions,
  funnel,
  collapsed = false,
}: {
  account: AccountResponse | null;
  accountError: string | null;
  positions: PositionItem[];
  funnel: RunFunnelResponse | null;
  /* Compact chrome (see App.tsx's chrome-collapse control): the same
   * facts — net liquidation value, today's P&L, unrealized, deployed
   * exposure, regime — rendered as one dense line instead of three cards.
   * Nothing is hidden that isn't recoverable by expanding; what goes is
   * the equity sparkline and the card chrome, which is what was costing
   * the chart its vertical room. */
  collapsed?: boolean;
}) {
  if (!account) {
    return (
      <Card className="mx-3 mt-3 !w-auto !bg-panel !p-3 !ring-border text-center">
        <Text>{accountError ? `Account unavailable: ${accountError}` : "Loading account…"}</Text>
      </Card>
    );
  }

  const unrealized = positions
    .filter((p) => !p.is_cash_equivalent)
    .reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const liquidity = account.liquidity;
  const total = account.portfolio_value || 0;
  const longMv = positions.filter((p) => p.direction === "long").reduce((sum, p) => sum + (p.market_value || 0), 0);
  const hedgeMv = positions
    .filter((p) => p.direction === "bearish_hedge")
    .reduce((sum, p) => sum + (p.market_value || 0), 0);
  const cashMv = Math.max(total - longMv - hedgeMv, 0);
  const riskDeployedPct = total > 0 ? ((longMv + hedgeMv) / total) * 100 : 0;
  const maxTotalPct = account.risk_limits?.max_total_position_pct ?? null;
  const history = equityHistorySeries(account);

  if (collapsed) {
    const macro = funnel?.macro_context;
    return (
      <div className="mx-3 mt-3 flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-border bg-panel px-3 py-2">
        <span className="flex items-baseline gap-2">
          <span className="label-xs">NLV</span>
          <span className="font-mono text-[length:var(--fs-stat)] font-semibold tabular-nums text-ink">
            {fmtMoney(account.portfolio_value)}
          </span>
        </span>
        <span className={`font-mono text-[length:var(--fs-body)] font-semibold tabular-nums ${pnlClass(account.daily_pnl)}`}>
          {fmtMoney(account.daily_pnl)} ({fmtPct(account.daily_pnl_pct)}) today
        </span>
        <span className={`font-mono text-[length:var(--fs-body)] tabular-nums ${pnlClass(unrealized)}`}>
          {fmtMoney(unrealized)} unrealized
        </span>
        <span className="text-[length:var(--fs-meta)] text-dim">
          {riskDeployedPct.toFixed(0)}% deployed
          {maxTotalPct !== null ? ` / ${maxTotalPct.toFixed(0)}% ceiling` : ""}
        </span>
        <span className="text-[length:var(--fs-meta)] text-dim">
          Deployable <strong className="font-mono text-ink">{fmtMoneyCompact(liquidity?.deployable_cash)}</strong>
        </span>
        {macro?.regime && (
          <span className="flex items-center gap-1.5">
            <Pill text={macro.regime} />
            <span
              className={`text-[length:var(--fs-meta)] font-bold tracking-wide ${
                macro.equity_outlook === "bullish" ? "text-pos" : macro.equity_outlook === "bearish" ? "text-neg" : "text-dim"
              }`}
            >
              {(macro.equity_outlook || "unknown").toUpperCase()}
            </span>
          </span>
        )}
        {accountError && (
          <Badge color="amber" size="xs" className="ml-auto">
            stale
          </Badge>
        )}
      </div>
    );
  }

  return (
    <div className="mx-3 mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[1.2fr_1fr_1fr]">
      <Card
        decoration="top"
        decorationColor={accountError ? "amber" : "cyan"}
        className="!bg-panel !p-3.5 !ring-border h-full"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <Text className="uppercase tracking-wide">Net liquidation value</Text>
            <Metric className="mt-1 font-mono !text-2xl tabular-nums text-ink">{fmtMoney(account.portfolio_value)}</Metric>
          </div>
          {accountError && <Badge color="amber">stale</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 text-sm">
          <span className={`font-mono font-semibold tabular-nums ${pnlClass(account.daily_pnl)}`}>
            {fmtMoney(account.daily_pnl)} ({fmtPct(account.daily_pnl_pct)}) today
          </span>
          <span className={`font-mono tabular-nums ${pnlClass(unrealized)}`}>{fmtMoney(unrealized)} unrealized</span>
        </div>
        {history.length > 1 && (
          <SparkAreaChart
            data={history}
            index="date"
            categories={["equity"]}
            colors={["cyan"]}
            className="mt-2 h-8"
            showGradient
          />
        )}
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-dim">
          <span>
            Deployable <strong className="font-mono text-ink">{fmtMoneyCompact(liquidity?.deployable_cash)}</strong>
          </span>
          <span>
            Sweep parked{" "}
            <strong className="font-mono text-ink">
              {liquidity?.sweep_enabled
                ? `${fmtMoneyCompact(liquidity.sweep_parked_value)} ${liquidity.sweep_symbol || ""}`
                : "disabled"}
            </strong>
          </span>
        </div>
      </Card>

      <Card decoration="top" decorationColor="cyan" className="!bg-panel !p-3.5 !ring-border h-full">
        <div className="flex items-start justify-between gap-3">
          <div>
            <Text className="uppercase tracking-wide">Portfolio exposure</Text>
            <Title className="mt-1 font-mono !text-2xl text-ink">{riskDeployedPct.toFixed(0)}% deployed</Title>
          </div>
          {maxTotalPct !== null && <Badge color="slate">ceiling {maxTotalPct.toFixed(0)}%</Badge>}
        </div>
        <ProgressBar value={riskDeployedPct} color="cyan" className="mt-3" />
        <Grid numItems={3} className="mt-3 gap-2">
          <div><Text className="text-xs uppercase">Long</Text><Metric className="font-mono text-base text-pos">{fmtMoneyCompact(longMv)}</Metric></div>
          <div><Text className="text-xs uppercase">Hedge</Text><Metric className="font-mono text-base text-hedge">{fmtMoneyCompact(hedgeMv)}</Metric></div>
          <div><Text className="text-xs uppercase">Liquidity</Text><Metric className="font-mono text-base text-ink">{fmtMoneyCompact(cashMv)}</Metric></div>
        </Grid>
      </Card>

      <RegimeCard funnel={funnel} />
    </div>
  );
}
